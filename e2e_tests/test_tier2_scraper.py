from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from helpers import (
    DEFAULT_SOURCE_FILE,
    DEFAULT_XLSX_COLUMNS,
    MINIMAL_XLSX_COLUMNS,
    CategorySeparator,
    EmptyRow,
    MockSztfhLink,
    RawRow,
    WhitespaceRow,
    build_corrupt_xlsx_bytes,
    build_large_catalog,
    build_prices_json,
    build_realistic_catalog,
    build_sztfh_html,
    build_xlsx_bytes,
    compute_item_id,
    make_catalog_item,
    validate_prices_schema,
)


def _row(
    name: str = "Marlboro Red 20 szálas",
    category: str | None = "Cigaretta",
    quantity: Any = 20,
    unit_name: Any = "szál",
    price: Any = 2350,
    valid_from: Any = "2026-11-26",
) -> dict[str, Any]:
    return {
        "registrar": "Philip Morris Magyarország Kft.",
        "ndn_code": "100001",
        "ean": "5990000000011",
        "category": category,
        "name": name,
        "brand": "Brand",
        "quantity": quantity,
        "unit_name": unit_name,
        "price": price,
        "valid_from": valid_from,
    }


def _seed_and_snapshot(workdir: Path, catalog: dict[str, Any]) -> tuple[bytes, int]:
    data_dir = workdir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    p = data_dir / "prices.json"
    p.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    os.utime(p, (1700000000, 1700000000))
    return p.read_bytes(), p.stat().st_mtime_ns


# ---------------------------------------------------------------------------
# F1: SZTFH HTML Link Discovery — Error & Edge Cases (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier2
def test_t2_f1_http_404_on_html_exits_nonzero_preserves_json(
    mock_sztfh_server, scraper_runner, tmp_path: Path
) -> None:
    workdir = tmp_path / "case_404_html"
    bytes_before, mtime_before = _seed_and_snapshot(workdir, build_realistic_catalog(count=20))
    mock_sztfh_server.set_html("Not Found", status=404)

    res = scraper_runner.run(mock_sztfh_server, workdir=workdir)
    assert res.returncode != 0
    assert res.raw_bytes == bytes_before
    assert res.mtime_ns == mtime_before


@pytest.mark.tier2
def test_t2_f1_http_500_on_html_exits_nonzero_preserves_json(
    mock_sztfh_server, scraper_runner, tmp_path: Path
) -> None:
    workdir = tmp_path / "case_500_html"
    bytes_before, mtime_before = _seed_and_snapshot(workdir, build_realistic_catalog(count=20))
    mock_sztfh_server.set_html("Server Error", status=500)

    res = scraper_runner.run(mock_sztfh_server, workdir=workdir)
    assert res.returncode != 0
    assert res.raw_bytes == bytes_before
    assert res.mtime_ns == mtime_before


@pytest.mark.tier2
@pytest.mark.parametrize("xlsx_status", [500, 404])
def test_t2_f1_http_500_on_xlsx_exits_nonzero_preserves_json(
    mock_sztfh_server, scraper_runner, tmp_path: Path, xlsx_status: int
) -> None:
    workdir = tmp_path / f"case_{xlsx_status}_xlsx"
    bytes_before, mtime_before = _seed_and_snapshot(workdir, build_realistic_catalog(count=20))
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(xlsx_path, b"error", status=xlsx_status)

    res = scraper_runner.run(mock_sztfh_server, workdir=workdir)
    assert res.returncode != 0
    assert res.raw_bytes == bytes_before
    assert res.mtime_ns == mtime_before


@pytest.mark.tier2
def test_t2_f1_missing_xlsx_link_exits_nonzero_preserves_json(
    mock_sztfh_server, scraper_runner, tmp_path: Path
) -> None:
    workdir = tmp_path / "case_no_xlsx"
    bytes_before, mtime_before = _seed_and_snapshot(workdir, build_realistic_catalog(count=20))
    mock_sztfh_server.set_html(build_sztfh_html(no_xlsx=True))

    res = scraper_runner.run(mock_sztfh_server, workdir=workdir)
    assert res.returncode != 0
    assert res.raw_bytes == bytes_before
    assert res.mtime_ns == mtime_before


