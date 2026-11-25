from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from helpers import (
    DEFAULT_SOURCE_FILE,
    DEFAULT_XLSX_COLUMNS,
    MINIMAL_XLSX_COLUMNS,
    SZTFH_PAGE_URL,
    CategorySeparator,
    EmptyRow,
    MockSztfhLink,
    build_prices_json,
    build_realistic_catalog,
    build_sztfh_html,
    build_xlsx_bytes,
    compute_item_id,
    make_catalog_item,
    validate_prices_schema,
)


def _setup_single_xlsx(
    mock_server,
    rows: list[Any],
    *,
    xlsx_path: str = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}",
    html: str | None = None,
    columns: tuple[str, ...] = DEFAULT_XLSX_COLUMNS,
    header_row: int = 2,
    preamble_rows: list[Any] | None = None,
    sheet_name: str = "Munka1",
    require_custom_ua: bool = False,
) -> None:
    if require_custom_ua:
        mock_server.require_custom_ua = True
    if html is None:
        html = build_sztfh_html(alkalmazando_links=[xlsx_path])
    mock_server.set_html(html)
    xlsx_bytes = build_xlsx_bytes(
        rows,
        columns=columns,
        header_row=header_row,
        preamble_rows=preamble_rows,
        sheet_name=sheet_name,
    )
    mock_server.set_xlsx(xlsx_path, xlsx_bytes)


def _sample_row(
    name: str = "Marlboro Red 20 szálas",
    category: str = "Cigaretta",
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
        "brand": name.split()[0] if name.split() else "Brand",
        "quantity": quantity,
        "unit_name": unit_name,
        "price": price,
        "valid_from": valid_from,
    }


# ---------------------------------------------------------------------------
# F1: SZTFH HTML Link Discovery (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_t1_f1_custom_user_agent_sent(mock_sztfh_server, scraper_runner) -> None:
    _setup_single_xlsx(mock_sztfh_server, [_sample_row()], require_custom_ua=True)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert len(res.requests) >= 2
    for req in res.requests:
        ua = req["user_agent"].strip()
        assert ua, f"Empty User-Agent on {req['path']}"
        assert not ua.lower().startswith("python-requests/"), f"Default UA used: {ua}"


@pytest.mark.tier1
def test_t1_f1_relative_urljoin_resolution(mock_sztfh_server, scraper_runner) -> None:
    rel_path = "/downloads/dohany/arkozzetetel/alkalmazando_arak_20261125120001.xlsx"
    _setup_single_xlsx(
        mock_sztfh_server,
        [_sample_row()],
        xlsx_path=rel_path,
        html=build_sztfh_html(alkalmazando_links=[rel_path]),
    )
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    data = res.load_prices_json()
    assert data["meta"]["source_url"] == f"https://sztfh.hu{rel_path}"
    assert any(r["path"] == rel_path for r in res.requests)


@pytest.mark.tier1
def test_t1_f1_absolute_url_link_discovery(mock_sztfh_server, scraper_runner) -> None:
    abs_path = "/downloads/alkalmazando_arak_20261125120001.xlsx"
    abs_url = f"https://sztfh.hu{abs_path}"
    _setup_single_xlsx(
        mock_sztfh_server,
        [_sample_row()],
        xlsx_path=abs_path,
        html=build_sztfh_html(alkalmazando_links=[abs_url]),
    )
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    data = res.load_prices_json()
    assert data["meta"]["source_url"] == abs_url
    assert data["meta"]["source_file"] == "alkalmazando_arak_20261125120001.xlsx"


@pytest.mark.tier1
def test_t1_f1_selects_alkalmazando_excludes_2017_and_valtozas(
    mock_sztfh_server, scraper_runner
) -> None:
    archive_path = "/downloads/dohany/arkozzetetel/2017_januar_1-jet_kovetoen_20261126120001.xlsx"
    valtozas_path = "/downloads/dohany/arkozzetetel/kiskereskedelmi_ar_valtozassal_erintett_20261126120001.xlsx"
    full_path = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"

    html = build_sztfh_html(
        links_override=[
            MockSztfhLink(href=archive_path, text="2017 archive"),
            MockSztfhLink(href=valtozas_path, text="Valtozas link"),
            MockSztfhLink(href=full_path, text="Alkalmazando full list"),
        ]
    )
    mock_sztfh_server.set_html(html)
    mock_sztfh_server.set_xlsx(archive_path, build_xlsx_bytes([_sample_row("Wrong 2017 Item")]))
    mock_sztfh_server.set_xlsx(valtozas_path, build_xlsx_bytes([_sample_row("Wrong Valtozas Item")]))
    mock_sztfh_server.set_xlsx(full_path, build_xlsx_bytes([_sample_row("Expected Full Item")]))

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    data = res.load_prices_json()
    assert data["meta"]["source_file"] == DEFAULT_SOURCE_FILE
    assert [i["name"] for i in data["items"]] == ["Expected Full Item"]


