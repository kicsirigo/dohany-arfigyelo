from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import date, datetime
from pathlib import Path

import openpyxl
import pytest
import requests

from conftest import start_static_server
from helpers import (
    DEFAULT_SOURCE_FILE,
    DEFAULT_XLSX_COLUMNS,
    SZTFH_PAGE_URL,
    CategorySeparator,
    EmptyRow,
    MockSztfhLink,
    RawRow,
    WhitespaceRow,
    assert_min_touch_targets,
    assert_no_horizontal_overflow,
    assert_product_highlighted,
    build_corrupt_xlsx_bytes,
    build_empty_xlsx_bytes,
    build_large_catalog,
    build_realistic_catalog,
    build_sztfh_html,
    build_xlsx_bytes,
    check_horizontal_overflow,
    check_touch_targets,
    clear_search,
    compute_item_id,
    count_rendered_products,
    detect_active_view,
    encode_excel_date,
    get_current_theme,
    get_favorites_count_from_tab,
    get_favorites_tab,
    get_grid_column_count,
    get_load_more_button,
    get_mark_texts,
    get_max_content_width,
    get_reset_filters_button,
    get_retry_button,
    get_stored_favorites,
    get_stored_theme,
    get_stored_view,
    get_sztfh_source_link,
    get_theme_toggle,
    parse_result_counter,
    search_products,
    select_category,
    select_sort,
    set_price_change_filter,
    set_stored_favorites,
    set_view_mode,
    to_excel_serial,
    toggle_favorite,
    validate_prices_schema,
    write_prices_json,
    write_xlsx,
)


@pytest.mark.selftest
def test_venv_dependencies_and_browser_launch(browser) -> None:
    import bs4  # noqa: F401
    import openpyxl as xl  # noqa: F401
    import playwright.sync_api  # noqa: F401
    import pytest as pt  # noqa: F401
    import requests as req  # noqa: F401

    assert browser.is_connected()
    assert browser.browser_type.name in ("chromium", "firefox")


@pytest.mark.selftest
def test_app_base_url_serves_directory_and_respects_override(app_base_url: str) -> None:
    resp = requests.get(app_base_url, timeout=5)
    assert resp.status_code == 200
    assert "no-store" in resp.headers.get("Cache-Control", "").lower()
    override_dir = os.environ.get("DOHANY_E2E_APP_DIR")
    if override_dir:
        idx = Path(override_dir) / "index.html"
        if idx.exists():
            assert idx.read_text(encoding="utf-8") in resp.text


@pytest.mark.selftest
def test_static_server_viewports_and_route_prices_json(
    tmp_path: Path,
    desktop_page,
    tablet_page,
    mobile_page,
    route_prices_json,
) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "prices.json").write_text(
        json.dumps({"items": [{"name": "Disk Item"}]}),
        encoding="utf-8",
    )
    (tmp_path / "index.html").write_text(
        """<!doctype html>
<html lang="hu">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body>
  <div id="out">loading</div>
  <script>
    fetch('./data/prices.json')
      .then(r => {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(d => { document.getElementById('out').textContent = d.items[0].name; })
      .catch(e => { document.getElementById('out').textContent = 'ERR:' + e.message; });
  </script>
</body>
</html>""",
        encoding="utf-8",
    )

    with start_static_server(tmp_path) as base_url:
        # Verify viewports
        desktop_page.goto(base_url)
        assert desktop_page.evaluate("() => [window.innerWidth, window.innerHeight]") == [1440, 900]
        assert desktop_page.locator("#out").inner_text() == "Disk Item"

        tablet_page.goto(base_url)
        assert tablet_page.evaluate("() => [window.innerWidth, window.innerHeight]") == [768, 1024]

        mobile_page.goto(base_url)
        assert mobile_page.evaluate("() => [window.innerWidth, window.innerHeight]") == [375, 812]

        # Route override with JSON dict
        route_prices_json(desktop_page, {"items": [{"name": "Teszt ŐŰ"}]})
        desktop_page.reload()
        assert desktop_page.locator("#out").inner_text() == "Teszt ŐŰ"

        # Route override with malformed JSON string
        route_prices_json(desktop_page, "{bad json")
        desktop_page.reload()
        assert desktop_page.locator("#out").inner_text().startswith("ERR:")

        # Route override with HTTP 500 (must not fail console teardown)
        route_prices_json(desktop_page, 500)
        desktop_page.reload()
        assert desktop_page.locator("#out").inner_text() == "ERR:HTTP 500"

        # Route override with abort (must not fail console teardown)
        route_prices_json(tablet_page, "abort")
        tablet_page.reload()
        assert tablet_page.locator("#out").inner_text().startswith("ERR:")