@pytest.mark.tier2
def test_t2_f1_relative_dot_segment_urljoin_and_accented_valtozassal_exclusion(
    mock_sztfh_server, scraper_runner
) -> None:
    valtozas_path = "/downloads/kisker_változással_20261126.xlsx"
    rel_href = "../downloads/alkalmazando_arak_20261125.xlsx"
    resolved_path = "/tevekenysegek/dohanyugyi-felugyelet/downloads/alkalmazando_arak_20261125.xlsx"

    html = build_sztfh_html(
        links_override=[
            MockSztfhLink(href=valtozas_path, text="Változással érintett"),
            MockSztfhLink(href=rel_href, text="Alkalmazandó"),
        ]
    )
    mock_sztfh_server.set_html(html)
    mock_sztfh_server.set_xlsx(valtozas_path, build_xlsx_bytes([_row("Wrong Accented Valtozas")]))
    mock_sztfh_server.set_xlsx(resolved_path, build_xlsx_bytes([_row("Resolved Dot Segment Item")]))

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    data = res.load_prices_json()
    assert data["meta"]["source_url"] == f"https://sztfh.hu{resolved_path}"
    assert [i["name"] for i in data["items"]] == ["Resolved Dot Segment Item"]


# ---------------------------------------------------------------------------
# F2: Excel Parsing Robustness — Boundary & Corrupt Payloads (6 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier2
def test_t2_f2_shifted_headers_at_row_6(mock_sztfh_server, scraper_runner) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    xlsx_bytes = build_xlsx_bytes(
        [_row("Item 1"), _row("Item 2"), _row("Item 3")],
        header_row=6,
        preamble_rows=[
            "SZTFH Árlista",
            "Második sor megjegyzés",
            "",
            "Hatályos: 2026.11.26.",
            "Megjegyzés: hivatalos adatok",
        ],
    )
    mock_sztfh_server.set_xlsx(xlsx_path, xlsx_bytes)

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    items = res.load_prices_json()["items"]
    assert [i["name"] for i in items] == ["Item 1", "Item 2", "Item 3"]


@pytest.mark.tier2
def test_t2_f2_reordered_columns_without_optional_fields(
    mock_sztfh_server, scraper_runner
) -> None:
    reordered_cols = (
        "Kiskereskedelmi eladási ár",
        "Mennyiség",
        "Termék megnevezése",
        "Érvényesség kezdete",
        "Mennyiségi egység",
        "Termék típus",
    )
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    xlsx_bytes = build_xlsx_bytes(
        [
            {
                "name": "Reordered Product A",
                "category": "Szivar",
                "quantity": 5,
                "unit_name": "szál",
                "price": 7500,
                "valid_from": "2026-11-26",
            }
        ],
        columns=reordered_cols,
        header_row=2,
    )
    mock_sztfh_server.set_xlsx(xlsx_path, xlsx_bytes)

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    item = res.load_prices_json()["items"][0]
    assert item["name"] == "Reordered Product A"
    assert item["category"] == "Szivar"
    assert item["unit"] == "5 szál"
    assert item["price"] == 7500
    assert item["valid_from"] == "2026-11-26"


@pytest.mark.tier2
def test_t2_f2_consecutive_and_trailing_blank_whitespace_rows(
    mock_sztfh_server, scraper_runner
) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    xlsx_bytes = build_xlsx_bytes(
        [
            _row("First Valid"),
            EmptyRow(2),
            WhitespaceRow(("   ", "\t", "  ")),
            EmptyRow(1),
            _row("Second Valid"),
            EmptyRow(3),
            WhitespaceRow((" ", " ", " ")),
            EmptyRow(2),
        ]
    )
    mock_sztfh_server.set_xlsx(xlsx_path, xlsx_bytes)

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert [i["name"] for i in res.load_prices_json()["items"]] == [
        "First Valid",
        "Second Valid",
    ]


@pytest.mark.tier2
def test_t2_f2_custom_sheet_name_and_repeated_header_row(
    mock_sztfh_server, scraper_runner
) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    xlsx_bytes = build_xlsx_bytes(
        [
            _row("Prod Before Repeat"),
            RawRow(list(MINIMAL_XLSX_COLUMNS)),
            _row("Prod After Repeat"),
        ],
        columns=MINIMAL_XLSX_COLUMNS,
        sheet_name="Árlista 2026",
    )
    mock_sztfh_server.set_xlsx(xlsx_path, xlsx_bytes)

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert [i["name"] for i in res.load_prices_json()["items"]] == [
        "Prod Before Repeat",
        "Prod After Repeat",
    ]