@pytest.mark.tier1
def test_t1_f1_selects_latest_timestamped_alkalmazando_link(
    mock_sztfh_server, scraper_runner
) -> None:
    older = "/downloads/dohany/arkozzetetel/alkalmazando_dohanygyartmany_kiskereskedelmi_eladasi_arak_20261120120001.xlsx"
    newer = f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    html = build_sztfh_html(
        links_override=[
            MockSztfhLink(href=older, text="Alkalmazando 2026-11-20"),
            MockSztfhLink(href=newer, text="Alkalmazando 2026-11-25"),
        ]
    )
    mock_sztfh_server.set_html(html)
    mock_sztfh_server.set_xlsx(older, build_xlsx_bytes([_sample_row("Older Item")]))
    mock_sztfh_server.set_xlsx(newer, build_xlsx_bytes([_sample_row("Newer Item")]))

    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    data = res.load_prices_json()
    assert data["meta"]["source_file"] == DEFAULT_SOURCE_FILE
    assert [i["name"] for i in data["items"]] == ["Newer Item"]


# ---------------------------------------------------------------------------
# F2: Excel Parsing Robustness (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_t1_f2_standard_sztfh_row2_header_table(mock_sztfh_server, scraper_runner) -> None:
    rows = [
        _sample_row("Marlboro Red 20 szálas", "Cigaretta", 20, "szál", 2350, "2026-11-26"),
        _sample_row("TEREA Amber", "Hevítésre szánt dohánytermék (zárjegyes)", 20, "szál", 2390, "2026-11-26"),
        _sample_row("Moods Filter 10", "Szivarka", 10, "szál", 2190, "2026-10-01"),
    ]
    _setup_single_xlsx(mock_sztfh_server, rows, header_row=2)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    data = res.load_prices_json()
    validate_prices_schema(data)
    assert len(data["items"]) == 3
    assert [i["name"] for i in data["items"]] == [
        "Marlboro Red 20 szálas",
        "TEREA Amber",
        "Moods Filter 10",
    ]


@pytest.mark.tier1
def test_t1_f2_shifted_header_row3(mock_sztfh_server, scraper_runner) -> None:
    rows = [
        _sample_row("Sopianae Multifilter Piros 20", "Cigaretta", 20, "szál", 2150),
        _sample_row("Dunhill Fine Cut Blue 20", "Cigaretta", 20, "szál", 2550),
    ]
    _setup_single_xlsx(
        mock_sztfh_server,
        rows,
        header_row=3,
        preamble_rows=["SZTFH Hivatalos Árlista", "Közzététel: 2026.11.25."],
    )
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    data = res.load_prices_json()
    assert len(data["items"]) == 2
    assert {i["name"] for i in data["items"]} == {
        "Sopianae Multifilter Piros 20",
        "Dunhill Fine Cut Blue 20",
    }


@pytest.mark.tier1
def test_t1_f2_skips_interspersed_empty_rows(mock_sztfh_server, scraper_runner) -> None:
    rows = [
        _sample_row("Product A"),
        EmptyRow(2),
        _sample_row("Product B"),
    ]
    _setup_single_xlsx(mock_sztfh_server, rows)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    data = res.load_prices_json()
    assert [i["name"] for i in data["items"]] == ["Product A", "Product B"]


@pytest.mark.tier1
def test_t1_f2_maps_termek_tipus_column(mock_sztfh_server, scraper_runner) -> None:
    rows = [
        _sample_row("Item Cig", "Cigaretta", 20, "szál", 2200),
        _sample_row("Item Szivar", "Szivar", 1, "szál", 4500),
        _sample_row("Item Szivarka", "Szivarka", 10, "szál", 1990),
    ]
    _setup_single_xlsx(mock_sztfh_server, rows)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    by_name = {i["name"]: i for i in res.load_prices_json()["items"]}
    assert by_name["Item Cig"]["category"] == "Cigaretta"
    assert by_name["Item Szivar"]["category"] == "Szivar"
    assert by_name["Item Szivarka"]["category"] == "Szivarka"