@pytest.mark.selftest
def test_console_and_pageerror_collectors(desktop_page) -> None:
    desktop_page.set_content("<h1>Collector Selftest</h1>")
    desktop_page.evaluate(
        """() => {
            console.error('selftest console error');
            setTimeout(() => { throw new Error('selftest uncaught'); }, 0);
        }"""
    )
    desktop_page.wait_for_timeout(100)
    assert any("selftest console error" in e for e in desktop_page._e2e_console_errors)
    assert any("selftest uncaught" in e for e in desktop_page._e2e_page_errors)
    # Clear intentionally triggered errors so fixture teardown passes cleanly
    desktop_page._e2e_console_errors.clear()
    desktop_page._e2e_page_errors.clear()


@pytest.mark.selftest
def test_helpers_xlsx_builder_roundtrip(tmp_path: Path) -> None:
    assert to_excel_serial(date(2026, 10, 1)) == 46296
    assert encode_excel_date(date(2026, 11, 26), "iso") == "2026-11-26"
    assert encode_excel_date(date(2026, 11, 26), "hu_dot") == "2026.11.26"
    assert encode_excel_date(date(2026, 10, 1), "serial") == 46296
    assert isinstance(encode_excel_date(date(2026, 10, 1), "datetime"), datetime)
    assert isinstance(encode_excel_date(datetime(2026, 10, 1, 12, 0), "date"), date)

    raw_name = "  Marlboro   Red\t20 szálas  "
    xlsx_path = write_xlsx(
        tmp_path / "nested" / "sample.xlsx",
        [
            CategorySeparator("Cigaretta"),
            EmptyRow(2),
            WhitespaceRow(("  ", "\t")),
            {
                "name": raw_name,
                "category": "Cigaretta",
                "quantity": 20.0,
                "unit_name": "szál",
                "price": 2350,
                "valid_from": 46296,
            },
            {
                "name": raw_name,
                "category": "Cigaretta",
                "quantity": 19.5,
                "unit_name": "g",
                "price": 4100,
                "valid_from": "2026-11-26",
            },
            RawRow(["raw1", "raw2"]),
        ],
        columns=("Termék megnevezése", "Termék típus", "Mennyiség", "Mennyiségi egység", "Kiskereskedelmi eladási ár", "Érvényesség kezdete"),
        header_row=4,
        preamble_rows=["Title Banner", ["Sub", "2026"]],
        sheet_name="Árlista",
        extra_sheets={"Meta": [["k", "v"]]},
    )

    wb = openpyxl.load_workbook(xlsx_path)
    assert wb.sheetnames == ["Árlista", "Meta"]
    ws = wb["Árlista"]
    assert ws.cell(1, 1).value == "Title Banner"
    assert ws.cell(2, 2).value == "2026"
    assert ws.cell(4, 1).value == "Termék megnevezése"
    assert ws.cell(5, 1).value == "Cigaretta"
    assert ws.cell(6, 1).value is None
    assert ws.cell(7, 1).value is None
    assert ws.cell(8, 1).value == "  "
    assert ws.cell(9, 1).value == raw_name  # whitespace preserved verbatim
    assert ws.cell(9, 3).value == 20.0
    assert ws.cell(10, 3).value == 19.5
    assert ws.cell(11, 1).value == "raw1"

    with pytest.raises(ValueError):
        build_xlsx_bytes([], header_row=0)
    with pytest.raises(ValueError):
        build_xlsx_bytes([], header_row=2, preamble_rows=["r1", "r2"])

    for mode in ("blank_sheet", "headers_only", "preamble_only"):
        empty_b = build_empty_xlsx_bytes(mode)  # type: ignore[arg-type]
        wb_e = openpyxl.load_workbook(io.BytesIO(empty_b))
        assert "Munka1" in wb_e.sheetnames

    assert build_corrupt_xlsx_bytes("empty_bytes") == b""
    assert b"not-a-valid" in build_corrupt_xlsx_bytes("garbage")
    assert build_corrupt_xlsx_bytes("truncated_zip").startswith(b"PK")
    missing_sheet = build_corrupt_xlsx_bytes("missing_sheet_xml")
    with zipfile.ZipFile(io.BytesIO(missing_sheet), "r") as zf:
        assert "xl/worksheets/sheet1.xml" not in zf.namelist()