@pytest.mark.tier2
def test_t2_f2_corrupt_xlsx_exits_nonzero_preserves_json(
    mock_sztfh_server, scraper_runner, tmp_path: Path
) -> None:
    workdir = tmp_path / "corrupt_xlsx"
    bytes_before, mtime_before = _seed_and_snapshot(workdir, build_realistic_catalog(count=20))
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(xlsx_path, build_corrupt_xlsx_bytes("garbage"))

    res = scraper_runner.run(mock_sztfh_server, workdir=workdir)
    assert res.returncode != 0
    assert res.raw_bytes == bytes_before
    assert res.mtime_ns == mtime_before


@pytest.mark.tier2
def test_t2_f2_empty_zero_byte_xlsx_exits_nonzero_preserves_json(
    mock_sztfh_server, scraper_runner, tmp_path: Path
) -> None:
    workdir = tmp_path / "empty_bytes_xlsx"
    bytes_before, mtime_before = _seed_and_snapshot(workdir, build_realistic_catalog(count=20))
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(xlsx_path, build_corrupt_xlsx_bytes("empty_bytes"))

    res = scraper_runner.run(mock_sztfh_server, workdir=workdir)
    assert res.returncode != 0
    assert res.raw_bytes == bytes_before
    assert res.mtime_ns == mtime_before


# ---------------------------------------------------------------------------
# F3: Exact Product-Name Preservation — Edge Cases (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier2
def test_t2_f3_leading_trailing_spaces_and_tabs_stripped(
    mock_sztfh_server, scraper_runner
) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(xlsx_path, build_xlsx_bytes([_row("\t   Marlboro Gold 20   \t ")]))

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert res.load_prices_json()["items"][0]["name"] == "Marlboro Gold 20"


@pytest.mark.tier2
def test_t2_f3_internal_tabs_newlines_multi_spaces_collapsed(
    mock_sztfh_server, scraper_runner
) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path, build_xlsx_bytes([_row("Borkum\t\tRiff   \n Cherry   Ruby")])
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert res.load_prices_json()["items"][0]["name"] == "Borkum Riff Cherry Ruby"


@pytest.mark.tier2
def test_t2_f3_non_breaking_spaces_collapsed(mock_sztfh_server, scraper_runner) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path, build_xlsx_bytes([_row("Sopianae\xa0\xa0Multifilter\xa0Piros 20")])
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert res.load_prices_json()["items"][0]["name"] == "Sopianae Multifilter Piros 20"


@pytest.mark.tier2
def test_t2_f3_very_long_name_no_truncation(mock_sztfh_server, scraper_runner) -> None:
    long_name = (
        "Őszi Szűz Prémium Kézzel Sodort Extra Hosszú Fűszeres Pipadohány "
        "Különleges Régiós Kiadás 2026 - Limitált Arany Fémdobozos Kiszerelés "
        "1234567890-ABCDE-ŐŰÁÉÍÓÖÚÜ"
    )
    assert len(long_name) == 160
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(xlsx_path, build_xlsx_bytes([_row(long_name)]))

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    got = res.load_prices_json()["items"][0]["name"]
    assert len(got) == 160
    assert got == long_name


@pytest.mark.tier2
def test_t2_f3_blank_and_whitespace_only_name_rows_skipped(
    mock_sztfh_server, scraper_runner
) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes(
            [
                _row(""),
                _row("   \t  "),
                _row(None),  # type: ignore[arg-type]
                _row("Valid Product Name"),
            ]
        ),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    items = res.load_prices_json()["items"]
    assert [i["name"] for i in items] == ["Valid Product Name"]


# ---------------------------------------------------------------------------
# F4: Stable ID Hashing & Deduplication — Edge Cases (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier2
def test_t2_f4_whitespace_variants_normalize_and_dedup(
    mock_sztfh_server, scraper_runner
) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes(
            [
                _row("Alsbo  Ruby", "Egyéb fogyasztási dohány", 50, "g", 2500, "2024-01-01"),
                _row("  Alsbo Ruby ", "Egyéb fogyasztási dohány", 50, "g", 4500, "2026-10-01"),
            ]
        ),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    items = res.load_prices_json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "Alsbo Ruby"
    assert items[0]["price"] == 4500
    assert items[0]["id"] == compute_item_id("Alsbo Ruby", "50 g")


