#!/usr/bin/env python3
"""SZTFH tobacco retail price scraper and diff generator."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
from io import BytesIO
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import openpyxl
from openpyxl.utils.datetime import WINDOWS_EPOCH, from_excel
import requests

SZTFH_PAGE_URL = "https://sztfh.hu/tevekenysegek/dohanyugyi-felugyelet/arak-kozzetetele/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; DohanyArfigyelo/1.0; "
    "+https://github.com/kicsirigo/dohany-arfigyelo)"
)
DEFAULT_OUTPUT_PATH = Path("data/prices.json")
DEFAULT_TIMEOUT = 30

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP_RE = re.compile(r"(20\d{6,12})")
_NUMERIC_STR_RE = re.compile(r"^\d+(?:[.,]\d+)?$")
_SUMMARY_PREFIXES = ("osszesen", "mindosszesen", "sum", "total")

logger = logging.getLogger("update_prices")


def _normalize_for_match(raw: object) -> str:
    """Fold Hungarian accents, lowercase, and collapse whitespace for matching."""
    if raw is None:
        return ""
    decomposed = unicodedata.normalize("NFD", str(raw))
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return " ".join(stripped.lower().split())


def normalize_name(raw: object) -> str:
    """Preserve official product name, stripping ends and collapsing whitespace."""
    if raw is None:
        return ""
    return " ".join(str(raw).split())


def _format_quantity(qty: object) -> str:
    """Format Mennyiseg value, stripping .0 on whole numbers while keeping decimals."""
    if qty is None or isinstance(qty, bool):
        return ""
    if isinstance(qty, int):
        return str(qty)
    if isinstance(qty, float):
        if math.isnan(qty) or math.isinf(qty):
            return ""
        if qty.is_integer():
            return str(int(qty))
        return f"{round(qty, 6):.6f}".rstrip("0").rstrip(".")
    text = " ".join(str(qty).split())
    if not text:
        return ""
    if _NUMERIC_STR_RE.match(text):
        normalized = text.replace(",", ".")
        if "." in normalized:
            try:
                num = float(normalized)
            except ValueError:
                return text
            if num.is_integer():
                return str(int(num))
            return normalized.rstrip("0").rstrip(".")
        return normalized
    return text


def normalize_unit(
    qty: object, unit_name: object, combined: object = None
) -> str:
    """Build normalized unit string from Mennyiseg + Mennyisegi egyseg or Kiszereles."""
    qty_str = _format_quantity(qty)
    unit_str = normalize_name(unit_name)
    combined_str = normalize_name(combined)

    if qty_str and unit_str:
        return f"{qty_str} {unit_str}"
    if qty_str:
        return qty_str
    if unit_str:
        return unit_str
    if combined_str:
        return combined_str
    return ""


def parse_price(raw: object) -> int | None:
    """Parse positive integer HUF price from numeric or formatted string cells."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, float):
        if math.isnan(raw) or math.isinf(raw) or not raw.is_integer():
            return None
        val = int(raw)
        return val if val > 0 else None
    if isinstance(raw, str):
        cleaned = raw.strip()
        if not cleaned:
            return None
        cleaned = re.sub(r"(?i)(?:ft|huf)", "", cleaned)
        cleaned = re.sub(r"[\s\u00a0\u2009\u202f]+", "", cleaned)
        if not cleaned:
            return None
        if re.fullmatch(r"\d+", cleaned):
            val = int(cleaned)
            return val if val > 0 else None
        if re.fullmatch(r"\d+\.0+", cleaned):
            val = int(cleaned.split(".", 1)[0])
            return val if val > 0 else None
        return None
    return None