@pytest.mark.selftest
def test_helpers_sztfh_html_and_catalog_builders(tmp_path: Path) -> None:
    html_default = build_sztfh_html()
    assert DEFAULT_SOURCE_FILE in html_default
    assert "valtozassal" in html_default
    assert "2017_januar" in html_default

    html_no_xlsx = build_sztfh_html(no_xlsx=True)
    assert ".xlsx" not in html_no_xlsx.lower()

    html_custom = build_sztfh_html(
        links_override=[MockSztfhLink(href="/a.xlsx", text="A", attrs={"data-kind": "full"})],
        base_url="https://sztfh.hu",
    )
    assert "https://sztfh.hu/a.xlsx" in html_custom
    assert 'data-kind="full"' in html_custom

    small = build_realistic_catalog(count=22)
    validate_prices_schema(small, require_page_url=True)
    assert small["meta"]["total_count"] == 22
    assert any("ő" in i["name"].lower() or "ű" in i["name"].lower() for i in small["items"])
    assert any((i["price_diff"] or 0) > 0 for i in small["items"])
    assert any((i["price_diff"] or 0) < 0 for i in small["items"])
    assert any(i["is_new"] for i in small["items"])

    large = build_large_catalog(4610)
    validate_prices_schema(large, require_page_url=True)
    assert len(large["items"]) == 4610
    assert compute_item_id("Marlboro Red 20 szálas", "20 szál") == small["items"][0]["id"]

    json_path = write_prices_json(tmp_path / "prices.json", small)
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    validate_prices_schema(loaded)


