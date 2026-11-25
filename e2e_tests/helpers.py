from __future__ import annotations

import hashlib
import html as html_lib
import io
import json
import random
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Sequence

import openpyxl
from openpyxl.utils.datetime import to_excel
from playwright.sync_api import Locator, Page

# ---------------------------------------------------------------------------
# Shared Constants & Canonical Schema Helpers
# ---------------------------------------------------------------------------

SZTFH_PAGE_URL = "https://sztfh.hu/tevekenysegek/dohanyugyi-felugyelet/arak-kozzetetele/"
DEFAULT_SOURCE_FILE = (
    "alkalmazando_dohanygyartmany_kiskereskedelmi_eladasi_arak_20261125120001.xlsx"
)
DEFAULT_SOURCE_URL = f"https://sztfh.hu/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"

LS_THEME_KEY = "dohany_theme"
LS_FAVORITES_KEY = "dohany_favorites"
LS_VIEW_KEY = "dohany_view"

DEFAULT_XLSX_COLUMNS: tuple[str, ...] = (
    "Termékregisztrátor megnevezése",
    "NDN cikkszám",
    "EAN kód",
    "Termék típus",
    "Termék megnevezése",
    "Márkanév",
    "Mennyiség",
    "Mennyiségi egység",
    "Kiskereskedelmi eladási ár",
    "Érvényesség kezdete",
)

MINIMAL_XLSX_COLUMNS: tuple[str, ...] = (
    "Termék megnevezése",
    "Termék típus",
    "Mennyiség",
    "Mennyiségi egység",
    "Kiskereskedelmi eladási ár",
    "Érvényesség kezdete",
)


def compute_item_id(name: str, unit: str) -> str:
    """Return sha256(f'{name}|{unit}')[:16] lowercase hex digest."""
    raw = f"{name}|{unit}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Component 1: .xlsx Workbook Builder (openpyxl)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategorySeparator:
    """Represents a standalone category/section header row (no price/unit)."""

    title: str
    col_index: int | None = 1  # 1-based column index; None maps to name column


@dataclass(frozen=True)
class EmptyRow:
    """Represents one or more truly empty rows ([None, ...])."""

    count: int = 1


@dataclass(frozen=True)
class WhitespaceRow:
    """Represents a row containing only whitespace strings."""

    values: tuple[str, ...] = ("   ", "", "\t")


@dataclass(frozen=True)
class RawRow:
    """Represents an arbitrary raw list of cell values written verbatim."""

    cells: Sequence[Any]


_HEADER_TO_FIELD: dict[str, str] = {
    "termék megnevezése": "name",
    "terméknév": "name",
    "megnevezés": "name",
    "név": "name",
    "termék típus": "category",
    "kategória": "category",
    "típus": "category",
    "mennyiség": "quantity",
    "mennyiségi egység": "unit_name",
    "egység": "unit_name",
    "kiszerelés": "unit",
    "kiskereskedelmi eladási ár": "price",
    "kiskereskedelmi ár": "price",
    "eladási ár": "price",
    "ár": "price",
    "érvényesség kezdete": "valid_from",
    "alkalmazás kezdete": "valid_from",
    "hatályos": "valid_from",
    "érvényesség": "valid_from",
    "ndn cikkszám": "ndn_code",
    "cikkszám": "ndn_code",
    "ean kód": "ean",
    "ean": "ean",
    "márkanév": "brand",
    "márka": "brand",
    "termékregisztrátor megnevezése": "registrar",
    "regisztrátor": "registrar",
}


def to_excel_serial(value: date | datetime) -> int:
    """Convert date/datetime to integer Excel 1900 serial date via openpyxl.utils.datetime.to_excel."""
    if isinstance(value, datetime):
        d = value.date()
    elif isinstance(value, date):
        d = value
    else:
        raise TypeError(f"Expected date or datetime, got {type(value)!r}")
    return int(round(to_excel(d)))


