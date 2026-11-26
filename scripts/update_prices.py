#!/usr/bin/env python3
"""SZTFH tobacco retail price scraper, diff generator, and 2017+ history builder."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
from html.parser import HTMLParser
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
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import openpyxl
from openpyxl.utils.datetime import WINDOWS_EPOCH, from_excel

SZTFH_PAGE_URL = "https://sztfh.hu/tevekenysegek/dohanyugyi-felugyelet/arak-kozzetetele/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; DohanyArfigyelo/1.0; "
    "+https://github.com/kicsirigo/dohany-arfigyelo)"
)
DEFAULT_OUTPUT_PATH = Path("data/prices.json")
DEFAULT_HISTORY_OUTPUT_PATH = Path("data/price_history.json")
DEFAULT_TIMEOUT = 30

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP_RE = re.compile(r"(20\d{6,12})")
_SUMMARY_PREFIXES = ("osszesen", "mindosszesen", "sum", "total")

_HEADER_ALIASES: dict[str, set[str]] = {
    "name": {"termek megnevezese", "termeknev", "megnevezes", "termek neve"},
    "category": {"termek tipus", "termek tipusa", "kategoria", "tipus"},
    "qty": {"mennyiseg"},
    "unit_label": {"mennyisegi egyseg", "egyseg", "mertekegyseg"},
    "combined_unit": {"kiszereles"},
    "price": {"kiskereskedelmi eladasi ar", "eladasi ar", "ar", "fogyasztoi ar"},
    "valid_from": {"ervenyesseg kezdete", "ervenyesseg", "hatalyos", "hatalybalepes", "datum"},
}

logger = logging.getLogger("update_prices")


class _HttpResponse:
    def __init__(self, status_code: int, content: bytes, url: str = "") -> None:
        self.status_code = status_code
        self.content = content
        self.url = url

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPError(self.url, self.status_code, f"HTTP {self.status_code}", {}, None)


class _UrllibSession:
    def get(self, url: str, headers: dict[str, str] | None = None, timeout: float = DEFAULT_TIMEOUT) -> _HttpResponse:
        req = Request(url, headers=headers or {})
        with urlopen(req, timeout=timeout) as resp:
            return _HttpResponse(getattr(resp, "status", 200), resp.read(), url=url)

    def close(self) -> None:
        pass


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self._href = href.strip()
                self._buf = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._buf).split())))
            self._href = None
            self._buf = []


def _normalize_for_match(raw: object) -> str:
    if raw is None:
        return ""
    decomposed = unicodedata.normalize("NFD", str(raw))
    return " ".join("".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn").lower().split())


def normalize_name(raw: object) -> str:
    """Preserve official product name, stripping ends and collapsing whitespace."""
    return "" if raw is None else " ".join(str(raw).split())


def _format_quantity(qty: object) -> str:
    if qty is None or isinstance(qty, bool):
        return ""
    if isinstance(qty, int):
        return str(qty)
    if isinstance(qty, float):
        if math.isnan(qty) or math.isinf(qty):
            return ""
        return str(int(qty)) if qty.is_integer() else f"{round(qty, 6):.6f}".rstrip("0").rstrip(".")
    text = " ".join(str(qty).split())
    if re.match(r"^\d+(?:[.,]\d+)?$", text):
        norm = text.replace(",", ".")
        if "." in norm:
            num = float(norm)
            return str(int(num)) if num.is_integer() else norm.rstrip("0").rstrip(".")
        return norm
    return text


def normalize_unit(qty: object, unit_name: object, combined: object = None) -> str:
    """Build normalized unit string from Mennyiseg + Mennyisegi egyseg or Kiszereles."""
    qty_str = _format_quantity(qty)
    unit_str = normalize_name(unit_name)
    if qty_str and unit_str:
        return f"{qty_str} {unit_str}"
    return qty_str or unit_str or normalize_name(combined)


def parse_price(raw: object) -> int | None:
    """Parse positive integer HUF price from numeric or formatted string cells."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, float):
        return int(raw) if not (math.isnan(raw) or math.isinf(raw)) and raw.is_integer() and raw > 0 else None
    cleaned = str(raw).strip()
    if not cleaned:
        return None
    cleaned = re.sub(r"(?i)\s*(?:ft|huf)\.?\s*$", "", cleaned)
    cleaned = cleaned.replace("\u00a0", "").replace(" ", "")
    if re.match(r"^\d+[.,]0+$", cleaned):
        cleaned = cleaned.split(".")[0].split(",")[0]
    elif re.match(r"^\d{1,3}(?:[.,]\d{3})+$", cleaned):
        cleaned = cleaned.replace(".", "").replace(",", "")
    if cleaned.isdigit():
        val = int(cleaned)
        return val if val > 0 else None
    return None