@pytest.mark.tier1
def test_t1_f2_parses_category_separator_rows(mock_sztfh_server, scraper_runner) -> None:
    cols = (
        "Termék megnevezése",
        "Mennyiség",
        "Mennyiségi egység",
        "Kiskereskedelmi eladási ár",
        "Érvényesség kezdete",
    )
    rows = [
        CategorySeparator("Cigaretta", col_index=1),
        {"name": "Marlboro Gold 20", "quantity": 20, "unit_name": "szál", "price": 2400, "valid_from": "2026-11-26"},
        {"name": "Camel Yellow 20", "quantity": 20, "unit_name": "szál", "price": 2320, "valid_from": "2026-11-26"},
        CategorySeparator("Szivarka", col_index=1),
        {"name": "Moods Filter 10", "quantity": 10, "unit_name": "szál", "price": 2190, "valid_from": "2026-11-26"},
    ]
    _setup_single_xlsx(mock_sztfh_server, rows, columns=cols)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    items = res.load_prices_json()["items"]
    assert len(items) == 3
    by_name = {i["name"]: i for i in items}
    assert by_name["Marlboro Gold 20"]["category"] == "Cigaretta"
    assert by_name["Camel Yellow 20"]["category"] == "Cigaretta"
    assert by_name["Moods Filter 10"]["category"] == "Szivarka"


# ---------------------------------------------------------------------------
# F3: Exact Product-Name Preservation (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_t1_f3_preserves_brands_and_numbers_verbatim(mock_sztfh_server, scraper_runner) -> None:
    names = [
        "Marlboro Red 20 szálas",
        "Sopianae Multifilter Piros 20",
        "Dunhill Fine Cut Blue 20",
    ]
    _setup_single_xlsx(mock_sztfh_server, [_sample_row(n) for n in names])
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert [i["name"] for i in res.load_prices_json()["items"]] == names


@pytest.mark.tier1
def test_t1_f3_preserves_hungarian_accents_o_u_verbatim(mock_sztfh_server, scraper_runner) -> None:
    names = [
        "Őszi Szűz Füst 20",
        "Hűvös Kék Áfonya Édes Ízű Örökség Üveg",
    ]
    _setup_single_xlsx(mock_sztfh_server, [_sample_row(n) for n in names])
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    got = [i["name"] for i in res.load_prices_json()["items"]]
    assert got == names
    assert "Ő" in got[0] and "ű" in got[0]
    assert "ű" in got[1] and "Á" in got[1]


@pytest.mark.tier1
def test_t1_f3_preserves_punctuation_hyphens_parens_verbatim(
    mock_sztfh_server, scraper_runner
) -> None:
    names = [
        "10ml MIST e-liquid 00mg - COCO'S BAKERY",
        "Cohiba Siglo II (25)",
        "J&J Black & Gold 1/4",
    ]
    _setup_single_xlsx(mock_sztfh_server, [_sample_row(n) for n in names])
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert [i["name"] for i in res.load_prices_json()["items"]] == names


@pytest.mark.tier1
def test_t1_f3_only_strips_and_collapses_internal_spaces(
    mock_sztfh_server, scraper_runner
) -> None:
    rows = [
        _sample_row("  Borkum Riff  Cherry   Ruby  "),
        _sample_row("  Stanwell  Vanilla Sungold "),
    ]
    _setup_single_xlsx(mock_sztfh_server, rows)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert [i["name"] for i in res.load_prices_json()["items"]] == [
        "Borkum Riff Cherry Ruby",
        "Stanwell Vanilla Sungold",
    ]


@pytest.mark.tier1
def test_t1_f3_preserves_case_and_mixed_styling_verbatim(
    mock_sztfh_server, scraper_runner
) -> None:
    names = ["TEREA Amber", "HEETS Sienna Selection", "iQOS TEREA Turquoise"]
    _setup_single_xlsx(mock_sztfh_server, [_sample_row(n) for n in names])
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert [i["name"] for i in res.load_prices_json()["items"]] == names