def _coerce_date_obj(value: date | datetime | int | float | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        cleaned = value.strip().rstrip(".")
        for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y. %m. %d"):
            try:
                return datetime.strptime(cleaned, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"Unrecognized date string: {value!r}")
    raise TypeError(f"Cannot convert {type(value)!r} to date")


def encode_excel_date(
    value: date | datetime | int | float | str,
    fmt: Literal["raw", "serial", "datetime", "date", "iso", "hu_dot"] = "raw",
) -> Any:
    """Format a date into the requested Excel cell representation."""
    if fmt == "raw":
        return value
    if isinstance(value, (int, float)) and fmt == "serial":
        return int(value)
    d = _coerce_date_obj(value)
    if fmt == "serial":
        return to_excel_serial(d)
    if fmt == "datetime":
        if isinstance(value, datetime):
            return value
        return datetime(d.year, d.month, d.day, 0, 0)
    if fmt == "date":
        return d
    if fmt == "iso":
        return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
    if fmt == "hu_dot":
        return f"{d.year:04d}.{d.month:02d}.{d.day:02d}"
    raise ValueError(f"Invalid date format mode: {fmt!r}")


def _split_unit_if_needed(unit_str: str) -> tuple[Any, str]:
    s = unit_str.strip()
    m = re.match(r"^(\d+(?:[.,]\d+)?)\s+(.+)$", s)
    if not m:
        return None, s
    num_raw, unit_name = m.group(1).replace(",", "."), m.group(2).strip()
    if "." in num_raw:
        val = float(num_raw)
        return (int(val) if val.is_integer() else val), unit_name
    return int(num_raw), unit_name


def _write_preamble(
    ws: Any,
    header_row: int,
    preamble_rows: Sequence[Sequence[Any] | str] | None,
) -> None:
    if preamble_rows is not None:
        if len(preamble_rows) >= header_row:
            raise ValueError(
                f"len(preamble_rows)={len(preamble_rows)} must be < header_row={header_row}"
            )
        for row_idx, row_data in enumerate(preamble_rows, start=1):
            if isinstance(row_data, str):
                ws.cell(row=row_idx, column=1, value=row_data)
            else:
                for col_idx, val in enumerate(row_data, start=1):
                    ws.cell(row=row_idx, column=col_idx, value=val)
    elif header_row > 1:
        ws.cell(
            row=1,
            column=1,
            value="Alkalmazható dohánygyártmány kiskereskedelmi eladási árak (SZTFH)",
        )


def build_xlsx_bytes(
    rows: Sequence[dict[str, Any] | CategorySeparator | EmptyRow | WhitespaceRow | RawRow],
    *,
    columns: Sequence[str] = DEFAULT_XLSX_COLUMNS,
    header_row: int = 2,
    preamble_rows: Sequence[Sequence[Any] | str] | None = None,
    sheet_name: str = "Munka1",
    extra_sheets: dict[str, Sequence[Sequence[Any]]] | None = None,
    header_aliases: dict[str, str] | None = None,
) -> bytes:
    """Build an in-memory .xlsx workbook via openpyxl and return its raw bytes."""
    if header_row < 1:
        raise ValueError(f"header_row must be >= 1, got {header_row}")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name

    _write_preamble(ws, header_row, preamble_rows)

    alias_map = dict(_HEADER_TO_FIELD)
    if header_aliases:
        for k, v in header_aliases.items():
            alias_map[k.strip().lower()] = v

    col_fields: list[tuple[str, str | None]] = []
    name_col_idx = 1
    for idx, col_header in enumerate(columns, start=1):
        field = alias_map.get(col_header.strip().lower())
        col_fields.append((col_header, field))
        if field == "name":
            name_col_idx = idx
        ws.cell(row=header_row, column=idx, value=col_header)

    has_qty_col = any(f == "quantity" for _, f in col_fields)
    has_unit_name_col = any(f == "unit_name" for _, f in col_fields)
    has_combined_unit_col = any(f == "unit" for _, f in col_fields)

    cur_row = header_row + 1
    for entry in rows:
        if isinstance(entry, EmptyRow):
            if entry.count < 0:
                raise ValueError("EmptyRow.count must be >= 0")
            cur_row += entry.count
            continue
        if isinstance(entry, WhitespaceRow):
            for col_idx, val in enumerate(entry.values, start=1):
                ws.cell(row=cur_row, column=col_idx, value=val)
            cur_row += 1
            continue
        if isinstance(entry, CategorySeparator):
            target_col = entry.col_index if entry.col_index is not None else name_col_idx
            ws.cell(row=cur_row, column=target_col, value=entry.title)
            cur_row += 1
            continue
        if isinstance(entry, RawRow):
            for col_idx, val in enumerate(entry.cells, start=1):
                ws.cell(row=cur_row, column=col_idx, value=val)
            cur_row += 1
            continue
        if not isinstance(entry, dict):
            raise TypeError(f"Unsupported row entry type: {type(entry)!r}")

        row_dict = dict(entry)
        if (
            "unit" in row_dict
            and not has_combined_unit_col
            and (has_qty_col or has_unit_name_col)
            and "quantity" not in row_dict
            and "unit_name" not in row_dict
        ):
            qty_val, uname_val = _split_unit_if_needed(str(row_dict["unit"]))
            row_dict["quantity"] = qty_val
            row_dict["unit_name"] = uname_val

        for col_idx, (col_header, field) in enumerate(col_fields, start=1):
            if col_header in row_dict:
                val = row_dict[col_header]
            elif field is not None and field in row_dict:
                val = row_dict[field]
            elif field == "unit" and "quantity" in row_dict and "unit_name" in row_dict:
                val = f"{row_dict['quantity']} {row_dict['unit_name']}".strip()
            else:
                val = None
            ws.cell(row=cur_row, column=col_idx, value=val)
        cur_row += 1

    if extra_sheets:
        for extra_name, extra_rows in extra_sheets.items():
            ews = wb.create_sheet(title=extra_name)
            for r_idx, r_vals in enumerate(extra_rows, start=1):
                for c_idx, val in enumerate(r_vals, start=1):
                    ews.cell(row=r_idx, column=c_idx, value=val)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def write_xlsx(path: Path | str, *args: Any, **kwargs: Any) -> Path:
    """Build .xlsx via build_xlsx_bytes and write to path, creating parent dirs if needed."""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(build_xlsx_bytes(*args, **kwargs))
    return out_path


def build_empty_xlsx_bytes(
    mode: Literal["blank_sheet", "headers_only", "preamble_only"] = "blank_sheet",
    *,
    columns: Sequence[str] = DEFAULT_XLSX_COLUMNS,
    header_row: int = 2,
    sheet_name: str = "Munka1",
) -> bytes:
    """Build valid .xlsx workbooks containing 0 product rows."""
    if mode == "blank_sheet":
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet_name
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
    if mode == "headers_only":
        return build_xlsx_bytes([], columns=columns, header_row=header_row, sheet_name=sheet_name)
    if mode == "preamble_only":
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet_name
        ws.cell(row=1, column=1, value="Alkalmazható dohánygyártmány kiskereskedelmi eladási árak")
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
    raise ValueError(f"Invalid empty xlsx mode: {mode!r}")


def build_corrupt_xlsx_bytes(
    mode: Literal["garbage", "empty_bytes", "truncated_zip", "missing_sheet_xml"] = "garbage",
) -> bytes:
    """Return malformed bytes simulating network corruption or non-xlsx responses."""
    if mode == "garbage":
        return b"not-a-valid-zip-xlsx-payload-corrupted-content"
    if mode == "empty_bytes":
        return b""
    if mode == "truncated_zip":
        valid = build_empty_xlsx_bytes("headers_only")
        return valid[: max(16, len(valid) // 3)]
    if mode == "missing_sheet_xml":
        valid = build_empty_xlsx_bytes("headers_only")
        src_buf = io.BytesIO(valid)
        dst_buf = io.BytesIO()
        with zipfile.ZipFile(src_buf, "r") as zin, zipfile.ZipFile(
            dst_buf, "w", compression=zipfile.ZIP_DEFLATED
        ) as zout:
            for item in zin.infolist():
                if item.filename == "xl/worksheets/sheet1.xml":
                    continue
                zout.writestr(item, zin.read(item.filename))
        return dst_buf.getvalue()
    raise ValueError(f"Invalid corrupt xlsx mode: {mode!r}")


# ---------------------------------------------------------------------------
# Component 2: Mock SZTFH HTML Page Builder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MockSztfhLink:
    href: str
    text: str
    attrs: dict[str, str] | None = None


def _render_anchor(link: MockSztfhLink, base_url: str | None) -> str:
    href = link.href
    if base_url and href.startswith("/"):
        href = f"{base_url.rstrip('/')}{href}"
    attrs_parts = [f'href="{html_lib.escape(href, quote=True)}"']
    if link.attrs:
        for k, v in link.attrs.items():
            attrs_parts.append(f'{html_lib.escape(k, quote=True)}="{html_lib.escape(v, quote=True)}"')
    return f'<a {" ".join(attrs_parts)}>{html_lib.escape(link.text)}</a>'


def build_sztfh_html(
    *,
    alkalmazando_links: Sequence[str | MockSztfhLink] | None = None,
    include_archive_2017: bool = True,
    include_valtozas: bool = True,
    archive_2017_href: str = (
        "/downloads/dohany/arkozzetetel/"
        "2017_januar_1-jet_kovetoen_alkalmazott_dohanygyartmany_kiskereskedelmi_eladasi_arak_listaja_20261125120001.xlsx"
    ),
    valtozas_href: str = (
        "/downloads/dohany/arkozzetetel/"
        "kiskereskedelmi_eladasi_ar_valtozassal_erintett_dohanygyartmanyok_20261125120001.xlsx"
    ),
    extra_links: Sequence[MockSztfhLink] = (),
    links_override: Sequence[MockSztfhLink] | None = None,
    no_xlsx: bool = False,
    base_url: str | None = None,
    wrapper_html: str | None = None,
    title: str = "Árak közzététele | SZTFH",
) -> str:
    """Build a realistic mock SZTFH price-publication HTML page."""
    if links_override is not None:
        links = list(links_override)
    elif no_xlsx:
        links = [
            MockSztfhLink(href="/tevekenysegek/dohanyugyi-felugyelet/", text="Dohányügyi felügyelet"),
            MockSztfhLink(href="/tajekoztato.pdf", text="Tájékoztató PDF"),
            *extra_links,
        ]
    else:
        links = []
        if alkalmazando_links is None:
            links.append(
                MockSztfhLink(
                    href=f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}",
                    text="Alkalmazandó dohánygyártmány kiskereskedelmi eladási árak",
                )
            )
        else:
            for item in alkalmazando_links:
                if isinstance(item, MockSztfhLink):
                    links.append(item)
                else:
                    links.append(
                        MockSztfhLink(
                            href=item,
                            text="Alkalmazandó dohánygyártmány kiskereskedelmi eladási árak",
                        )
                    )
        if include_valtozas:
            links.append(
                MockSztfhLink(
                    href=valtozas_href,
                    text="Kiskereskedelmi eladási ár változással érintett dohánygyártmányok",
                )
            )
        if include_archive_2017:
            links.append(
                MockSztfhLink(
                    href=archive_2017_href,
                    text="2017. január 1-jét követően alkalmazott kiskereskedelmi eladási árak",
                )
            )
        links.extend(extra_links)

    rendered_list = "\n".join(f"<li>{_render_anchor(lnk, base_url)}</li>" for lnk in links)
    links_block = f"<ul>\n{rendered_list}\n</ul>"

    if wrapper_html is not None:
        return wrapper_html.format(links=links_block)

    return (
        "<!doctype html>\n"
        '<html lang="hu">\n'
        f"<head><meta charset=\"utf-8\"><title>{html_lib.escape(title)}</title></head>\n"
        "<body>\n"
        "  <main>\n"
        "    <h1>Dohánygyártmány kiskereskedelmi eladási árak közzététele</h1>\n"
        f"    {links_block}\n"
        "  </main>\n"
        "</body>\n"
        "</html>\n"
    )


# ---------------------------------------------------------------------------
# Component 3: prices.json Catalog Builder & Schema Validator
# ---------------------------------------------------------------------------


def make_catalog_item(
    *,
    name: str,
    category: str,
    unit: str,
    price: int,
    valid_from: str = "2026-11-26",
    previous_price: int | None = None,
    changed_at: str | None = None,
    is_new: bool = False,
    price_diff: int | None = None,
) -> dict[str, Any]:
    """Create a single prices.json item dict with deterministic id = compute_item_id(name, unit)."""
    if price_diff is None and previous_price is not None:
        price_diff = price - previous_price
    return {
        "id": compute_item_id(name, unit),
        "name": name,
        "category": category,
        "unit": unit,
        "price": price,
        "valid_from": valid_from,
        "previous_price": previous_price,
        "price_diff": price_diff,
        "changed_at": changed_at,
        "is_new": is_new,
    }


def _realistic_item_seed() -> list[dict[str, Any]]:
    # 25 realistic Hungarian tobacco items; first 20 already cover all 5+ categories,
    # ő/ű accents, long multi-word names, increases, decreases, new items, and unchanged items.
    return [
        make_catalog_item(
            name="Marlboro Red 20 szálas",
            category="Cigaretta",
            unit="20 szál",
            price=2450,
            valid_from="2026-11-26",
            previous_price=2350,
            changed_at="2026-11-26",
            is_new=False,
        ),
        make_catalog_item(
            name="Sopianae Multifilter Piros 20",
            category="Cigaretta",
            unit="20 szál",
            price=2150,
            valid_from="2026-11-26",
            previous_price=2200,
            changed_at="2026-11-26",
            is_new=False,
        ),
        make_catalog_item(
            name="Dunhill Fine Cut Blue 20",
            category="Cigaretta",
            unit="20 szál",
            price=2550,
            valid_from="2026-10-01",
        ),
        make_catalog_item(
            name="TEREA Amber",
            category="Hevítésre szánt dohánytermék (zárjegyes)",
            unit="20 szál",
            price=2390,
            valid_from="2026-11-26",
            previous_price=2290,
            changed_at="2026-11-26",
            is_new=False,
        ),
        make_catalog_item(
            name="TEREA Turquoise",
            category="Hevítésre szánt dohánytermék (zárjegyes)",
            unit="20 szál",
            price=2390,
            valid_from="2026-10-01",
        ),
        make_catalog_item(
            name="HEETS Bronze Selection",
            category="Hevítésre szánt dohánytermék (zárjegyes)",
            unit="20 szál",
            price=2250,
            valid_from="2026-10-01",
        ),
        make_catalog_item(
            name="Golden Virginia Original 30 g",
            category="Finomra vágott fogyasztási dohány",
            unit="30 g",
            price=4350,
            valid_from="2026-11-26",
            previous_price=4200,
            changed_at="2026-11-26",
            is_new=False,
        ),
        make_catalog_item(
            name="Sopianae Kék Vágott Dohány 30 g",
            category="Finomra vágott fogyasztási dohány",
            unit="30 g",
            price=3690,
            valid_from="2026-10-01",
        ),
        make_catalog_item(
            name="Moods Filter 10",
            category="Szivarka",
            unit="10 szál",
            price=2190,
            valid_from="2026-10-01",
        ),
        make_catalog_item(
            name="Café Crème Original 10",
            category="Szivarka",
            unit="10 szál",
            price=1990,
            valid_from="2026-11-26",
            previous_price=2040,
            changed_at="2026-11-26",
            is_new=False,
        ),
        make_catalog_item(
            name="Toscanello Rosso Caffè 5",
            category="Szivarka",
            unit="5 szál",
            price=2590,
            valid_from="2026-10-01",
        ),
        make_catalog_item(
            name="VELO Freezing Peppermint 14 g",
            category="Egyéb nikotinos termékek (zárjegyes)",
            unit="14 g",
            price=1890,
            valid_from="2026-11-26",
            changed_at="2026-11-26",
            is_new=True,
        ),
        make_catalog_item(
            name="Őszi Szűz Fűszeres Pipadohány 50 g Extra Hosszú Kiszerelésben",
            category="Egyéb fogyasztási dohány",
            unit="50 g",
            price=5490,
            valid_from="2026-11-26",
            changed_at="2026-11-26",
            is_new=True,
        ),
        make_catalog_item(
            name="Hűvös Kék Áfonya Hevítőrúd 20 szálas",
            category="Hevítésre szánt dohánytermék (zárjegyes)",
            unit="20 szál",
            price=2320,
            valid_from="2026-11-26",
            previous_price=2370,
            changed_at="2026-11-26",
            is_new=False,
        ),
        make_catalog_item(
            name="Cohiba Siglo II Tubos 3 szálas Prémium Kézzel Sodort Szivar",
            category="Szivar",
            unit="3 szál",
            price=18500,
            valid_from="2026-10-01",
        ),
        make_catalog_item(
            name="Borkum Riff Cherry Ruby 50 g",
            category="Egyéb fogyasztási dohány",
            unit="50 g",
            price=4890,
            valid_from="2026-10-01",
        ),
        make_catalog_item(
            name="Camel Yellow 20 szálas",
            category="Cigaretta",
            unit="20 szál",
            price=2320,
            valid_from="2026-10-01",
        ),
        make_catalog_item(
            name="Lucky Strike Original Red 20",
            category="Cigaretta",
            unit="20 szál",
            price=2290,
            valid_from="2026-11-26",
            previous_price=2240,
            changed_at="2026-11-26",
            is_new=False,
        ),
        make_catalog_item(
            name="Pall Mall Blue 100s",
            category="Cigaretta",
            unit="20 szál",
            price=2220,
            valid_from="2026-10-01",
        ),
        make_catalog_item(
            name="ZYN Cool Mint Mini Dry 8 g",
            category="Egyéb nikotinos termékek (zárjegyes)",
            unit="8 g",
            price=1950,
            valid_from="2026-10-01",
        ),
        make_catalog_item(
            name="Winston Classic Red 20",
            category="Cigaretta",
            unit="20 szál",
            price=2260,
            valid_from="2026-10-01",
        ),
        make_catalog_item(
            name="NEO Terracotta Tobacco 20",
            category="Hevítésre szánt dohánytermék (zárjegyes)",
            unit="20 szál",
            price=2150,
            valid_from="2026-11-26",
            previous_price=2100,
            changed_at="2026-11-26",
            is_new=False,
        ),
        make_catalog_item(
            name="Pueblo Classic Fine Cut 30 g",
            category="Finomra vágott fogyasztási dohány",
            unit="30 g",
            price=3950,
            valid_from="2026-10-01",
        ),
        make_catalog_item(
            name="Villiger Mini Espresso Flavour 20",
            category="Szivarka",
            unit="20 szál",
            price=2790,
            valid_from="2026-10-01",
        ),
        make_catalog_item(
            name="Romeo y Julieta No. 2 Tubos",
            category="Szivar",
            unit="1 szál",
            price=4990,
            valid_from="2026-10-01",
        ),
    ]


def build_prices_json(
    items: Sequence[dict[str, Any]],
    *,
    last_updated: str = "2026-11-25T22:00:00Z",
    source_file: str = DEFAULT_SOURCE_FILE,
    source_url: str = DEFAULT_SOURCE_URL,
    page_url: str = SZTFH_PAGE_URL,
    total_count: int | None = None,
) -> dict[str, Any]:
    """Assemble top-level {'meta': ..., 'items': list(items)} dict."""
    items_list = list(items)
    return {
        "meta": {
            "last_updated": last_updated,
            "source_file": source_file,
            "source_url": source_url,
            "page_url": page_url,
            "total_count": len(items_list) if total_count is None else total_count,
        },
        "items": items_list,
    }


def build_realistic_catalog(
    *,
    count: int = 22,
    last_updated: str = "2026-11-25T22:00:00Z",
    source_file: str = DEFAULT_SOURCE_FILE,
    source_url: str = DEFAULT_SOURCE_URL,
    page_url: str = SZTFH_PAGE_URL,
) -> dict[str, Any]:
    """Build a 20-25 item (default 22) realistic Hungarian tobacco catalog."""
    if not (20 <= count <= 25):
        raise ValueError(f"Realistic catalog count must be between 20 and 25, got {count}")
    items = _realistic_item_seed()[:count]
    return build_prices_json(
        items,
        last_updated=last_updated,
        source_file=source_file,
        source_url=source_url,
        page_url=page_url,
    )


_LARGE_CATEGORIES: tuple[tuple[str, str, int, int], ...] = (
    ("Cigaretta", "20 szál", 1950, 2950),
    ("Hevítésre szánt dohánytermék (zárjegyes)", "20 szál", 2100, 2600),
    ("Finomra vágott fogyasztási dohány", "30 g", 3400, 4900),
    ("Szivarka", "10 szál", 1800, 3200),
    ("Szivar", "1 szál", 2500, 18500),
    ("Egyéb fogyasztási dohány", "50 g", 4200, 6800),
    ("Egyéb nikotinos termékek (zárjegyes)", "14 g", 1750, 2490),
)

_LARGE_ACCENT_VARIANTS: tuple[str, ...] = (
    "Arany Őszi",
    "Kék Hűvös",
    "Piros Fűszeres",
    "Sötét Tölgy",
    "Ezüst Füst",
    "Üde Áfonya",
)


def build_large_catalog(
    count: int = 4610,
    *,
    seed: int = 42,
    include_realistic_base: bool = True,
    changed_ratio: float = 0.10,
    new_ratio: float = 0.03,
    last_updated: str = "2026-11-25T22:00:00Z",
    source_file: str = DEFAULT_SOURCE_FILE,
    source_url: str = DEFAULT_SOURCE_URL,
    page_url: str = SZTFH_PAGE_URL,
) -> dict[str, Any]:
    """Deterministic parameterized generator for 4,600+ items (default 4,610)."""
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")

    rng = random.Random(seed)
    items: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()

    if include_realistic_base and count > 0:
        for base_item in _realistic_item_seed():
            if len(items) >= count:
                break
            key = (base_item["name"], base_item["unit"])
            if key not in seen_keys:
                seen_keys.add(key)
                items.append(dict(base_item))

    idx = 1
    while len(items) < count:
        cat, unit, p_min, p_max = _LARGE_CATEGORIES[idx % len(_LARGE_CATEGORIES)]
        accent = _LARGE_ACCENT_VARIANTS[idx % len(_LARGE_ACCENT_VARIANTS)]
        name = f"DohányMárka {accent} #{idx:04d}"
        key = (name, unit)
        idx += 1
        if key in seen_keys:
            continue
        seen_keys.add(key)

        price = rng.randrange(p_min, p_max + 10, 10)
        roll = rng.random()
        previous_price: int | None = None
        changed_at: str | None = None
        is_new = False
        valid_from = "2026-10-01"

        if roll < new_ratio:
            is_new = True
            valid_from = "2026-11-26"
            changed_at = "2026-11-26"
        elif roll < new_ratio + changed_ratio:
            delta = rng.choice([-150, -100, -50, 50, 100, 150])
            previous_price = max(100, price - delta)
            if previous_price == price:
                previous_price += 50
            valid_from = "2026-11-26"
            changed_at = "2026-11-26"

        items.append(
            make_catalog_item(
                name=name,
                category=cat,
                unit=unit,
                price=price,
                valid_from=valid_from,
                previous_price=previous_price,
                changed_at=changed_at,
                is_new=is_new,
            )
        )

    return build_prices_json(
        items,
        last_updated=last_updated,
        source_file=source_file,
        source_url=source_url,
        page_url=page_url,
    )


def write_prices_json(path: Path | str, catalog: dict[str, Any]) -> Path:
    """Write catalog as UTF-8 JSON (ensure_ascii=False, indent=2) to path."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_prices_schema(data: dict[str, Any], *, require_page_url: bool = False) -> None:
    """Assert all PROJECT.md schema & data invariants."""
    assert isinstance(data, dict), f"Top-level JSON must be dict, got {type(data)!r}"
    assert "meta" in data and isinstance(data["meta"], dict), "Missing or invalid 'meta' dict"
    assert "items" in data and isinstance(data["items"], list), "Missing or invalid 'items' list"

    meta = data["meta"]
    items = data["items"]
    for key in ("last_updated", "source_file", "source_url"):
        assert key in meta and isinstance(meta[key], str) and meta[key].strip(), (
            f"meta.{key} must be non-empty str, got {meta.get(key)!r}"
        )
    if require_page_url:
        assert (
            "page_url" in meta
            and isinstance(meta["page_url"], str)
            and meta["page_url"].startswith("https://")
        ), f"meta.page_url invalid: {meta.get('page_url')!r}"

    assert isinstance(meta.get("total_count"), int) and not isinstance(
        meta.get("total_count"), bool
    ), f"meta.total_count must be int, got {meta.get('total_count')!r}"
    assert meta["total_count"] == len(items), (
        f"meta.total_count ({meta['total_count']}) != len(items) ({len(items)})"
    )

    seen_ids: set[str] = set()
    for idx, item in enumerate(items):
        assert isinstance(item, dict), f"items[{idx}] must be dict"
        for req_key in (
            "id",
            "name",
            "category",
            "unit",
            "price",
            "valid_from",
            "previous_price",
            "price_diff",
            "changed_at",
            "is_new",
        ):
            assert req_key in item, f"items[{idx}] missing key {req_key!r}"

        item_id = item["id"]
        assert isinstance(item_id, str) and _HEX16_RE.match(item_id), (
            f"items[{idx}].id invalid hex16: {item_id!r}"
        )
        assert item_id not in seen_ids, f"Duplicate item id at items[{idx}]: {item_id!r}"
        seen_ids.add(item_id)

        assert isinstance(item["name"], str) and item["name"].strip(), (
            f"items[{idx}].name invalid: {item['name']!r}"
        )
        assert isinstance(item["category"], str) and item["category"].strip(), (
            f"items[{idx}].category invalid: {item['category']!r}"
        )
        assert isinstance(item["unit"], str), f"items[{idx}].unit must be str"

        expected_id = compute_item_id(item["name"], item["unit"])
        assert item_id == expected_id, (
            f"items[{idx}].id ({item_id}) != sha256(name|unit)[:16] ({expected_id})"
        )

        price = item["price"]
        assert isinstance(price, int) and not isinstance(price, bool) and price > 0, (
            f"items[{idx}].price must be positive int, got {price!r}"
        )

        vf = item["valid_from"]
        assert isinstance(vf, str) and (vf == "" or _DATE_RE.match(vf)), (
            f"items[{idx}].valid_from must be YYYY-MM-DD or '', got {vf!r}"
        )

        prev = item["previous_price"]
        diff = item["price_diff"]
        if prev is None:
            assert diff is None, f"items[{idx}].price_diff must be None when previous_price is None"
        else:
            assert isinstance(prev, int) and not isinstance(prev, bool) and prev > 0, (
                f"items[{idx}].previous_price invalid: {prev!r}"
            )
            assert isinstance(diff, int) and not isinstance(diff, bool), (
                f"items[{idx}].price_diff must be int, got {diff!r}"
            )
            assert diff == price - prev, (
                f"items[{idx}].price_diff ({diff}) != price ({price}) - previous_price ({prev})"
            )

        changed_at = item["changed_at"]
        assert changed_at is None or (
            isinstance(changed_at, str) and _DATE_RE.match(changed_at)
        ), f"items[{idx}].changed_at invalid: {changed_at!r}"

        assert isinstance(item["is_new"], bool), f"items[{idx}].is_new must be bool"


# ---------------------------------------------------------------------------
# Component 4: Shared Semantic Playwright UI Helpers (E2 & E3)
# ---------------------------------------------------------------------------


def get_search_input(page: Page) -> Locator:
    """Locate search input via role='searchbox' or placeholder/label matching 'Keresés'."""
    by_role = page.get_by_role("searchbox")
    by_placeholder = page.get_by_placeholder(re.compile(r"Keres[eé]s", re.I))
    by_label = page.get_by_label(re.compile(r"Keres[eé]s", re.I))
    return by_role.or_(by_placeholder).or_(by_label).first


def get_clear_search_button(page: Page) -> Locator:
    """Locate '×' / 'Keresés törlése' clear button (anchored regex excluding 'Szűrők és keresés törlése')."""
    name_pat = re.compile(
        r"^\s*(×|✕|✖|x|keres[eé]s\s+t[oö]rl[eé]se|t[oö]rl[eé]s)\s*$", re.I
    )
    return page.get_by_role("button", name=name_pat).first


def get_reset_filters_button(page: Page) -> Locator:
    """Locate empty-state 'Szűrők és keresés törlése' reset button."""
    return page.get_by_role(
        "button", name=re.compile(r"Sz[uű]r[oő]k\s+[eé]s\s+keres[eé]s\s+t[oö]rl[eé]se", re.I)
    ).first


def search_products(page: Page, query: str, *, wait_ms: int = 300) -> None:
    """Fill search input and wait past ~150ms debounce for results to settle."""
    inp = get_search_input(page)
    inp.fill(query)
    if wait_ms > 0:
        page.wait_for_timeout(wait_ms)


def clear_search(page: Page, *, wait_ms: int = 200) -> None:
    """Click the '×' clear button and wait briefly."""
    get_clear_search_button(page).click()
    if wait_ms > 0:
        page.wait_for_timeout(wait_ms)


def get_category_chip(page: Page, category_name: str) -> Locator:
    """Locate category chip/tab matching '^<category_name>(\\s*[\\(\\[]?\\d+[\\)\\]]?)?$'."""
    escaped = re.escape(category_name)
    pat = re.compile(rf"^\s*{escaped}(?:\s*[\(\[]?\d+[\)\]]?)?\s*$", re.I)
    by_btn = page.get_by_role("button", name=pat)
    by_tab = page.get_by_role("tab", name=pat)
    by_radio = page.get_by_role("radio", name=pat)
    return by_btn.or_(by_tab).or_(by_radio).first


def select_category(page: Page, category_name: str) -> None:
    """Click the category chip (e.g. 'Összes', 'Cigaretta')."""
    get_category_chip(page, category_name).click()


def get_price_change_toggle(page: Page) -> Locator:
    """Locate 'Csak árváltozások' control across checkbox, switch, button, or label roles."""
    pat = re.compile(r"Csak\s+[aá]rv[aá]ltoz[aá]sok", re.I)
    return (
        page.get_by_role("checkbox", name=pat)
        .or_(page.get_by_role("switch", name=pat))
        .or_(page.get_by_role("button", name=pat))
        .or_(page.get_by_label(pat))
        .first
    )


def is_control_active(locator: Locator) -> bool:
    """Return True if checkbox/switch/button is active."""
    tag = locator.evaluate("el => el.tagName.toLowerCase()")
    if tag == "input":
        input_type = (locator.get_attribute("type") or "").lower()
        if input_type in ("checkbox", "radio"):
            return locator.is_checked()
    aria_checked = locator.get_attribute("aria-checked")
    if aria_checked is not None:
        return aria_checked.lower() == "true"
    aria_pressed = locator.get_attribute("aria-pressed")
    if aria_pressed is not None:
        return aria_pressed.lower() == "true"
    return False


def set_price_change_filter(page: Page, enabled: bool) -> None:
    """Idempotently set 'Csak árváltozások' filter to enabled (True/False)."""
    ctrl = get_price_change_toggle(page)
    if is_control_active(ctrl) != enabled:
        ctrl.click()


def get_sort_select(page: Page) -> Locator:
    """Locate sort <select> via 'Rendezés' label/aria-label or containing option 'Legutóbb változott'."""
    by_label = page.get_by_label(re.compile(r"Rendez[eé]s", re.I))
    by_role = page.get_by_role("combobox", name=re.compile(r"Rendez[eé]s", re.I))
    by_option = page.locator("select").filter(
        has=page.locator("option", has_text=re.compile(r"Legut[oó]bb\s+v[aá]ltozott", re.I))
    )
    return by_label.or_(by_role).or_(by_option).first


_SORT_LABEL_PATTERNS: dict[str, re.Pattern[str]] = {
    "name-asc": re.compile(r"(?:n[eé]v.*A\s*[-–]\s*Z|A\s*[-–]\s*Z)", re.I),
    "name-desc": re.compile(r"(?:n[eé]v.*Z\s*[-–]\s*A|Z\s*[-–]\s*A)", re.I),
    "price-asc": re.compile(r"[aá]r.*n[oö]vekv[oő]", re.I),
    "price-desc": re.compile(r"[aá]r.*cs[oö]kken[oő]", re.I),
    "changed-desc": re.compile(r"legut[oó]bb\s+v[aá]ltozott", re.I),
}


def select_sort(
    page: Page,
    mode: Literal["name-asc", "name-desc", "price-asc", "price-desc", "changed-desc"],
) -> None:
    """Select sort option matching Hungarian label for mode."""
    if mode not in _SORT_LABEL_PATTERNS:
        raise ValueError(f"Unknown sort mode: {mode!r}")
    sel = get_sort_select(page)
    pattern = _SORT_LABEL_PATTERNS[mode]
    opt_value = sel.evaluate(
        """(el, patSource) => {
            const rx = new RegExp(patSource, 'i');
            for (const opt of el.options) {
                if (rx.test(opt.textContent || '') || rx.test(opt.value || '')) {
                    return opt.value;
                }
            }
            return null;
        }""",
        pattern.pattern,
    )
    if opt_value is None:
        raise AssertionError(f"No sort <option> found matching {mode!r} ({pattern.pattern})")
    sel.select_option(opt_value)


def get_favorites_tab(page: Page) -> Locator:
    """Locate 'Kedvencek' filter button/tab (tolerating optional count badge)."""
    pat = re.compile(r"^\s*(?:★\s*)?Kedvencek(?:\s*[\(\[]?\d+[\)\]]?)?\s*$", re.I)
    return (
        page.get_by_role("tab", name=pat)
        .or_(page.get_by_role("button", name=pat))
        .or_(page.get_by_role("checkbox", name=pat))
        .first
    )


def get_favorites_count_from_tab(page: Page) -> int | None:
    """Extract integer favorite count displayed in the 'Kedvencek' button/tab text, if present."""
    text = get_favorites_tab(page).inner_text()
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def get_product_container(page: Page, product_name: str) -> Locator:
    """Locate visible product card (<article>/<li>/card) or table row (<tr>) containing exact product_name."""
    main_or_body = page.locator("main").or_(page.locator("body")).first
    star_btn_filter = page.locator(
        "button[aria-pressed]:not([role='tab']), "
        "button[aria-label*='kedvenc' i]:not([role='tab']), "
        "button[title*='kedvenc' i]:not([role='tab']), "
        "button:has-text('★'):not([role='tab']), "
        "button:has-text('☆'):not([role='tab'])"
    )
    rows = main_or_body.locator("table tbody tr").filter(has_text=product_name)
    semantic_cards = (
        main_or_body.locator("article, li, [data-product-id], [data-id], .product-card, .card")
        .filter(has_text=product_name)
        .filter(has=star_btn_filter)
    )
    preferred = rows.or_(semantic_cards)
    if preferred.count() > 0:
        return preferred.first
    return (
        main_or_body.locator("div").filter(has_text=product_name).filter(has=star_btn_filter).last
    )


def get_product_star_button(container: Locator) -> Locator:
    """Locate per-item star toggle button inside container."""
    by_pressed = container.locator("button[aria-pressed]")
    by_name = container.get_by_role("button", name=re.compile(r"(kedvenc|★|☆)", re.I))
    return by_pressed.or_(by_name).first


def toggle_favorite(page: Page, product_name: str) -> bool:
    """Click star button for product_name and return new active boolean state."""
    container = get_product_container(page, product_name)
    btn = get_product_star_button(container)
    btn.click()
    return is_control_active(btn)


def get_stored_favorites(page: Page) -> list[str]:
    """Read and parse localStorage['dohany_favorites'] (returns [] if missing/null)."""
    raw = page.evaluate("key => window.localStorage.getItem(key)", LS_FAVORITES_KEY)
    if not raw:
        return []
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed]


def set_stored_favorites(page: Page, ids: Sequence[str]) -> None:
    """Seed localStorage['dohany_favorites'] with a JSON list of item IDs."""
    payload = json.dumps(list(ids), ensure_ascii=False)
    page.evaluate(
        "([key, val]) => window.localStorage.setItem(key, val)",
        [LS_FAVORITES_KEY, payload],
    )


def get_theme_toggle(page: Page) -> Locator:
    """Locate dark/light theme toggle button/switch via accessible name."""
    pat = re.compile(r"(t[eé]ma|vil[aá]gos|s[oö]t[eé]t|m[oó]d)", re.I)
    return (
        page.get_by_role("button", name=pat)
        .or_(page.get_by_role("switch", name=pat))
        .or_(page.get_by_role("checkbox", name=pat))
        .first
    )


def get_current_theme(page: Page) -> str:
    """Return document.documentElement.getAttribute('data-theme') or body attr or 'dark'."""
    theme = page.evaluate(
        "() => document.documentElement.getAttribute('data-theme') || document.body.getAttribute('data-theme') || 'dark'"
    )
    return str(theme)


def get_stored_theme(page: Page) -> str | None:
    """Return localStorage['dohany_theme']."""
    val = page.evaluate("key => window.localStorage.getItem(key)", LS_THEME_KEY)
    return None if val is None else str(val)


def get_view_toggle_button(
    page: Page, target_view: Literal["grid", "table"] | None = None
) -> Locator:
    """Locate desktop grid/table view toggle button(s) by accessible name."""
    if target_view == "grid":
        pat = re.compile(r"(k[aá]rtya|r[aá]cs|grid)", re.I)
    elif target_view == "table":
        pat = re.compile(r"(t[aá]bl[aá]zat|lista|table)", re.I)
    else:
        pat = re.compile(r"(k[aá]rtya|r[aá]cs|t[aá]bl[aá]zat|lista|n[eé]zet)", re.I)
    return page.get_by_role("button", name=pat).first


def detect_active_view(page: Page) -> Literal["grid", "table"]:
    """Detect whether visible <table> with <tbody> tr rows is shown ('table') or card layout ('grid')."""
    is_table = page.evaluate(
        """() => {
            const tables = Array.from(document.querySelectorAll('table'));
            for (const tbl of tables) {
                const style = window.getComputedStyle(tbl);
                const rect = tbl.getBoundingClientRect();
                if (style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0) {
                    if (tbl.querySelectorAll('tbody tr').length > 0) return true;
                }
            }
            return false;
        }"""
    )
    return "table" if is_table else "grid"


def set_view_mode(page: Page, mode: Literal["grid", "table"]) -> None:
    """Switch desktop view to 'grid' or 'table' if not already active."""
    if detect_active_view(page) == mode:
        return
    specific = get_view_toggle_button(page, mode)
    if specific.count() > 0 and specific.is_visible():
        specific.click()
    else:
        get_view_toggle_button(page, None).click()


def get_stored_view(page: Page) -> str | None:
    """Return localStorage['dohany_view']."""
    val = page.evaluate("key => window.localStorage.getItem(key)", LS_VIEW_KEY)
    return None if val is None else str(val)


def get_result_counter_locator(page: Page) -> Locator:
    """Locate 'X / Y termék' result counter text."""
    return page.get_by_text(
        re.compile(r"\d[\d\s\u00a0\u202f]*\s*/\s*\d[\d\s\u00a0\u202f]*\s*term[eé]k", re.I)
    ).first


def parse_result_counter(page: Page) -> tuple[int, int]:
    """Parse 'X / Y termék' (stripping spaces, \\u00a0, \\u202f) -> (shown_count, total_count)."""
    txt = get_result_counter_locator(page).inner_text()
    m = re.search(
        r"([\d\s\u00a0\u202f]+)\s*/\s*([\d\s\u00a0\u202f]+)\s*term[eé]k",
        txt,
        re.I,
    )
    if not m:
        raise AssertionError(f"Could not parse result counter from text: {txt!r}")
    shown = int(re.sub(r"[\s\u00a0\u202f]+", "", m.group(1)))
    total = int(re.sub(r"[\s\u00a0\u202f]+", "", m.group(2)))
    return shown, total


def get_load_more_button(page: Page) -> Locator:
    """Locate 'Továbbiak betöltése' pagination button."""
    return page.get_by_role("button", name=re.compile(r"Tov[aá]bbiak\s+bet[oö]lt[eé]se", re.I)).first


def count_rendered_products(page: Page) -> int:
    """Count currently rendered product items."""
    return int(
        page.evaluate(
            """() => {
                const isVis = (el) => {
                    const s = window.getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
                };
                const isFavTab = (btn) => {
                    if (btn.getAttribute('role') === 'tab') return true;
                    const txt = (btn.textContent || '').trim();
                    return /^(?:★\\s*)?kedvencek(?:\\s*[\\(\\[]?\\d+[\\)\\]]?)?$/i.test(txt);
                };
                const tables = Array.from(document.querySelectorAll('table')).filter(isVis);
                for (const tbl of tables) {
                    const rows = Array.from(tbl.querySelectorAll('tbody tr')).filter(isVis);
                    if (rows.length > 0) return rows.length;
                }
                const root = document.querySelector('main') || document.body;
                const stars = Array.from(root.querySelectorAll('button')).filter(btn => {
                    if (!isVis(btn) || isFavTab(btn)) return false;
                    const label = (btn.getAttribute('aria-label') || '') + ' ' + (btn.textContent || '');
                    return btn.hasAttribute('aria-pressed') || /kedvenc|★|☆/i.test(label);
                });
                return stars.length;
            }"""
        )
    )


def get_mark_texts(page: Page, container: Locator | None = None) -> list[str]:
    """Return inner_text() of all <mark> elements on page or inside container."""
    scope = container if container is not None else page
    marks = scope.locator("mark")
    return [marks.nth(i).inner_text() for i in range(marks.count())]


def assert_product_highlighted(
    page: Page,
    expected_name: str,
    expected_mark_substrings: Sequence[str],
) -> None:
    """Assert rendered product container shows full expected_name and <mark> elements matching expected_mark_substrings."""
    container = get_product_container(page, expected_name)
    full_text = container.inner_text()
    assert expected_name in full_text, (
        f"Expected full product name {expected_name!r} inside container text {full_text!r}"
    )
    marks = get_mark_texts(page, container)
    assert marks, f"Expected at least one <mark> element for {expected_name!r}, found none"
    lowered_marks = [m.lower() for m in marks]
    for sub in expected_mark_substrings:
        assert any(sub.lower() in m for m in lowered_marks), (
            f"Expected <mark> matching {sub!r} in {marks!r} for product {expected_name!r}"
        )


def get_sztfh_source_link(page: Page) -> Locator:
    """Locate official SZTFH link (a[href*='sztfh.hu'])."""
    return page.locator("a[href*='sztfh.hu']").first


def get_retry_button(page: Page) -> Locator:
    """Locate fetch-error retry button ('Újrapróbálás' / 'Újra' / 'Frissítés')."""
    return page.get_by_role(
        "button", name=re.compile(r"([ÚU]jrapr[oó]b[aá]l[aá]s|[ÚU]jra|Friss[ií]t[eé]s)", re.I)
    ).first


def check_horizontal_overflow(page: Page) -> dict[str, Any]:
    """Return {'scroll_width': int, 'client_width': int, 'inner_width': int, 'has_overflow': bool}."""
    metrics = page.evaluate(
        """() => ({
            scroll_width: document.documentElement.scrollWidth,
            client_width: document.documentElement.clientWidth,
            inner_width: window.innerWidth,
        })"""
    )
    metrics["has_overflow"] = metrics["scroll_width"] > metrics["inner_width"]
    return metrics


def assert_no_horizontal_overflow(page: Page) -> None:
    """Assert documentElement.scrollWidth <= window.innerWidth."""
    metrics = check_horizontal_overflow(page)
    assert not metrics["has_overflow"], f"Horizontal overflow detected: {metrics}"


def check_touch_targets(
    page: Page,
    *,
    min_size: float = 44.0,
    include_links: bool = False,
) -> list[dict[str, Any]]:
    """Audit visible interactive controls and return any with width < min_size or height < min_size."""
    selector = (
        "button, input, select, [role='button'], [role='tab'], [role='switch'], [role='checkbox']"
    )
    if include_links:
        selector += ", a[href]"
    violations = page.evaluate(
        """([sel, minSz]) => {
            const out = [];
            for (const el of document.querySelectorAll(sel)) {
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) continue;
                if (rect.width + 0.01 < minSz || rect.height + 0.01 < minSz) {
                    out.push({
                        tag: el.tagName.toLowerCase(),
                        text: (el.innerText || el.value || '').trim().slice(0, 60),
                        ariaLabel: el.getAttribute('aria-label'),
                        width: Math.round(rect.width * 100) / 100,
                        height: Math.round(rect.height * 100) / 100,
                    });
                }
            }
            return out;
        }""",
        [selector, min_size],
    )
    return list(violations)


def assert_min_touch_targets(
    page: Page,
    *,
    min_size: float = 44.0,
    include_links: bool = False,
) -> None:
    """Assert check_touch_targets(...) is empty, with readable diagnostics on failure."""
    violations = check_touch_targets(page, min_size=min_size, include_links=include_links)
    assert not violations, f"Touch targets smaller than {min_size}px: {violations}"


def get_max_content_width(page: Page) -> float | None:
    """Measure bounding box width of <main> (or primary container) to verify <= 1280px on >1024px desktop."""
    width = page.evaluate(
        """() => {
            const el = document.querySelector('main') || document.querySelector('.container, #app');
            if (!el) return null;
            return el.getBoundingClientRect().width;
        }"""
    )
    return float(width) if width is not None else None


def get_grid_column_count(page: Page) -> int:
    """Geometrically count distinct horizontal column x-positions of visible first-row product cards."""
    return int(
        page.evaluate(
            """() => {
                const isVis = (el) => {
                    const s = window.getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
                };
                const isFavTab = (btn) => {
                    if (btn.getAttribute('role') === 'tab') return true;
                    const txt = (btn.textContent || '').trim();
                    return /^(?:★\\s*)?kedvencek(?:\\s*[\\(\\[]?\\d+[\\)\\]]?)?$/i.test(txt);
                };
                const root = document.querySelector('main') || document.body;
                const stars = Array.from(root.querySelectorAll('button')).filter(btn => {
                    if (!isVis(btn) || isFavTab(btn)) return false;
                    const label = (btn.getAttribute('aria-label') || '') + ' ' + (btn.textContent || '');
                    return btn.hasAttribute('aria-pressed') || /kedvenc|★|☆/i.test(label);
                });
                const cards = [];
                for (const btn of stars) {
                    const card = btn.closest('article, li, [data-product-id], [data-id], .card, .product-card') || btn.parentElement;
                    if (card && !cards.includes(card)) cards.push(card);
                }
                if (cards.length === 0) return 0;
                const rects = cards.map(c => c.getBoundingClientRect());
                const minTop = Math.min(...rects.map(r => r.top));
                const firstRowXs = [];
                for (const r of rects) {
                    if (Math.abs(r.top - minTop) <= 12) {
                        const x = Math.round(r.left);
                        if (!firstRowXs.some(existing => Math.abs(existing - x) <= 8)) {
                            firstRowXs.push(x);
                        }
                    }
                }
                return firstRowXs.length;
            }"""
        )
    )