@pytest.mark.selftest
def test_helpers_ui_semantic_helpers_against_synthetic_fixture(
    tmp_path: Path, desktop_page, mobile_page
) -> None:
    # Build a synthetic compliant HTML fixture in tmp_path exercising all UI helper contracts
    (tmp_path / "index.html").write_text(
        """<!doctype html>
<html lang="hu" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; font-family: sans-serif; }
    main { max-width: 1280px; margin: 0 auto; padding: 12px; }
    button, input, select { min-width: 44px; min-height: 44px; }
    .grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
    @media (max-width: 639px) { .grid { grid-template-columns: 1fr; } }
    .hidden { display: none; }
  </style>
</head>
<body>
  <header>
    <button id="themeBtn" type="button">Téma váltása</button>
    <button id="viewGridBtn" type="button">Kártya nézet</button>
    <button id="viewTableBtn" type="button">Táblázat nézet</button>
  </header>
  <main>
    <div>
      <input id="searchInput" type="search" placeholder="Keresés pontos névre...">
      <button id="clearBtn" type="button" aria-label="Keresés törlése">×</button>
    </div>
    <div>
      <button type="button" class="cat" data-cat="Összes">Összes (3)</button>
      <button type="button" class="cat" data-cat="Cigaretta">Cigaretta (2)</button>
      <button type="button" role="tab" id="favTab">Kedvencek (1)</button>
      <label><input id="chgToggle" type="checkbox"> Csak árváltozások</label>
      <select id="sortSel" aria-label="Rendezés">
        <option value="name-asc">Név (A–Z)</option>
        <option value="name-desc">Név (Z–A)</option>
        <option value="price-asc">Ár növekvő</option>
        <option value="price-desc">Ár csökkenő</option>
        <option value="changed-desc">Legutóbb változott</option>
      </select>
    </div>
    <div id="counter">3 / 4\u00a0610 termék</div>
    <div id="gridView" class="grid">
      <article class="product-card" data-product-id="id1">
        <h3><mark>Őszi</mark> Szűz Fűszeres</h3>
        <button type="button" aria-pressed="false" aria-label="Kedvenc">☆</button>
      </article>
      <article class="product-card" data-product-id="id2">
        <h3>Marlboro Red 20</h3>
        <button type="button" aria-pressed="true" aria-label="Kedvenc">★</button>
      </article>
      <article class="product-card" data-product-id="id3">
        <h3>TEREA Amber</h3>
        <button type="button" aria-pressed="false" aria-label="Kedvenc">☆</button>
      </article>
    </div>
    <table id="tableView" class="hidden">
      <tbody>
        <tr><td>Őszi Szűz Fűszeres</td><td><button type="button" aria-pressed="false" aria-label="Kedvenc">☆</button></td></tr>
        <tr><td>Marlboro Red 20</td><td><button type="button" aria-pressed="true" aria-label="Kedvenc">★</button></td></tr>
      </tbody>
    </table>
    <div id="emptyState">
      <button id="resetBtn" type="button">Szűrők és keresés törlése</button>
    </div>
    <button id="loadMoreBtn" type="button">Továbbiak betöltése</button>
    <button id="retryBtn" type="button">Újrapróbálás</button>
  </main>
  <footer><a href="https://sztfh.hu/tevekenysegek/">SZTFH</a></footer>
  <script>
    const favKey = 'dohany_favorites';
    const themeKey = 'dohany_theme';
    const viewKey = 'dohany_view';
    document.getElementById('clearBtn').addEventListener('click', () => {
      document.getElementById('searchInput').value = '';
    });
    document.getElementById('themeBtn').addEventListener('click', () => {
      const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      localStorage.setItem(themeKey, next);
    });
    document.getElementById('viewTableBtn').addEventListener('click', () => {
      document.getElementById('gridView').classList.add('hidden');
      document.getElementById('tableView').classList.remove('hidden');
      localStorage.setItem(viewKey, 'table');
    });
    document.getElementById('viewGridBtn').addEventListener('click', () => {
      document.getElementById('tableView').classList.add('hidden');
      document.getElementById('gridView').classList.remove('hidden');
      localStorage.setItem(viewKey, 'grid');
    });
    document.querySelectorAll('article button[aria-pressed]').forEach(btn => {
      btn.addEventListener('click', () => {
        const cur = btn.getAttribute('aria-pressed') === 'true';
        btn.setAttribute('aria-pressed', cur ? 'false' : 'true');
        localStorage.setItem(favKey, JSON.stringify(['id1']));
      });
    });
  </script>
</body>
</html>""",
        encoding="utf-8",
    )

    with start_static_server(tmp_path) as base_url:
        desktop_page.goto(base_url)
        assert_no_horizontal_overflow(desktop_page)
        assert check_horizontal_overflow(desktop_page)["has_overflow"] is False
        assert_min_touch_targets(desktop_page)
        assert check_touch_targets(desktop_page) == []

        max_w = get_max_content_width(desktop_page)
        assert max_w is not None and max_w <= 1280.0
        assert get_grid_column_count(desktop_page) == 3

        assert parse_result_counter(desktop_page) == (3, 4610)
        assert count_rendered_products(desktop_page) == 3
        assert get_mark_texts(desktop_page) == ["Őszi"]
        assert_product_highlighted(desktop_page, "Őszi Szűz Fűszeres", ["Őszi"])

        search_products(desktop_page, "oszi", wait_ms=50)
        clear_search(desktop_page, wait_ms=50)
        assert desktop_page.locator("#searchInput").input_value() == ""

        select_category(desktop_page, "Cigaretta")
        set_price_change_filter(desktop_page, True)
        set_price_change_filter(desktop_page, False)
        select_sort(desktop_page, "changed-desc")
        assert desktop_page.locator("#sortSel").input_value() == "changed-desc"

        assert get_favorites_tab(desktop_page).is_visible()
        assert get_favorites_count_from_tab(desktop_page) == 1
        assert toggle_favorite(desktop_page, "Őszi Szűz Fűszeres") is True
        assert get_stored_favorites(desktop_page) == ["id1"]
        set_stored_favorites(desktop_page, ["id1", "id2"])
        assert get_stored_favorites(desktop_page) == ["id1", "id2"]

        assert get_current_theme(desktop_page) == "dark"
        get_theme_toggle(desktop_page).click()
        assert get_current_theme(desktop_page) == "light"
        assert get_stored_theme(desktop_page) == "light"

        assert detect_active_view(desktop_page) == "grid"
        set_view_mode(desktop_page, "table")
        assert detect_active_view(desktop_page) == "table"
        assert get_stored_view(desktop_page) == "table"
        assert count_rendered_products(desktop_page) == 2

        assert get_reset_filters_button(desktop_page).is_visible()
        assert get_load_more_button(desktop_page).is_visible()
        assert get_retry_button(desktop_page).is_visible()
        assert get_sztfh_source_link(desktop_page).is_visible()

        mobile_page.goto(base_url)
        assert_no_horizontal_overflow(mobile_page)
        assert_min_touch_targets(mobile_page)
        assert get_grid_column_count(mobile_page) == 1