@pytest.mark.tier2
def test_t2_f4_duplicate_earlier_date_after_later_date_keeps_latest(
    mock_sztfh_server, scraper_runner
) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes(
            [
                _row("Alsbo Ruby", "Egyéb fogyasztási dohány", 50, "g", 4500, "2026-11-20"),
                _row("Alsbo Ruby", "Egyéb fogyasztási dohány", 50, "g", 3200, "2024-01-10"),
            ]
        ),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    items = res.load_prices_json()["items"]
    assert len(items) == 1
    assert items[0]["price"] == 4500
    assert items[0]["valid_from"] == "2026-11-20"


@pytest.mark.tier2
def test_t2_f4_duplicate_later_date_after_earlier_date_keeps_latest(
    mock_sztfh_server, scraper_runner
) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes(
            [
                _row("Alsbo Ruby", "Egyéb fogyasztási dohány", 50, "g", 3200, "2024-01-10"),
                _row("Alsbo Ruby", "Egyéb fogyasztási dohány", 50, "g", 4500, "2026-11-20"),
            ]
        ),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    items = res.load_prices_json()["items"]
    assert len(items) == 1
    assert items[0]["price"] == 4500
    assert items[0]["valid_from"] == "2026-11-20"


@pytest.mark.tier2
def test_t2_f4_duplicate_same_valid_from_tie_breaks_last_seen(
    mock_sztfh_server, scraper_runner
) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes(
            [
                _row("Tie Item", "Cigaretta", 20, "szál", 1000, "2026-11-26"),
                _row("Tie Item", "Cigaretta", 20, "szál", 1200, "2026-11-26"),
                _row("Tie Item", "Cigaretta", 20, "szál", 1500, "2026-11-26"),
            ]
        ),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    items = res.load_prices_json()["items"]
    assert len(items) == 1
    assert items[0]["price"] == 1500


@pytest.mark.tier2
def test_t2_f4_accented_names_produce_distinct_deterministic_ids(
    mock_sztfh_server, scraper_runner
) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes(
            [
                _row("Toro Rosso", "Szivar", 1, "szál", 3000),
                _row("Tóró Rósső", "Szivar", 1, "szál", 3100),
            ]
        ),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    items = res.load_prices_json()["items"]
    assert len(items) == 2
    by_name = {i["name"]: i for i in items}
    assert by_name["Toro Rosso"]["id"] == compute_item_id("Toro Rosso", "1 szál")
    assert by_name["Tóró Rósső"]["id"] == compute_item_id("Tóró Rósső", "1 szál")
    assert by_name["Toro Rosso"]["id"] != by_name["Tóró Rósső"]["id"]


# ---------------------------------------------------------------------------
# F5: Unit & Integer Price Extraction — Edge Cases (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier2
def test_t2_f5_point_zero_vs_point_fifty_decimal_units(
    mock_sztfh_server, scraper_runner
) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes(
            [
                _row("U1", quantity=20.0, unit_name="szál", price=2350),
                _row("U2", quantity=19.50, unit_name="g", price=3200),
                _row("U3", quantity=0.5, unit_name="ml", price=1200),
            ]
        ),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    by_name = {i["name"]: i for i in res.load_prices_json()["items"]}
    assert by_name["U1"]["unit"] == "20 szál"
    assert by_name["U2"]["unit"] == "19.5 g"
    assert by_name["U3"]["unit"] == "0.5 ml"


@pytest.mark.tier2
def test_t2_f5_boundary_prices_and_nbsp_formatted_price(
    mock_sztfh_server, scraper_runner
) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes(
            [
                _row("Min Price", price=1),
                _row("High Price", price=99999),
                _row("NBSP Price", price="12\xa0500 Ft"),
            ]
        ),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    by_name = {i["name"]: i for i in res.load_prices_json()["items"]}
    assert by_name["Min Price"]["price"] == 1
    assert by_name["High Price"]["price"] == 99999
    assert by_name["NBSP Price"]["price"] == 12500


@pytest.mark.tier2
def test_t2_f5_invalid_zero_negative_empty_prices_skipped(
    mock_sztfh_server, scraper_runner
) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes(
            [
                _row("Zero Price", price=0),
                _row("Neg Price", price=-500),
                _row("Empty Price", price=""),
                _row("Text Price", price="N/A"),
                _row("None Price", price=None),
                _row("Valid Positive", price=2350),
            ]
        ),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    items = res.load_prices_json()["items"]
    assert [i["name"] for i in items] == ["Valid Positive"]