def parse_valid_from(raw: object, wb_epoch: datetime = WINDOWS_EPOCH) -> str:
    """Parse Excel date, datetime, serial number, or string into YYYY-MM-DD."""
    if raw is None or isinstance(raw, bool):
        return ""
    if isinstance(raw, datetime):
        return raw.date().strftime("%Y-%m-%d")
    if isinstance(raw, date):
        return raw.strftime("%Y-%m-%d")
    if isinstance(raw, (int, float)) and not (math.isnan(raw) or math.isinf(raw)) and 1 <= raw <= 2958465:
        try:
            dt = from_excel(raw, epoch=wb_epoch)
            return dt.strftime("%Y-%m-%d") if isinstance(dt, (date, datetime)) else ""
        except Exception:
            return ""
    text = " ".join(str(raw).split())
    for pattern, order in (
        (r"^(\d{4})[-./]\s*(\d{1,2})[-./]\s*(\d{1,2})(?:\.?|[T\s].*)?$", "ymd"),
        (r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$", "dmy"),
    ):
        m = re.match(pattern, text)
        if m:
            a, b, c = map(int, m.groups())
            y, mo, d = (a, b, c) if order == "ymd" else (c, b, a)
            try:
                return date(y, mo, d).strftime("%Y-%m-%d")
            except ValueError:
                return ""
    return ""


def compute_item_id(name: str, unit: str) -> str:
    """Compute deterministic 16-char lowercase hex ID from normalized name + unit."""
    return hashlib.sha256(f"{name}|{unit}".encode("utf-8")).hexdigest()[:16]


def _valid_from_key(valid_from: str) -> tuple[int, str]:
    return (1, valid_from) if (valid_from and _ISO_DATE_RE.match(valid_from)) else (0, "")


def _extract_xlsx_links(html: str, page_url: str) -> list[tuple[int, str, str, str, str]]:
    parser = _AnchorParser()
    parser.feed(html)
    results = []
    for idx, (raw_href, anchor_text) in enumerate(parser.links):
        if raw_href.startswith(("#", "javascript:", "mailto:")):
            continue
        full_url = urljoin(page_url, raw_href)
        parsed = urlparse(full_url)
        if not parsed.path.lower().endswith(".xlsx"):
            continue
        filename_norm = _normalize_for_match(Path(parsed.path).name)
        text_norm = _normalize_for_match(anchor_text)
        ts_matches = _TIMESTAMP_RE.findall(Path(parsed.path).name)
        ts_key = ts_matches[-1] if ts_matches else ""
        results.append((idx, ts_key, full_url, filename_norm, text_norm))
    return results


def _pick_best_candidate(candidates: list[tuple[int, str, str]]) -> str:
    if any(item[1] for item in candidates):
        return max(candidates, key=lambda item: (item[1], -item[0]))[2]
    return candidates[0][2]


def find_xlsx_url(html: str, page_url: str) -> str:
    """Discover the active full SZTFH .xlsx price-list URL from HTML."""
    primary, secondary, fallback = [], [], []
    for idx, ts_key, full_url, filename_norm, text_norm in _extract_xlsx_links(html, page_url):
        combined = f"{filename_norm} {text_norm}"
        if "2017" in combined or "valtozas" in combined:
            continue
        cand = (idx, ts_key, full_url)
        if "alkalmazando_dohanygyartmany_kiskereskedelmi_eladasi_arak" in filename_norm:
            primary.append(cand)
        elif any(tok in combined for tok in ("alkalmazand", "eladasi_arak", "eladasi arak", "arlista")):
            secondary.append(cand)
        else:
            fallback.append(cand)
    for bucket in (primary, secondary, fallback):
        if bucket:
            return _pick_best_candidate(bucket)
    raise ValueError(f"No active SZTFH .xlsx price list link found at {page_url}")


def find_history_xlsx_url(html: str, page_url: str) -> str | None:
    """Discover the cumulative 2017+ historical SZTFH .xlsx URL from HTML, if present."""
    candidates = [
        (idx, ts_key, full_url)
        for idx, ts_key, full_url, fn, txt in _extract_xlsx_links(html, page_url)
        if "2017" in f"{fn} {txt}" and "valtozas" not in f"{fn} {txt}"
    ]
    return _pick_best_candidate(candidates) if candidates else None


def _match_header_row(row: tuple[object, ...]) -> dict[str, int] | None:
    col_map: dict[str, int] = {}
    for idx, cell in enumerate(row):
        norm = _normalize_for_match(cell)
        if not norm:
            continue
        for key, aliases in _HEADER_ALIASES.items():
            if key in col_map:
                continue
            if norm in aliases or (key == "name" and norm.startswith("termek megnevezese")) or (
                key == "price" and "kiskereskedelmi eladasi ar" in norm
            ):
                col_map[key] = idx
                break
    return col_map if ("name" in col_map and "price" in col_map) else None


def _get_cell(row: tuple[object, ...], idx: int | None) -> object:
    return row[idx] if (idx is not None and 0 <= idx < len(row)) else None


def _is_empty_cell(val: object) -> bool:
    return val is None or str(val).strip() == ""


def _is_summary_text(text: str) -> bool:
    norm = _normalize_for_match(text)
    return any(norm == p or norm.startswith(f"{p} ") or norm.startswith(f"{p}:") for p in _SUMMARY_PREFIXES)


def _iter_parsed_rows(xlsx_bytes: bytes):
    """Yield normalized product dicts from the first matching sheet of an SZTFH workbook."""
    wb = openpyxl.load_workbook(BytesIO(xlsx_bytes), read_only=True, data_only=True)
    try:
        wb_epoch = getattr(wb, "epoch", WINDOWS_EPOCH) or WINDOWS_EPOCH
        sheets = [wb.active] + [s for s in wb.worksheets if s is not wb.active]
        target_ws, header_row_idx, col_map = None, None, None

        for ws in sheets:
            for row_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
                if row_idx > 50:
                    break
                matched = _match_header_row(row)
                if matched is not None:
                    target_ws, header_row_idx, col_map = ws, row_idx, matched
                    break
            if target_ws is not None:
                break

        if target_ws is None or header_row_idx is None or col_map is None:
            raise ValueError("Could not locate header row with name and price columns in workbook.")

        current_section_category: str | None = None
        for row_idx, row in enumerate(target_ws.iter_rows(values_only=True), start=1):
            if row_idx <= header_row_idx:
                continue
            non_empty = [(i, v) for i, v in enumerate(row) if not _is_empty_cell(v)]
            if not non_empty:
                continue

            raw_name = _get_cell(row, col_map.get("name"))
            raw_qty = _get_cell(row, col_map.get("qty"))
            raw_unit_label = _get_cell(row, col_map.get("unit_label"))
            raw_combined = _get_cell(row, col_map.get("combined_unit"))
            raw_valid_from = _get_cell(row, col_map.get("valid_from"))
            price = parse_price(_get_cell(row, col_map.get("price")))

            if price is None:
                if len(non_empty) == 1 and isinstance(non_empty[0][1], str):
                    cand = normalize_name(non_empty[0][1])
                    if cand and not _is_summary_text(cand):
                        current_section_category = cand
                elif not _is_empty_cell(raw_name) and all(
                    _is_empty_cell(v) for v in (raw_qty, raw_unit_label, raw_combined, raw_valid_from)
                ):
                    cand = normalize_name(raw_name)
                    if cand and not _is_summary_text(cand):
                        current_section_category = cand
                continue

            name = normalize_name(raw_name)
            if not name or _is_summary_text(name):
                continue

            unit = normalize_unit(raw_qty, raw_unit_label, raw_combined)
            category = normalize_name(_get_cell(row, col_map.get("category"))) or current_section_category or "Egyéb"
            valid_from = parse_valid_from(raw_valid_from, wb_epoch=wb_epoch)
            yield {
                "id": compute_item_id(name, unit),
                "name": name,
                "category": category,
                "unit": unit,
                "price": price,
                "valid_from": valid_from,
            }
    finally:
        wb.close()


def canonical_variant_key(category: str, name: str, unit: str) -> tuple[str, str, str]:
    """Compute a canonical key stripping regulatory/packaging suffixes (ECS, TT, 2025 új kód)."""
    s = re.sub(r"\(\s*(?:20\d{2}\s+új\s+kód|új|ecs|tt|\d{1,2})\s*\)", "", name, flags=re.I)
    s = re.sub(r"\b(?:ecs|tt)\b", "", s, flags=re.I)
    s = re.sub(r"[\s\-_/]+", " ", s).strip()
    return (_normalize_for_match(category), _normalize_for_match(s), _normalize_for_match(unit))


def _variant_rank(item: dict, order_idx: int) -> tuple:
    """Rank competing variants: newest valid_from wins; tie-break cleaner ECS name, price, order."""
    name_norm = _normalize_for_match(item["name"])
    has_uj_kod = "uj kod" in name_norm or "(uj)" in name_norm
    has_ecs = bool(re.search(r"\becs\b", name_norm))
    return (_valid_from_key(item["valid_from"]), not has_uj_kod, has_ecs, item["price"], order_idx)


def dedupe_variants(items: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Collapse old ECS/TT/packaging variants, keeping only the newest item per canonical product."""
    groups: dict[tuple[str, str, str], list[tuple[int, dict]]] = {}
    for idx, item in enumerate(items):
        key = canonical_variant_key(item["category"], item["name"], item["unit"])
        groups.setdefault(key, []).append((idx, item))

    deduped: list[dict] = []
    id_aliases: dict[str, str] = {}
    for entries in groups.values():
        _, winner = max(entries, key=lambda pair: _variant_rank(pair[1], pair[0]))
        deduped.append(winner)
        for _, candidate in entries:
            if candidate["id"] != winner["id"]:
                id_aliases[candidate["id"]] = winner["id"]
    return deduped, id_aliases


def _parse_workbook_with_aliases(xlsx_bytes: bytes) -> tuple[list[dict], dict[str, str]]:
    dedup_map: dict[str, dict] = {}
    for item in _iter_parsed_rows(xlsx_bytes):
        existing = dedup_map.get(item["id"])
        if existing is None or _valid_from_key(item["valid_from"]) >= _valid_from_key(existing["valid_from"]):
            dedup_map[item["id"]] = item
    return dedupe_variants(list(dedup_map.values()))


def parse_workbook(xlsx_bytes: bytes) -> list[dict]:
    """Parse SZTFH .xlsx bytes into deduplicated normalized product dicts."""
    items, _ = _parse_workbook_with_aliases(xlsx_bytes)
    return items


def parse_history_workbook(
    xlsx_bytes: bytes,
    active_ids: set[str] | None = None,
    id_aliases: dict[str, str] | None = None,
) -> dict[str, list[list]]:
    """Parse cumulative 2017+ SZTFH .xlsx into compact {id: [[date, price], ...]} history."""
    aliases = id_aliases or {}
    by_id: dict[str, dict[str, int]] = {}
    for item in _iter_parsed_rows(xlsx_bytes):
        target_id = aliases.get(item["id"], item["id"])
        if active_ids is not None and target_id not in active_ids:
            continue
        if item["valid_from"]:
            by_id.setdefault(target_id, {})[item["valid_from"]] = item["price"]

    result: dict[str, list[list]] = {}
    for item_id, date_map in by_id.items():
        ordered: list[list] = []
        for dt_str in sorted(date_map):
            pr = date_map[dt_str]
            if not ordered or ordered[-1][1] != pr:
                ordered.append([dt_str, pr])
        if ordered:
            result[item_id] = ordered
    return result


def _core_records(items: list[dict]) -> list[tuple]:
    try:
        return sorted(
            (it["id"], it["name"], it["category"], it["unit"], it["price"], it["valid_from"])
            for it in items
        )
    except (KeyError, TypeError):
        return []


def load_prior_data(output_path: Path) -> dict | None:
    if not output_path.exists():
        return None
    try:
        data = json.loads(output_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.warning("Failed to load prior %s (%s); treating as baseline.", output_path, exc)
        return None


def should_skip_write(prior_data: dict | None, source_file: str, parsed_items: list[dict]) -> bool:
    if not prior_data:
        return False
    meta = prior_data.get("meta")
    prior_items = prior_data.get("items")
    if not isinstance(meta, dict) or not isinstance(prior_items, list):
        return False
    return meta.get("source_file") == source_file and _core_records(prior_items) == _core_records(parsed_items)


def apply_price_diffs(
    parsed_items: list[dict],
    prior_items: list[dict],
    has_prior_catalog: bool,
    today_utc: str,
) -> list[dict]:
    prior_by_id = {
        it["id"]: it for it in prior_items if isinstance(it, dict) and isinstance(it.get("id"), str)
    }
    result: list[dict] = []
    for item in parsed_items:
        prior = prior_by_id.get(item["id"])
        if not has_prior_catalog:
            previous_price = price_diff = changed_at = None
            is_new = False
        elif prior is None:
            previous_price = price_diff = None
            changed_at = today_utc
            is_new = True
        else:
            prior_price = prior.get("price")
            if isinstance(prior_price, int) and not isinstance(prior_price, bool) and prior_price != item["price"]:
                previous_price = prior_price
                price_diff = item["price"] - prior_price
                changed_at = today_utc
            else:
                prev_raw, diff_raw = prior.get("previous_price"), prior.get("price_diff")
                previous_price = prev_raw if isinstance(prev_raw, int) and not isinstance(prev_raw, bool) else None
                price_diff = diff_raw if isinstance(diff_raw, int) and not isinstance(diff_raw, bool) else None
                changed_at = prior.get("changed_at") if isinstance(prior.get("changed_at"), str) else None
            is_new = False

        result.append(
            {
                **item,
                "previous_price": previous_price,
                "price_diff": price_diff,
                "changed_at": changed_at,
                "is_new": is_new,
            }
        )
    return result


def atomic_write_json(output_path: Path, payload: dict, *, compact: bool = False) -> None:
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
            if compact:
                json.dump(payload, tmp_file, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            else:
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


def _update_history_if_available(
    http: object,
    headers: dict[str, str],
    page_html: str,
    page_url: str,
    active_ids: set[str],
    history_output_path: Path | None,
    timeout: float,
    id_aliases: dict[str, str] | None = None,
) -> bool:
    if history_output_path is None:
        return False
    history_url = find_history_xlsx_url(page_html, page_url)
    if not history_url:
        return False
    try:
        hist_resp = http.get(history_url, headers=headers, timeout=timeout)
        hist_resp.raise_for_status()
        history_map = parse_history_workbook(
            hist_resp.content, active_ids=active_ids, id_aliases=id_aliases
        )
        if not history_map:
            return False
        if history_output_path.exists():
            try:
                if json.loads(history_output_path.read_text(encoding="utf-8")) == history_map:
                    return False
            except Exception:
                pass
        atomic_write_json(history_output_path, history_map, compact=True)
        logger.info("Wrote history for %d items to %s", len(history_map), history_output_path)
        return True
    except Exception as exc:
        logger.warning("Could not update price history (%s): %s", history_url, exc)
        return False


def update_prices(
    page_url: str = SZTFH_PAGE_URL,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    timeout: float = DEFAULT_TIMEOUT,
    now: datetime | None = None,
    session: object | None = None,
    history_output_path: Path | None = None,
) -> bool:
    """Fetch SZTFH prices, compute diffs, and write output_path if changed."""
    output_path = Path(output_path)
    resolved_history_path = (
        Path(history_output_path)
        if history_output_path is not None
        else output_path.parent / DEFAULT_HISTORY_OUTPUT_PATH.name
    )
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    http = session or _UrllibSession()

    try:
        page_resp = http.get(page_url, headers=headers, timeout=timeout)
        page_resp.raise_for_status()

        xlsx_url = find_xlsx_url(page_resp.text, page_url)
        source_file = Path(urlparse(xlsx_url).path).name
        xlsx_resp = http.get(xlsx_url, headers=headers, timeout=timeout)
        xlsx_resp.raise_for_status()

        parsed_items, id_aliases = _parse_workbook_with_aliases(xlsx_resp.content)
        if not parsed_items:
            raise ValueError(f"Parsed 0 product items from {xlsx_url}")

        active_ids = {item["id"] for item in parsed_items}
        prior_data = load_prior_data(output_path)
        if should_skip_write(prior_data, source_file, parsed_items):
            if not resolved_history_path.exists():
                _update_history_if_available(
                    http, headers, page_resp.text, page_url, active_ids, resolved_history_path, timeout, id_aliases=id_aliases
                )
            return False

        current_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        prior_items = prior_data.get("items", []) if prior_data and isinstance(prior_data.get("items"), list) else []
        has_prior_catalog = any(isinstance(it, dict) and isinstance(it.get("id"), str) for it in prior_items)

        items = apply_price_diffs(parsed_items, prior_items, has_prior_catalog, current_utc.strftime("%Y-%m-%d"))
        payload = {
            "meta": {
                "last_updated": current_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source_file": source_file,
                "source_url": xlsx_url,
                "page_url": page_url,
                "total_count": len(items),
            },
            "items": items,
        }
        atomic_write_json(output_path, payload)
        _update_history_if_available(
            http, headers, page_resp.text, page_url, active_ids, resolved_history_path, timeout, id_aliases=id_aliases
        )
        return True
    finally:
        if session is None:
            http.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Update tobacco retail prices from official SZTFH publications.")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--page-url", "--url", dest="page_url", default=SZTFH_PAGE_URL)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )
    try:
        update_prices(page_url=args.page_url, output_path=args.output, timeout=args.timeout)
        return 0
    except Exception as exc:
        logger.error("Price update failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