@pytest.mark.selftest
def test_scraper_runner_intercepts_requests_and_urllib_opaquely(
    tmp_path: Path,
    mock_sztfh_server,
    scraper_runner,
) -> None:
    mock_sztfh_server.require_custom_ua = True
    mock_sztfh_server.set_html(
        build_sztfh_html(
            alkalmazando_links=[f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}"]
        )
    )
    mock_sztfh_server.set_redirect(
        "/downloads/redirect.xlsx",
        f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}",
    )
    mock_sztfh_server.set_xlsx(
        f"/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}",
        build_xlsx_bytes(
            [
                {
                    "name": "Marlboro Red 20 szálas",
                    "category": "Cigaretta",
                    "quantity": 20,
                    "unit_name": "szál",
                    "price": 2350,
                    "valid_from": "2026-11-26",
                }
            ]
        ),
    )

    dummy_script = tmp_path / "dummy_scraper.py"
    dummy_script.write_text(
        f"""
import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin
import requests

assert "run_" in str(Path(__file__).resolve()), f"__file__ not isolated: {{__file__}}"

headers = {{"User-Agent": "DohanySelftest/1.0"}}
r_html = requests.get("{SZTFH_PAGE_URL}", headers=headers, timeout=5)
r_html.raise_for_status()
assert r_html.url == "{SZTFH_PAGE_URL}", r_html.url

xlsx_url = urljoin(r_html.url, "/downloads/redirect.xlsx")
r_xlsx = requests.get(xlsx_url, headers=headers, timeout=5)
r_xlsx.raise_for_status()
assert r_xlsx.url == "https://sztfh.hu/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}", r_xlsx.url

req_u = urllib.request.Request("https://sztfh.hu/downloads/dohany/arkozzetetel/{DEFAULT_SOURCE_FILE}", headers=headers)
with urllib.request.urlopen(req_u, timeout=5) as u_resp:
    assert u_resp.status == 200
    assert u_resp.geturl().startswith("https://sztfh.hu/")

try:
    urllib.request.urlopen(urllib.request.Request("https://sztfh.hu/missing-404", headers=headers), timeout=5)
    raise AssertionError("Expected 404 HTTPError")
except urllib.error.HTTPError as exc:
    assert exc.code == 404
    assert exc.url == "https://sztfh.hu/missing-404", exc.url

try:
    requests.get("https://example.com/blocked", timeout=2)
    raise AssertionError("Expected external host block")
except Exception:
    pass

out = Path("data") / "prices.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({{"ok": True, "bytes": len(r_xlsx.content)}}), encoding="utf-8")
""",
        encoding="utf-8",
    )

    res = scraper_runner.run(mock_sztfh_server, script_path=dummy_script)
    assert res.returncode == 0, f"stderr: {res.stderr}\nstdout: {res.stdout}"
    data = res.load_prices_json()
    assert data["ok"] is True and data["bytes"] > 100
    assert len(res.requests) >= 4
    assert all(req["user_agent"] == "DohanySelftest/1.0" for req in res.requests)