@pytest.mark.tier2
def test_t2_f5_missing_unit_name_or_missing_quantity_cleanly_formatted(
    mock_sztfh_server, scraper_runner
) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes(
            [
                _row("Only Qty", quantity=20, unit_name=None, price=2350),
                _row("Only Unit", quantity=None, unit_name="csomag", price=2350),
            ]
        ),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    by_name = {i["name"]: i for i in res.load_prices_json()["items"]}
    assert by_name["Only Qty"]["unit"] == "20"
    assert by_name["Only Unit"]["unit"] == "csomag"


@pytest.mark.tier2
def test_t2_f5_single_combined_kiszereles_col_and_float_price(
    mock_sztfh_server, scraper_runner
) -> None:
    cols = (
        "Termék megnevezése",
        "Termék típus",
        "Kiszerelés",
        "Kiskereskedelmi eladási ár",
        "Érvényesség kezdete",
    )
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes(
            [
                {
                    "name": "Combined Unit Item",
                    "category": "Cigaretta",
                    "unit": "20 szál",
                    "price": 2350.0,
                    "valid_from": "2026-11-26",
                }
            ],
            columns=cols,
        ),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    item = res.load_prices_json()["items"][0]
    assert item["unit"] == "20 szál"
    assert item["price"] == 2350
    assert type(item["price"]) is int


# ---------------------------------------------------------------------------
# F6: valid_from Date Normalization — Edge Cases (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier2
def test_t2_f6_excel_serial_1900_epoch_dates(mock_sztfh_server, scraper_runner) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes(
            [
                _row("D1", valid_from=1),
                _row("D2", valid_from=61),
                _row("D3", valid_from=36526),
                _row("D4", valid_from=46296),
            ]
        ),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    by_name = {i["name"]: i["valid_from"] for i in res.load_prices_json()["items"]}
    assert by_name == {
        "D1": "1900-01-01",
        "D2": "1900-03-01",
        "D3": "2000-01-01",
        "D4": "2026-10-01",
    }


@pytest.mark.tier2
def test_t2_f6_excel_serial_float_with_time_fraction(
    mock_sztfh_server, scraper_runner
) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(xlsx_path, build_xlsx_bytes([_row(valid_from=46296.75)]))

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert res.load_prices_json()["items"][0]["valid_from"] == "2026-10-01"


@pytest.mark.tier2
def test_t2_f6_hungarian_spaced_dotted_and_iso_datetime_strings(
    mock_sztfh_server, scraper_runner
) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes(
            [
                _row("Spaced Dot", valid_from="2026. 11. 26."),
                _row("ISO DateTime", valid_from="2026-11-26T00:00:00"),
            ]
        ),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert [i["valid_from"] for i in res.load_prices_json()["items"]] == [
        "2026-11-26",
        "2026-11-26",
    ]


@pytest.mark.tier2
def test_t2_f6_european_dd_mm_yyyy_string(mock_sztfh_server, scraper_runner) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(xlsx_path, build_xlsx_bytes([_row(valid_from="26.11.2026")]))

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert res.load_prices_json()["items"][0]["valid_from"] == "2026-11-26"


@pytest.mark.tier2
def test_t2_f6_blank_or_none_valid_from_handled_gracefully(
    mock_sztfh_server, scraper_runner
) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes(
            [
                _row("None Date", valid_from=None),
                _row("Empty Date", valid_from=""),
            ]
        ),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    items = res.load_prices_json()["items"]
    assert len(items) == 2
    assert items[0]["valid_from"] == ""
    assert items[1]["valid_from"] == ""


# ---------------------------------------------------------------------------
# F7: Category Extraction — Edge Cases (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier2
def test_t2_f7_missing_category_column_falls_back_to_egyeb(
    mock_sztfh_server, scraper_runner
) -> None:
    cols = (
        "Termék megnevezése",
        "Mennyiség",
        "Mennyiségi egység",
        "Kiskereskedelmi eladási ár",
        "Érvényesség kezdete",
    )
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes([_row("No Cat Col 1"), _row("No Cat Col 2")], columns=cols),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert [i["category"] for i in res.load_prices_json()["items"]] == ["Egyéb", "Egyéb"]