# ---------------------------------------------------------------------------
# F4: Stable ID Hashing & Deduplication (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_t1_f4_id_matches_sha256_name_unit_formula(mock_sztfh_server, scraper_runner) -> None:
    _setup_single_xlsx(
        mock_sztfh_server,
        [_sample_row("Marlboro Red 20 szálas", quantity=20, unit_name="szál")],
    )
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    item = res.load_prices_json()["items"][0]
    expected = hashlib.sha256("Marlboro Red 20 szálas|20 szál".encode("utf-8")).hexdigest()[:16]
    assert item["id"] == expected


@pytest.mark.tier1
def test_t1_f4_all_ids_unique_16_char_lowercase_hex(mock_sztfh_server, scraper_runner) -> None:
    rows = [
        _sample_row(f"Termék {idx}", quantity=10 + idx, unit_name="szál", price=1500 + idx * 10)
        for idx in range(15)
    ]
    _setup_single_xlsx(mock_sztfh_server, rows)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    items = res.load_prices_json()["items"]
    assert len(items) == 15
    ids = [i["id"] for i in items]
    assert len(set(ids)) == 15
    assert all(re.match(r"^[0-9a-f]{16}$", item_id) for item_id in ids)


@pytest.mark.tier1
def test_t1_f4_distinct_units_produce_distinct_ids(mock_sztfh_server, scraper_runner) -> None:
    rows = [
        _sample_row("Marlboro Red", quantity=20, unit_name="szál", price=2350),
        _sample_row("Marlboro Red", quantity=25, unit_name="szál", price=2850),
    ]
    _setup_single_xlsx(mock_sztfh_server, rows)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    items = res.load_prices_json()["items"]
    assert len(items) == 2
    ids = {i["unit"]: i["id"] for i in items}
    assert ids["20 szál"] == compute_item_id("Marlboro Red", "20 szál")
    assert ids["25 szál"] == compute_item_id("Marlboro Red", "25 szál")
    assert ids["20 szál"] != ids["25 szál"]


@pytest.mark.tier1
def test_t1_f4_dedup_keeps_latest_valid_from(mock_sztfh_server, scraper_runner) -> None:
    rows = [
        _sample_row("Alsbo Ruby", "Egyéb fogyasztási dohány", 50, "g", 2550, "2017-06-15"),
        _sample_row("Alsbo Ruby", "Egyéb fogyasztási dohány", 50, "g", 4500, "2026-10-01"),
    ]
    _setup_single_xlsx(mock_sztfh_server, rows)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    items = res.load_prices_json()["items"]
    assert len(items) == 1
    assert items[0]["price"] == 4500
    assert items[0]["valid_from"] == "2026-10-01"


@pytest.mark.tier1
def test_t1_f4_dedup_tie_break_keeps_last_seen(mock_sztfh_server, scraper_runner) -> None:
    rows = [
        _sample_row("Cohiba BHK 52", "Szivar", 1, "szál", 18000, "2026-11-26"),
        _sample_row("Cohiba BHK 52", "Szivar", 1, "szál", 19500, "2026-11-26"),
    ]
    _setup_single_xlsx(mock_sztfh_server, rows)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    items = res.load_prices_json()["items"]
    assert len(items) == 1
    assert items[0]["price"] == 19500


# ---------------------------------------------------------------------------
# F5: Unit & Integer Price Extraction (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_t1_f5_integer_quantity_and_unit_combined(mock_sztfh_server, scraper_runner) -> None:
    rows = [
        _sample_row("Prod 1", quantity=20, unit_name="szál", price=2350),
        _sample_row("Prod 2", quantity=50, unit_name="g", price=4500),
        _sample_row("Prod 3", quantity=10, unit_name="ml", price=1900),
    ]
    _setup_single_xlsx(mock_sztfh_server, rows)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    by_name = {i["name"]: i for i in res.load_prices_json()["items"]}
    assert by_name["Prod 1"]["unit"] == "20 szál"
    assert by_name["Prod 2"]["unit"] == "50 g"
    assert by_name["Prod 3"]["unit"] == "10 ml"