def parse_valid_from(raw: object, wb_epoch: datetime = WINDOWS_EPOCH) -> str:
    """Normalize Excel date/serial/string values into YYYY-MM-DD or ''."""
    if raw is None or isinstance(raw, bool):
        return ""
    if isinstance(raw, datetime):
        return raw.strftime("%Y-%m-%d")
    if isinstance(raw, date):
        return raw.strftime("%Y-%m-%d")
    if isinstance(raw, (int, float)):
        if isinstance(raw, float) and (math.isnan(raw) or math.isinf(raw)):
            return ""
        if raw <= 0:
            return ""
        try:
            converted = from_excel(raw, epoch=wb_epoch or WINDOWS_EPOCH)
            if isinstance(converted, (datetime, date)):
                return converted.strftime("%Y-%m-%d")
        except (ValueError, OverflowError, TypeError):
            return ""
        return ""
    if isinstance(raw, str):
        text = " ".join(raw.split())
        if not text:
            return ""
        # YYYY-MM-DD or ISO datetime prefix
        match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T\s].*)?$", text)
        if match:
            try:
                y, m, d = map(int, match.groups())
                return date(y, m, d).strftime("%Y-%m-%d")
            except ValueError:
                return ""
        # YYYY.MM.DD or YYYY.MM.DD.
        match = re.match(r"^(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?$", text)
        if match:
            try:
                y, m, d = map(int, match.groups())
                return date(y, m, d).strftime("%Y-%m-%d")
            except ValueError:
                return ""
        # YYYY/MM/DD
        match = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})$", text)
        if match:
            try:
                y, m, d = map(int, match.groups())
                return date(y, m, d).strftime("%Y-%m-%d")
            except ValueError:
                return ""
        # DD.MM.YYYY / DD/MM/YYYY / DD-MM-YYYY
        match = re.match(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$", text)
        if match:
            try:
                d, m, y = map(int, match.groups())
                return date(y, m, d).strftime("%Y-%m-%d")
            except ValueError:
                return ""
        return ""
    return ""


def compute_item_id(name: str, unit: str) -> str:
    """Compute deterministic 16-char lowercase hex ID from normalized name + unit."""
    payload = f"{name}|{unit}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _valid_from_key(valid_from: str) -> tuple[int, str]:
    """Sort key ensuring valid YYYY-MM-DD dates strictly beat empty/invalid values."""
    if valid_from and _ISO_DATE_RE.match(valid_from):
        return (1, valid_from)
    return (0, "")


def _is_excluded_xlsx(filename_norm: str, text_norm: str) -> bool:
    combined = f"{filename_norm} {text_norm}"
    return "2017" in combined or "valtozas" in combined


def _extract_timestamp_key(url: str) -> str:
    name = Path(urlparse(url).path).name
    matches = _TIMESTAMP_RE.findall(name)
    return matches[-1] if matches else ""


def find_xlsx_url(html: str, page_url: str) -> str:
    """Discover the active full SZTFH .xlsx price-list URL from HTML."""
    soup = BeautifulSoup(html, "html.parser")
    primary: list[tuple[int, str, str]] = []
    secondary: list[tuple[int, str, str]] = []
    fallback: list[tuple[int, str, str]] = []

    for idx, anchor in enumerate(soup.find_all("a", href=True)):
        raw_href = str(anchor["href"]).strip()
        if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:")):
            continue
        full_url = urljoin(page_url, raw_href)
        parsed = urlparse(full_url)
        if not parsed.path.lower().endswith(".xlsx"):
            continue

        filename_norm = _normalize_for_match(Path(parsed.path).name)
        text_norm = _normalize_for_match(anchor.get_text(" ", strip=True))
        if _is_excluded_xlsx(filename_norm, text_norm):
            continue

        ts_key = _extract_timestamp_key(full_url)
        candidate = (idx, ts_key, full_url)
        if "alkalmazando_dohanygyartmany_kiskereskedelmi_eladasi_arak" in filename_norm:
            primary.append(candidate)
        elif any(
            token in f"{filename_norm} {text_norm}"
            for token in ("alkalmazand", "eladasi_arak", "eladasi arak", "arlista")
        ):
            secondary.append(candidate)
        else:
            fallback.append(candidate)

    for bucket in (primary, secondary, fallback):
        if bucket:
            # Prefer latest embedded timestamp if present, else earliest in document.
            if any(item[1] for item in bucket):
                best = max(bucket, key=lambda item: (item[1], -item[0]))
            else:
                best = bucket[0]
            return best[2]

    raise ValueError(f"No active SZTFH .xlsx price list link found at {page_url}")


def _match_header_row(row: tuple[object, ...]) -> dict[str, int] | None:
    """Identify column indices from a candidate header row."""
    col_map: dict[str, int] = {}
    for idx, cell in enumerate(row):
        norm = _normalize_for_match(cell)
        if not norm:
            continue
        if "name" not in col_map and (
            norm in {"termek megnevezese", "termeknev", "megnevezes", "termek neve"}
            or norm.startswith("termek megnevezese")
        ):
            col_map["name"] = idx
        elif "category" not in col_map and norm in {
            "termek tipus",
            "termek tipusa",
            "kategoria",
            "tipus",
        }:
            col_map["category"] = idx
        elif "qty" not in col_map and norm == "mennyiseg":
            col_map["qty"] = idx
        elif "unit_label" not in col_map and norm in {
            "mennyisegi egyseg",
            "egyseg",
            "mertekegyseg",
        }:
            col_map["unit_label"] = idx
        elif "combined_unit" not in col_map and norm == "kiszereles":
            col_map["combined_unit"] = idx
        elif "price" not in col_map and (
            norm
            in {
                "kiskereskedelmi eladasi ar",
                "eladasi ar",
                "ar",
                "fogyasztoi ar",
            }
            or "kiskereskedelmi eladasi ar" in norm
        ):
            col_map["price"] = idx
        elif "valid_from" not in col_map and norm in {
            "ervenyesseg kezdete",
            "ervenyesseg",
            "hatalyos",
            "hatalybalepes",
            "datum",
        }:
            col_map["valid_from"] = idx

    if "name" in col_map and "price" in col_map:
        return col_map
    return None


def _get_cell(row: tuple[object, ...], idx: int | None) -> object:
    if idx is None or idx < 0 or idx >= len(row):
        return None
    return row[idx]


def _is_empty_cell(val: object) -> bool:
    return val is None or str(val).strip() == ""


def _is_summary_text(text: str) -> bool:
    norm = _normalize_for_match(text)
    return any(
        norm == prefix or norm.startswith(f"{prefix} ") or norm.startswith(f"{prefix}:")
        for prefix in _SUMMARY_PREFIXES
    )


def parse_workbook(xlsx_bytes: bytes) -> list[dict]:
    """Parse SZTFH .xlsx bytes into deduplicated normalized product dicts."""
    wb = openpyxl.load_workbook(BytesIO(xlsx_bytes), data_only=True)
    try:
        wb_epoch = getattr(wb, "epoch", WINDOWS_EPOCH) or WINDOWS_EPOCH
        sheets = [wb.active] + [s for s in wb.worksheets if s is not wb.active]

        target_ws = None
        header_row_idx = None
        col_map: dict[str, int] | None = None

        for ws in sheets:
            for row_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
                if row_idx > 50:
                    break
                matched = _match_header_row(row)
                if matched is not None:
                    target_ws = ws
                    header_row_idx = row_idx
                    col_map = matched
                    break
            if target_ws is not None:
                break

        if target_ws is None or header_row_idx is None or col_map is None:
            raise ValueError(
                "Could not locate header row with name and price columns in workbook."
            )

        dedup_map: dict[str, dict] = {}
        current_section_category: str | None = None

        for row_idx, row in enumerate(target_ws.iter_rows(values_only=True), start=1):
            if row_idx <= header_row_idx:
                continue

            non_empty = [(i, v) for i, v in enumerate(row) if not _is_empty_cell(v)]
            if not non_empty:
                continue

            raw_name = _get_cell(row, col_map.get("name"))
            raw_price = _get_cell(row, col_map.get("price"))
            raw_qty = _get_cell(row, col_map.get("qty"))
            raw_unit_label = _get_cell(row, col_map.get("unit_label"))
            raw_combined = _get_cell(row, col_map.get("combined_unit"))
            raw_valid_from = _get_cell(row, col_map.get("valid_from"))
            raw_category = _get_cell(row, col_map.get("category"))

            price = parse_price(raw_price)

            # Detect single-cell or header-only category-separator rows
            if price is None:
                if len(non_empty) == 1 and isinstance(non_empty[0][1], str):
                    candidate_cat = normalize_name(non_empty[0][1])
                    if candidate_cat and not _is_summary_text(candidate_cat):
                        current_section_category = candidate_cat
                    continue

                if (
                    not _is_empty_cell(raw_name)
                    and _is_empty_cell(raw_qty)
                    and _is_empty_cell(raw_unit_label)
                    and _is_empty_cell(raw_combined)
                    and _is_empty_cell(raw_valid_from)
                ):
                    candidate_cat = normalize_name(raw_name)
                    if candidate_cat and not _is_summary_text(candidate_cat):
                        current_section_category = candidate_cat
                    continue

                continue

            name = normalize_name(raw_name)
            if not name or _is_summary_text(name):
                continue

            unit = normalize_unit(raw_qty, raw_unit_label, raw_combined)
            cat_from_col = normalize_name(raw_category)
            if cat_from_col:
                category = cat_from_col
            elif current_section_category:
                category = current_section_category
            else:
                category = "Egyéb"

            valid_from = parse_valid_from(raw_valid_from, wb_epoch=wb_epoch)
            item_id = compute_item_id(name, unit)

            item = {
                "id": item_id,
                "name": name,
                "category": category,
                "unit": unit,
                "price": price,
                "valid_from": valid_from,
            }

            existing = dedup_map.get(item_id)
            if existing is None or _valid_from_key(valid_from) >= _valid_from_key(
                existing["valid_from"]
            ):
                dedup_map[item_id] = item

        return list(dedup_map.values())
    finally:
        wb.close()


def _core_records(items: list[dict]) -> list[tuple]:
    try:
        return [
            (
                it["id"],
                it["name"],
                it["category"],
                it["unit"],
                it["price"],
                it["valid_from"],
            )
            for it in items
        ]
    except (KeyError, TypeError):
        return []


def load_prior_data(output_path: Path) -> dict | None:
    """Safely load existing prices.json, returning None if missing or invalid."""
    if not output_path.exists():
        return None
    try:
        content = output_path.read_text(encoding="utf-8")
        if not content.strip():
            logger.warning("Prior %s is empty; treating as fresh baseline.", output_path)
            return None
        data = json.loads(content)
        if not isinstance(data, dict):
            logger.warning("Prior %s is not a JSON object; treating as baseline.", output_path)
            return None
        return data
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Prior %s unreadable (%s); treating as fresh baseline.", output_path, exc)
        return None


def should_skip_write(
    prior_data: dict | None, source_file: str, parsed_items: list[dict]
) -> bool:
    """Return True if prior prices.json matches source_file and core item payloads."""
    if prior_data is None:
        return False
    prior_meta = prior_data.get("meta")
    prior_items = prior_data.get("items")
    if (
        not isinstance(prior_meta, dict)
        or not isinstance(prior_items, list)
        or not prior_items
    ):
        return False
    if prior_meta.get("source_file") != source_file:
        return False
    if prior_meta.get("total_count") != len(parsed_items):
        return False
    return _core_records(prior_items) == _core_records(parsed_items)


def apply_price_diffs(
    parsed_items: list[dict],
    prior_items: list[dict],
    has_prior_catalog: bool,
    today_utc: str,
) -> list[dict]:
    """Compute previous_price, price_diff, changed_at, and is_new for all items."""
    prior_by_id = {
        item["id"]: item
        for item in prior_items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }

    result: list[dict] = []
    for item in parsed_items:
        price = item["price"]
        if not has_prior_catalog:
            previous_price = None
            price_diff = None
            changed_at = None
            is_new = False
        elif item["id"] not in prior_by_id:
            previous_price = None
            price_diff = None
            changed_at = today_utc
            is_new = True
        else:
            prev = prior_by_id[item["id"]]
            prev_price = prev.get("price")
            if isinstance(prev_price, int) and not isinstance(prev_price, bool) and price != prev_price:
                previous_price = prev_price
                price_diff = price - prev_price
                changed_at = today_utc
                is_new = False
            else:
                prev_prev = prev.get("previous_price")
                if isinstance(prev_prev, int) and not isinstance(prev_prev, bool):
                    previous_price = prev_prev
                    price_diff = price - prev_prev
                else:
                    previous_price = None
                    price_diff = None
                prev_changed = prev.get("changed_at")
                changed_at = prev_changed if isinstance(prev_changed, str) else None
                is_new = False

        result.append(
            {
                "id": item["id"],
                "name": item["name"],
                "category": item["category"],
                "unit": item["unit"],
                "price": price,
                "valid_from": item["valid_from"],
                "previous_price": previous_price,
                "price_diff": price_diff,
                "changed_at": changed_at,
                "is_new": is_new,
            }
        )

    return result


def atomic_write_json(output_path: Path, payload: dict) -> None:
    """Atomically write JSON with UTF-8 encoding, fsync, 0644 permissions, and os.replace."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.stem}_",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            json.dump(payload, tmp_file, ensure_ascii=False, indent=2)
            tmp_file.write("\n")
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, output_path)
    except Exception:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def update_prices(
    page_url: str = SZTFH_PAGE_URL,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    timeout: float = DEFAULT_TIMEOUT,
    now: datetime | None = None,
    session: requests.Session | None = None,
) -> bool:
    """Fetch SZTFH prices, compute diffs, and write output_path if changed.

    Returns True if output_path was written, False if skipped idempotently.
    """
    output_path = Path(output_path)
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    owns_session = session is None
    http = session or requests.Session()

    try:
        logger.info("Fetching SZTFH page: %s", page_url)
        page_resp = http.get(page_url, headers=headers, timeout=timeout)
        page_resp.raise_for_status()

        xlsx_url = find_xlsx_url(page_resp.text, page_url)
        source_file = Path(urlparse(xlsx_url).path).name
        logger.info("Downloading workbook: %s", xlsx_url)

        xlsx_resp = http.get(xlsx_url, headers=headers, timeout=timeout)
        xlsx_resp.raise_for_status()

        parsed_items = parse_workbook(xlsx_resp.content)
        if not parsed_items:
            raise ValueError(f"Parsed 0 product items from {xlsx_url}")

        prior_data = load_prior_data(output_path)
        if should_skip_write(prior_data, source_file, parsed_items):
            logger.info(
                "No changes detected for %s (%d items); leaving %s untouched.",
                source_file,
                len(parsed_items),
                output_path,
            )
            return False

        current_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        today_utc = current_utc.strftime("%Y-%m-%d")
        last_updated = current_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        prior_items: list[dict] = []
        if prior_data is not None and isinstance(prior_data.get("items"), list):
            prior_items = prior_data["items"]
        has_prior_catalog = any(
            isinstance(it, dict) and isinstance(it.get("id"), str) for it in prior_items
        )

        items = apply_price_diffs(
            parsed_items=parsed_items,
            prior_items=prior_items,
            has_prior_catalog=has_prior_catalog,
            today_utc=today_utc,
        )

        payload = {
            "meta": {
                "last_updated": last_updated,
                "source_file": source_file,
                "source_url": xlsx_url,
                "page_url": page_url,
                "total_count": len(items),
            },
            "items": items,
        }

        atomic_write_json(output_path, payload)
        logger.info("Wrote %d items to %s (source: %s)", len(items), output_path, source_file)
        return True
    finally:
        if owns_session:
            http.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Update tobacco retail prices from official SZTFH publications."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Path to output JSON file (default: data/prices.json).",
    )
    parser.add_argument(
        "--page-url",
        "--url",
        dest="page_url",
        default=SZTFH_PAGE_URL,
        help="SZTFH publication HTML page URL.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="HTTP request timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose debug logging.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )

    try:
        update_prices(
            page_url=args.page_url,
            output_path=args.output,
            timeout=args.timeout,
        )
        return 0
    except Exception as exc:
        logger.error("Price update failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