@pytest.mark.tier2
def test_t2_f7_blank_category_cells_fall_back_to_egyeb(
    mock_sztfh_server, scraper_runner
) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes(
            [
                _row("Has Cat", category="Cigaretta"),
                _row("None Cat", category=None),
                _row("Blank Cat", category="   "),
            ]
        ),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    by_name = {i["name"]: i["category"] for i in res.load_prices_json()["items"]}
    assert by_name == {
        "Has Cat": "Cigaretta",
        "None Cat": "Egyéb",
        "Blank Cat": "Egyéb",
    }


@pytest.mark.tier2
def test_t2_f7_category_whitespace_normalized(mock_sztfh_server, scraper_runner) -> None:
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes([_row("Cat WS", category="  Finomra  vágott   dohány  ")]),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert res.load_prices_json()["items"][0]["category"] == "Finomra vágott dohány"


@pytest.mark.tier2
def test_t2_f7_multiple_section_headers_switch_category(
    mock_sztfh_server, scraper_runner
) -> None:
    cols = (
        "Termék megnevezése",
        "Mennyiség",
        "Mennyiségi egység",
        "Kiskereskedelmi eladási ár",
        "Érvényesség kezdete",
    )
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes(
            [
                CategorySeparator("Cigaretta", col_index=1),
                _row("C1"),
                _row("C2"),
                CategorySeparator("Szivarka", col_index=1),
                _row("S1"),
                _row("S2"),
                CategorySeparator("Szivar", col_index=1),
                _row("Z1"),
            ],
            columns=cols,
        ),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    by_name = {i["name"]: i["category"] for i in res.load_prices_json()["items"]}
    assert by_name == {
        "C1": "Cigaretta",
        "C2": "Cigaretta",
        "S1": "Szivarka",
        "S2": "Szivarka",
        "Z1": "Szivar",
    }


@pytest.mark.tier2
def test_t2_f7_section_header_persists_across_blank_rows(
    mock_sztfh_server, scraper_runner
) -> None:
    cols = (
        "Termék megnevezése",
        "Mennyiség",
        "Mennyiségi egység",
        "Kiskereskedelmi eladási ár",
        "Érvényesség kezdete",
    )
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes(
            [
                CategorySeparator("Szivar", col_index=1),
                EmptyRow(2),
                _row("Cohiba Robusto"),
            ],
            columns=cols,
        ),
    )

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert res.load_prices_json()["items"][0]["category"] == "Szivar"


# ---------------------------------------------------------------------------
# F8: Price-Diff Calculation — Edge Cases (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier2
def test_t2_f8_mixed_zero_change_increase_decrease_new_items(
    mock_sztfh_server, scraper_runner
) -> None:
    prior = build_prices_json(
        [
            make_catalog_item(name="Unchanged", category="Cigaretta", unit="20 szál", price=2200),
            make_catalog_item(name="Increased", category="Cigaretta", unit="20 szál", price=2000),
            make_catalog_item(name="Decreased", category="Cigaretta", unit="20 szál", price=2500),
        ],
        source_file="alkalmazando_old.xlsx",
    )
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes(
            [
                _row("Unchanged", "Cigaretta", 20, "szál", 2200),
                _row("Increased", "Cigaretta", 20, "szál", 2100),
                _row("Decreased", "Cigaretta", 20, "szál", 2450),
                _row("Brand New", "Szivarka", 10, "szál", 1900),
            ]
        ),
    )

    res = scraper_runner.run(mock_sztfh_server, seed_prices_json=prior)
    assert res.returncode == 0, res.stderr
    data = res.load_prices_json()
    validate_prices_schema(data)
    by_name = {i["name"]: i for i in data["items"]}
    assert by_name["Unchanged"]["previous_price"] is None
    assert by_name["Unchanged"]["price_diff"] is None
    assert by_name["Unchanged"]["is_new"] is False
    assert by_name["Increased"]["previous_price"] == 2000
    assert by_name["Increased"]["price_diff"] == 100
    assert by_name["Increased"]["is_new"] is False
    assert by_name["Decreased"]["previous_price"] == 2500
    assert by_name["Decreased"]["price_diff"] == -50
    assert by_name["Decreased"]["is_new"] is False
    assert by_name["Brand New"]["is_new"] is True
    assert by_name["Brand New"]["previous_price"] is None
    assert by_name["Brand New"]["price_diff"] is None