@pytest.mark.tier1
def test_t1_f5_float_point_zero_stripped_from_unit(mock_sztfh_server, scraper_runner) -> None:
    rows = [
        _sample_row("Prod A", quantity=20.0, unit_name="szál", price=2350),
        _sample_row("Prod B", quantity=10.0, unit_name="ml", price=1900),
    ]
    _setup_single_xlsx(mock_sztfh_server, rows)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    by_name = {i["name"]: i for i in res.load_prices_json()["items"]}
    assert by_name["Prod A"]["unit"] == "20 szál"
    assert by_name["Prod B"]["unit"] == "10 ml"


@pytest.mark.tier1
def test_t1_f5_real_decimal_quantity_preserved_in_unit(
    mock_sztfh_server, scraper_runner
) -> None:
    rows = [
        _sample_row("Cut Tobacco A", quantity=19.5, unit_name="g", price=3100),
        _sample_row("Cut Tobacco B", quantity=16.5, unit_name="g", price=2800),
    ]
    _setup_single_xlsx(mock_sztfh_server, rows)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    by_name = {i["name"]: i for i in res.load_prices_json()["items"]}
    assert by_name["Cut Tobacco A"]["unit"] == "19.5 g"
    assert by_name["Cut Tobacco B"]["unit"] == "16.5 g"


@pytest.mark.tier1
def test_t1_f5_formatted_price_strings_parsed_to_int(mock_sztfh_server, scraper_runner) -> None:
    rows = [
        _sample_row("Price Str Space", price="2 350 Ft"),
        _sample_row("Price Str Compact", price="2350 Ft"),
        _sample_row("Price Float", price=2350.0),
    ]
    _setup_single_xlsx(mock_sztfh_server, rows)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    items = res.load_prices_json()["items"]
    assert len(items) == 3
    for item in items:
        assert item["price"] == 2350
        assert type(item["price"]) is int


@pytest.mark.tier1
def test_t1_f5_all_prices_positive_integers(mock_sztfh_server, scraper_runner) -> None:
    prices = [800, 1999, 2350, 4150, 18500]
    rows = [_sample_row(f"Prod {p}", price=p) for p in prices]
    _setup_single_xlsx(mock_sztfh_server, rows)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    items = res.load_prices_json()["items"]
    assert [i["price"] for i in items] == prices
    for item in items:
        assert isinstance(item["price"], int) and not isinstance(item["price"], bool)
        assert item["price"] > 0


# ---------------------------------------------------------------------------
# F6: valid_from Date Normalization (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_t1_f6_valid_from_python_datetime_cell(mock_sztfh_server, scraper_runner) -> None:
    _setup_single_xlsx(
        mock_sztfh_server,
        [_sample_row(valid_from=datetime(2026, 11, 26, 14, 30))],
    )
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert res.load_prices_json()["items"][0]["valid_from"] == "2026-11-26"


@pytest.mark.tier1
def test_t1_f6_valid_from_python_date_cell(mock_sztfh_server, scraper_runner) -> None:
    _setup_single_xlsx(mock_sztfh_server, [_sample_row(valid_from=date(2026, 11, 26))])
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert res.load_prices_json()["items"][0]["valid_from"] == "2026-11-26"


@pytest.mark.tier1
def test_t1_f6_valid_from_excel_serial_number(mock_sztfh_server, scraper_runner) -> None:
    rows = [
        _sample_row("Serial Oct", valid_from=46296),
        _sample_row("Serial Nov", valid_from=46352),
    ]
    _setup_single_xlsx(mock_sztfh_server, rows)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    by_name = {i["name"]: i for i in res.load_prices_json()["items"]}
    assert by_name["Serial Oct"]["valid_from"] == "2026-10-01"
    assert by_name["Serial Nov"]["valid_from"] == "2026-11-26"


@pytest.mark.tier1
def test_t1_f6_valid_from_iso_string(mock_sztfh_server, scraper_runner) -> None:
    _setup_single_xlsx(mock_sztfh_server, [_sample_row(valid_from="2026-11-26")])
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert res.load_prices_json()["items"][0]["valid_from"] == "2026-11-26"


@pytest.mark.tier1
def test_t1_f6_valid_from_hungarian_dotted_string(mock_sztfh_server, scraper_runner) -> None:
    rows = [
        _sample_row("Dot No Trail", valid_from="2026.11.26"),
        _sample_row("Dot With Trail", valid_from="2026.11.26."),
    ]
    _setup_single_xlsx(mock_sztfh_server, rows)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    items = res.load_prices_json()["items"]
    assert [i["valid_from"] for i in items] == ["2026-11-26", "2026-11-26"]


# ---------------------------------------------------------------------------
# F7: Category Extraction (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_t1_f7_extracts_termek_tipus_column_category(mock_sztfh_server, scraper_runner) -> None:
    _setup_single_xlsx(mock_sztfh_server, [_sample_row("Marlboro Red 20", "Cigaretta")])
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert res.load_prices_json()["items"][0]["category"] == "Cigaretta"


@pytest.mark.tier1
def test_t1_f7_extracts_multiple_categories(mock_sztfh_server, scraper_runner) -> None:
    cats = [
        "Cigaretta",
        "Szivar",
        "Finomra vágott fogyasztási dohány",
        "Hevítésre szánt dohánytermék (zárjegyes)",
    ]
    rows = [_sample_row(f"Item {idx}", c) for idx, c in enumerate(cats)]
    _setup_single_xlsx(mock_sztfh_server, rows)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert [i["category"] for i in res.load_prices_json()["items"]] == cats


@pytest.mark.tier1
def test_t1_f7_extracts_section_header_categories(mock_sztfh_server, scraper_runner) -> None:
    cols = (
        "Termék megnevezése",
        "Mennyiség",
        "Mennyiségi egység",
        "Kiskereskedelmi eladási ár",
        "Érvényesség kezdete",
    )
    rows = [
        CategorySeparator("Szivarka", col_index=1),
        {"name": "Moods 10", "quantity": 10, "unit_name": "szál", "price": 2190, "valid_from": "2026-11-26"},
        CategorySeparator("Egyéb fogyasztási dohány", col_index=1),
        {"name": "Alsbo Gold", "quantity": 50, "unit_name": "g", "price": 4500, "valid_from": "2026-11-26"},
    ]
    _setup_single_xlsx(mock_sztfh_server, rows, columns=cols)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    by_name = {i["name"]: i for i in res.load_prices_json()["items"]}
    assert by_name["Moods 10"]["category"] == "Szivarka"
    assert by_name["Alsbo Gold"]["category"] == "Egyéb fogyasztási dohány"


@pytest.mark.tier1
def test_t1_f7_fallback_egyeb_when_category_missing(mock_sztfh_server, scraper_runner) -> None:
    _setup_single_xlsx(mock_sztfh_server, [_sample_row("No Cat Item", category=None)])  # type: ignore[arg-type]
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert res.load_prices_json()["items"][0]["category"] == "Egyéb"


@pytest.mark.tier1
def test_t1_f7_preserves_hungarian_category_names_verbatim(
    mock_sztfh_server, scraper_runner
) -> None:
    cats = [
        "Tüsszentésre szolgáló dohánytermék (zárjegyes)",
        "Dohányzási célú gyógynövénytermék (zárjegyes)",
    ]
    rows = [_sample_row(f"Cat Item {idx}", c) for idx, c in enumerate(cats)]
    _setup_single_xlsx(mock_sztfh_server, rows)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert [i["category"] for i in res.load_prices_json()["items"]] == cats


# ---------------------------------------------------------------------------
# F8: Price-Diff Calculation (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_t1_f8_price_increase_diff_fields(mock_sztfh_server, scraper_runner) -> None:
    prior = build_prices_json(
        [
            make_catalog_item(
                name="Marlboro Red 20 szálas",
                category="Cigaretta",
                unit="20 szál",
                price=2300,
                valid_from="2026-10-01",
            )
        ],
        source_file="alkalmazando_old.xlsx",
    )
    _setup_single_xlsx(
        mock_sztfh_server,
        [_sample_row("Marlboro Red 20 szálas", "Cigaretta", 20, "szál", 2350, "2026-11-26")],
    )
    res = scraper_runner.run(mock_sztfh_server, seed_prices_json=prior)
    assert res.returncode == 0, res.stderr
    item = res.load_prices_json()["items"][0]
    assert item["price"] == 2350
    assert item["previous_price"] == 2300
    assert item["price_diff"] == 50
    assert item["is_new"] is False
    assert item["changed_at"] and re.match(r"^\d{4}-\d{2}-\d{2}$", item["changed_at"])