@pytest.mark.tier2
def test_t2_f8_removed_item_absent_from_output(mock_sztfh_server, scraper_runner) -> None:
    prior = build_prices_json(
        [
            make_catalog_item(name="Keep A", category="Cigaretta", unit="20 szál", price=2200),
            make_catalog_item(name="Drop B", category="Cigaretta", unit="20 szál", price=2300),
        ],
        source_file="alkalmazando_old.xlsx",
    )
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(xlsx_path, build_xlsx_bytes([_row("Keep A")]))

    res = scraper_runner.run(mock_sztfh_server, seed_prices_json=prior)
    assert res.returncode == 0, res.stderr
    data = res.load_prices_json()
    assert data["meta"]["total_count"] == 1
    assert [i["name"] for i in data["items"]] == ["Keep A"]


@pytest.mark.tier2
def test_t2_f8_empty_prior_items_list_baseline(mock_sztfh_server, scraper_runner) -> None:
    prior = build_prices_json([], source_file="alkalmazando_old.xlsx")
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(xlsx_path, build_xlsx_bytes([_row("Item 1"), _row("Item 2")]))

    res = scraper_runner.run(mock_sztfh_server, seed_prices_json=prior)
    assert res.returncode == 0, res.stderr
    data = res.load_prices_json()
    validate_prices_schema(data)
    assert len(data["items"]) == 2
    for item in data["items"]:
        assert item["previous_price"] is None
        assert item["price_diff"] is None
        assert isinstance(item["is_new"], bool)


@pytest.mark.tier2
def test_t2_f8_multi_run_price_reversal_tracks_latest_diff(
    mock_sztfh_server, scraper_runner, tmp_path: Path
) -> None:
    workdir = tmp_path / "reversal"
    for step, (suffix, price) in enumerate([("01", 2000), ("02", 2200), ("03", 2000)], start=1):
        xlsx_path = f"/downloads/dohany/arkozzetetel/alkalmazando_202611{suffix}120001.xlsx"
        mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
        mock_sztfh_server.set_xlsx(
            xlsx_path, build_xlsx_bytes([_row("Cycle Item", "Cigaretta", 20, "szál", price)])
        )
        res = scraper_runner.run(mock_sztfh_server, workdir=workdir)
        assert res.returncode == 0, res.stderr
        item = res.load_prices_json()["items"][0]
        if step == 2:
            assert item["previous_price"] == 2000
            assert item["price_diff"] == 200
            assert item["is_new"] is False
        elif step == 3:
            assert item["price"] == 2000
            assert item["previous_price"] == 2200
            assert item["price_diff"] == -200
            assert item["is_new"] is False


@pytest.mark.tier2
def test_t2_f8_prior_diff_item_with_unchanged_price_new_source_file(
    mock_sztfh_server, scraper_runner
) -> None:
    prior = build_prices_json(
        [
            make_catalog_item(
                name="Carryover Item",
                category="Cigaretta",
                unit="20 szál",
                price=2350,
                previous_price=2300,
                changed_at="2026-11-20",
                is_new=False,
            )
        ],
        source_file="alkalmazando_20261120.xlsx",
    )
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(
        xlsx_path,
        build_xlsx_bytes([_row("Carryover Item", "Cigaretta", 20, "szál", 2350)]),
    )

    res = scraper_runner.run(mock_sztfh_server, seed_prices_json=prior)
    assert res.returncode == 0, res.stderr
    item = res.load_prices_json()["items"][0]
    assert item["price"] == 2350
    assert item["is_new"] is False
    assert (
        (item["previous_price"] is None and item["price_diff"] is None)
        or (item["previous_price"] == 2300 and item["price_diff"] == 50)
    )


# ---------------------------------------------------------------------------
# F9: Idempotent data/prices.json & Scale Invariants (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier2
def test_t2_f9_different_source_file_same_items_updates_meta(
    mock_sztfh_server, scraper_runner
) -> None:
    prior = build_prices_json(
        [make_catalog_item(name="Same Item", category="Cigaretta", unit="20 szál", price=2350)],
        last_updated="2026-01-01T00:00:00Z",
        source_file="alkalmazando_20261124.xlsx",
    )
    new_path = "/downloads/dohany/arkozzetetel/alkalmazando_20261125.xlsx"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[new_path]))
    mock_sztfh_server.set_xlsx(
        new_path, build_xlsx_bytes([_row("Same Item", "Cigaretta", 20, "szál", 2350)])
    )

    res = scraper_runner.run(mock_sztfh_server, seed_prices_json=prior)
    assert res.returncode == 0, res.stderr
    data = res.load_prices_json()
    assert data["meta"]["source_file"] == "alkalmazando_20261125.xlsx"
    assert data["meta"]["last_updated"] != "2026-01-01T00:00:00Z"