@pytest.mark.tier1
def test_t1_f8_price_decrease_diff_fields(mock_sztfh_server, scraper_runner) -> None:
    prior = build_prices_json(
        [
            make_catalog_item(
                name="Sopianae Piros 20",
                category="Cigaretta",
                unit="20 szál",
                price=2400,
                valid_from="2026-10-01",
            )
        ],
        source_file="alkalmazando_old.xlsx",
    )
    _setup_single_xlsx(
        mock_sztfh_server,
        [_sample_row("Sopianae Piros 20", "Cigaretta", 20, "szál", 2250, "2026-11-26")],
    )
    res = scraper_runner.run(mock_sztfh_server, seed_prices_json=prior)
    assert res.returncode == 0, res.stderr
    item = res.load_prices_json()["items"][0]
    assert item["price"] == 2250
    assert item["previous_price"] == 2400
    assert item["price_diff"] == -150
    assert item["is_new"] is False
    assert item["changed_at"] and re.match(r"^\d{4}-\d{2}-\d{2}$", item["changed_at"])


@pytest.mark.tier1
def test_t1_f8_unchanged_baseline_price_null_diffs(mock_sztfh_server, scraper_runner) -> None:
    prior = build_prices_json(
        [
            make_catalog_item(
                name="Camel Blue 20",
                category="Cigaretta",
                unit="20 szál",
                price=2000,
                valid_from="2026-10-01",
                previous_price=None,
                changed_at=None,
                is_new=False,
            )
        ],
        source_file="alkalmazando_old.xlsx",
    )
    _setup_single_xlsx(
        mock_sztfh_server,
        [_sample_row("Camel Blue 20", "Cigaretta", 20, "szál", 2000, "2026-11-26")],
    )
    res = scraper_runner.run(mock_sztfh_server, seed_prices_json=prior)
    assert res.returncode == 0, res.stderr
    item = res.load_prices_json()["items"][0]
    assert item["price"] == 2000
    assert item["previous_price"] is None
    assert item["price_diff"] is None
    assert item["changed_at"] is None
    assert item["is_new"] is False


@pytest.mark.tier1
def test_t1_f8_new_item_flagged_is_new_true(mock_sztfh_server, scraper_runner) -> None:
    prior = build_prices_json(
        [make_catalog_item(name="Item A", category="Cigaretta", unit="20 szál", price=2200)],
        source_file="alkalmazando_old.xlsx",
    )
    _setup_single_xlsx(
        mock_sztfh_server,
        [
            _sample_row("Item A", "Cigaretta", 20, "szál", 2200),
            _sample_row("Item B", "Cigaretta", 20, "szál", 2500),
        ],
    )
    res = scraper_runner.run(mock_sztfh_server, seed_prices_json=prior)
    assert res.returncode == 0, res.stderr
    by_name = {i["name"]: i for i in res.load_prices_json()["items"]}
    assert by_name["Item B"]["is_new"] is True
    assert by_name["Item B"]["previous_price"] is None
    assert by_name["Item B"]["price_diff"] is None


@pytest.mark.tier1
def test_t1_f8_changed_at_populated_and_diff_invariant_on_mixed_catalog(
    mock_sztfh_server, scraper_runner
) -> None:
    prior = build_prices_json(
        [
            make_catalog_item(name="Inc Item", category="Cigaretta", unit="20 szál", price=2000),
            make_catalog_item(name="Dec Item", category="Cigaretta", unit="20 szál", price=2500),
            make_catalog_item(name="Same Item", category="Cigaretta", unit="20 szál", price=2200),
            make_catalog_item(name="Other Item", category="Szivarka", unit="10 szál", price=1800),
        ],
        source_file="alkalmazando_old.xlsx",
    )
    _setup_single_xlsx(
        mock_sztfh_server,
        [
            _sample_row("Inc Item", "Cigaretta", 20, "szál", 2150, "2026-11-26"),
            _sample_row("Dec Item", "Cigaretta", 20, "szál", 2400, "2026-11-26"),
            _sample_row("Same Item", "Cigaretta", 20, "szál", 2200, "2026-10-01"),
            _sample_row("Added Item", "Szivar", 1, "szál", 4900, "2026-11-26"),
        ],
    )
    res = scraper_runner.run(mock_sztfh_server, seed_prices_json=prior)
    assert res.returncode == 0, res.stderr
    data = res.load_prices_json()
    validate_prices_schema(data)
    by_name = {i["name"]: i for i in data["items"]}
    assert by_name["Inc Item"]["price_diff"] == 150
    assert by_name["Dec Item"]["price_diff"] == -100
    assert by_name["Same Item"]["price_diff"] is None
    assert by_name["Added Item"]["is_new"] is True
    for name in ("Inc Item", "Dec Item"):
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", str(by_name[name]["changed_at"]))


# ---------------------------------------------------------------------------
# F9: Idempotent data/prices.json & meta (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_t1_f9_fresh_creation_when_prices_json_missing(
    mock_sztfh_server, scraper_runner
) -> None:
    rows = [_sample_row(f"Fresh {idx}") for idx in range(3)]
    _setup_single_xlsx(mock_sztfh_server, rows)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    assert res.prices_json_path.exists()
    data = res.load_prices_json()
    validate_prices_schema(data)
    assert len(data["items"]) == 3
    for item in data["items"]:
        assert item["is_new"] is False
        assert item["previous_price"] is None
        assert item["price_diff"] is None
        assert item["changed_at"] is None


@pytest.mark.tier1
def test_t1_f9_meta_contains_all_required_fields(mock_sztfh_server, scraper_runner) -> None:
    _setup_single_xlsx(mock_sztfh_server, [_sample_row()])
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    data = res.load_prices_json()
    validate_prices_schema(data, require_page_url=True)
    meta = data["meta"]
    datetime.fromisoformat(meta["last_updated"].replace("Z", "+00:00"))
    assert meta["source_file"] == DEFAULT_SOURCE_FILE
    assert meta["source_url"] == f"https://sztfh.hu/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"
    assert meta["page_url"] == SZTFH_PAGE_URL
    assert meta["total_count"] == 1


@pytest.mark.tier1
def test_t1_f9_meta_total_count_equals_len_items(mock_sztfh_server, scraper_runner) -> None:
    catalog = build_realistic_catalog(count=25)
    rows = [
        {
            "name": item["name"],
            "category": item["category"],
            "unit": item["unit"],
            "price": item["price"],
            "valid_from": item["valid_from"],
        }
        for item in catalog["items"]
    ]
    _setup_single_xlsx(mock_sztfh_server, rows, columns=MINIMAL_XLSX_COLUMNS)
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    data = res.load_prices_json()
    validate_prices_schema(data)
    assert data["meta"]["total_count"] == len(data["items"]) == 25


@pytest.mark.tier1
def test_t1_f9_valid_utf8_json_with_hungarian_accents(
    mock_sztfh_server, scraper_runner
) -> None:
    name = "Őszi Szűz Őrölt Üveg Áfonya Édes Íz Óra Öt Út"
    _setup_single_xlsx(mock_sztfh_server, [_sample_row(name)])
    res = scraper_runner.run(mock_sztfh_server)
    assert res.returncode == 0, res.stderr
    parsed = json.loads(res.prices_json_path.read_text(encoding="utf-8"))
    assert parsed["items"][0]["name"] == name


@pytest.mark.tier1
def test_t1_f9_idempotent_rerun_preserves_mtime_and_last_updated(
    mock_sztfh_server, scraper_runner, tmp_path: Path
) -> None:
    rows = [_sample_row("Marlboro Red 20 szálas", "Cigaretta", 20, "szál", 2350, "2026-11-26")]
    _setup_single_xlsx(mock_sztfh_server, rows)
    workdir = tmp_path / "idem_work"

    res1 = scraper_runner.run(mock_sztfh_server, workdir=workdir)
    assert res1.returncode == 0, res1.stderr

    data1 = res1.load_prices_json()
    data1["meta"]["last_updated"] = "2026-01-01T00:00:00Z"
    res1.prices_json_path.write_text(
        json.dumps(data1, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.utime(res1.prices_json_path, (1700000000, 1700000000))
    bytes_before = res1.prices_json_path.read_bytes()
    mtime_before = res1.prices_json_path.stat().st_mtime_ns

    res2 = scraper_runner.run(mock_sztfh_server, workdir=workdir)
    assert res2.returncode == 0, res2.stderr
    assert res2.raw_bytes == bytes_before
    assert res2.mtime_ns == mtime_before
    assert res2.load_prices_json()["meta"]["last_updated"] == "2026-01-01T00:00:00Z"