@pytest.mark.tier2
def test_t2_f9_same_source_file_changed_price_updates_json(
    mock_sztfh_server, scraper_runner
) -> None:
    prior = build_prices_json(
        [make_catalog_item(name="Edited Item", category="Cigaretta", unit="20 szál", price=2000)],
        last_updated="2026-01-01T00:00:00Z",
        source_file="alkalmazando_20261125.xlsx",
    )
    same_path = "/downloads/dohany/arkozzetetel/alkalmazando_20261125.xlsx"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[same_path]))
    mock_sztfh_server.set_xlsx(
        same_path, build_xlsx_bytes([_row("Edited Item", "Cigaretta", 20, "szál", 2100)])
    )

    res = scraper_runner.run(mock_sztfh_server, seed_prices_json=prior)
    assert res.returncode == 0, res.stderr
    data = res.load_prices_json()
    assert data["items"][0]["price"] == 2100
    assert data["items"][0]["price_diff"] == 100
    assert data["meta"]["last_updated"] != "2026-01-01T00:00:00Z"


@pytest.mark.tier2
def test_t2_f9_same_source_file_whitespace_only_xlsx_diff_is_idempotent(
    mock_sztfh_server, scraper_runner, tmp_path: Path
) -> None:
    workdir = tmp_path / "ws_idempotent"
    same_file = "alkalmazando_20261125.xlsx"
    prior = build_prices_json(
        [
            make_catalog_item(
                name="Marlboro Red",
                category="Cigaretta",
                unit="20 szál",
                price=2350,
                valid_from="2026-11-26",
            )
        ],
        last_updated="2026-01-01T00:00:00Z",
        source_file=same_file,
        source_url=f"https://sztfh.hu/downloads/dohany/arkozzetetel/{same_file}",
    )
    bytes_before, mtime_before = _seed_and_snapshot(workdir, prior)

    same_path = f"/downloads/dohany/arkozzetetel/{same_file}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[same_path]))
    mock_sztfh_server.set_xlsx(
        same_path,
        build_xlsx_bytes([_row("  Marlboro  Red  ", "Cigaretta", 20, "szál", 2350, "2026-11-26")]),
    )

    res = scraper_runner.run(mock_sztfh_server, workdir=workdir)
    assert res.returncode == 0, res.stderr
    assert res.raw_bytes == bytes_before
    assert res.mtime_ns == mtime_before
    assert res.load_prices_json()["meta"]["last_updated"] == "2026-01-01T00:00:00Z"


@pytest.mark.tier2
def test_t2_f9_creates_missing_data_directory(
    mock_sztfh_server, scraper_runner, tmp_path: Path
) -> None:
    workdir = tmp_path / "no_data_dir"
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(xlsx_path, build_xlsx_bytes([_row("Auto Dir Item")]))

    assert not (workdir / "data").exists()
    res = scraper_runner.run(mock_sztfh_server, workdir=workdir, create_data_dir=False)
    assert res.returncode == 0, res.stderr
    assert res.prices_json_path.exists()
    validate_prices_schema(res.load_prices_json())


@pytest.mark.tier2
def test_t2_f9_high_volume_4600_items_scale_and_invariants(
    mock_sztfh_server, scraper_runner
) -> None:
    large = build_large_catalog(4610)
    rows = [
        {
            "name": f"  {item['name']}  ",
            "category": item["category"],
            "unit": item["unit"],
            "price": item["price"],
            "valid_from": item["valid_from"],
        }
        for item in large["items"]
    ]
    xlsx_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[xlsx_path]))
    mock_sztfh_server.set_xlsx(xlsx_path, build_xlsx_bytes(rows, columns=MINIMAL_XLSX_COLUMNS))

    res = scraper_runner.run(mock_sztfh_server, timeout=60.0)
    assert res.returncode == 0, res.stderr
    data = res.load_prices_json()
    validate_prices_schema(data)
    assert data["meta"]["total_count"] == len(data["items"]) == 4610
    assert len({i["id"] for i in data["items"]}) == 4610
