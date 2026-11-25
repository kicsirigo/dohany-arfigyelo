from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import pytest
from playwright.sync_api import Locator, Page, expect

from helpers import (
    assert_min_touch_targets,
    assert_no_horizontal_overflow,
    build_large_catalog,
    build_realistic_catalog,
    build_sztfh_html,
    build_xlsx_bytes,
    clear_search,
    compute_item_id,
    count_rendered_products,
    detect_active_view,
    get_current_theme,
    get_favorites_count_from_tab,
    get_favorites_tab,
    get_grid_column_count,
    get_load_more_button,
    get_mark_texts,
    get_max_content_width,
    get_result_counter_locator,
    get_search_input,
    get_stored_favorites,
    get_stored_theme,
    get_stored_view,
    get_sztfh_source_link,
    get_theme_toggle,
    parse_result_counter,
    search_products,
    select_category,
    set_price_change_filter,
    toggle_favorite,
    validate_prices_schema,
)

APP_ROOT = Path("/home/mate/dohany_app")
_REALISTIC_25 = build_realistic_catalog(count=25)
_LARGE_4610 = build_large_catalog(4610)

_SORT_PATTERNS: dict[str, re.Pattern[str]] = {
    "name-asc": re.compile(r"(?:n[eé]v.*A\s*[-–]\s*Z|A\s*[-–]\s*Z)", re.I),
    "name-desc": re.compile(r"(?:n[eé]v.*Z\s*[-–]\s*A|Z\s*[-–]\s*A)", re.I),
    "price-asc": re.compile(r"[aá]r.*n[oö]vekv[oő]", re.I),
    "price-desc": re.compile(r"[aá]r.*cs[oö]kken[oő]", re.I),
    "changed-desc": re.compile(r"legut[oó]bb\s+v[aá]ltozott", re.I),
}


def _wait_for_catalog_ready(page: Page, *, expect_nonzero: bool = True) -> None:
    counter = get_result_counter_locator(page)
    expect(counter).to_be_visible(timeout=15000)
    if expect_nonzero:
        expect(counter).not_to_have_text(re.compile(r"^\s*0\s*/\s*0\s*term[eé]k\s*$", re.I), timeout=15000)


def _load_catalog(
    page: Page,
    base_url: str,
    route_prices_json: Any,
    payload: dict[str, Any] | None = None,
) -> None:
    if payload is not None:
        route_prices_json(page, payload)
    page.goto(base_url, wait_until="domcontentloaded")
    expect_nonzero = (
        isinstance(payload, dict) and len(payload.get("items", [])) > 0
    ) or payload is None
    _wait_for_catalog_ready(page, expect_nonzero=expect_nonzero)


def _select_sort(
    page: Page,
    mode: Literal["name-asc", "name-desc", "price-asc", "price-desc", "changed-desc"],
) -> None:
    sel = (
        page.locator("select")
        .filter(has=page.locator("option", has_text=re.compile(r"Legut[oó]bb\s+v[aá]ltozott", re.I)))
        .or_(page.get_by_role("combobox", name=re.compile(r"^\s*Rendez[eé]s\s*$", re.I)))
        .first
    )
    pattern = _SORT_PATTERNS[mode]
    opt_value = sel.evaluate(
        """(el, patSource) => {
            const rx = new RegExp(patSource, 'i');
            for (const opt of el.options) {
                if (rx.test(opt.textContent || '') || rx.test(opt.value || '')) return opt.value;
            }
            return null;
        }""",
        pattern.pattern,
    )
    assert opt_value is not None, f"No sort <option> matching {mode!r}"
    sel.select_option(opt_value)


def _set_view_mode(page: Page, mode: Literal["grid", "table"]) -> None:
    if detect_active_view(page) == mode:
        return
    if mode == "grid":
        btn = page.get_by_role("button", name=re.compile(r"(k[aá]rty[aá]|r[aá]cs|grid)", re.I)).first
    else:
        btn = page.get_by_role("button", name=re.compile(r"(t[aá]bl[aá]zat|lista|table)", re.I)).first
    if btn.count() > 0 and btn.is_visible():
        btn.click()
    else:
        page.get_by_role(
            "button", name=re.compile(r"(k[aá]rty[aá]|r[aá]cs|t[aá]bl[aá]zat|lista|n[eé]zet)", re.I)
        ).first.click()


def _visible_product_container(page: Page, product_name: str) -> Locator:
    main_or_body = page.locator("main").or_(page.locator("body")).first
    star_btn_filter = page.locator(
        "button[aria-pressed]:not([role='tab']), "
        "button[aria-label*='kedvenc' i]:not([role='tab']), "
        "button[title*='kedvenc' i]:not([role='tab']), "
        "button:has-text('★'):not([role='tab']), "
        "button:has-text('☆'):not([role='tab'])"
    )
    rows = main_or_body.locator("table:visible tbody tr:visible").filter(has_text=product_name)
    if rows.count() > 0:
        return rows.first
    cards = (
        main_or_body.locator(
            "article:visible, li:visible, [data-product-id]:visible, "
            ".product-card:visible, .card:visible"
        )
        .filter(has_text=product_name)
        .filter(has=star_btn_filter)
    )
    if cards.count() > 0:
        return cards.first
    return (
        main_or_body.locator("div:visible")
        .filter(has_text=product_name)
        .filter(has=star_btn_filter)
        .last
    )


def _get_visible_product_texts(page: Page) -> list[str]:
    return list(
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
                const isStarBtn = (btn) => {
                    if (!isVis(btn) || isFavTab(btn)) return false;
                    const label = (btn.getAttribute('aria-label') || '') + ' ' + (btn.textContent || '');
                    return btn.hasAttribute('aria-pressed') || /kedvenc|★|☆/i.test(label);
                };
                const tables = Array.from(document.querySelectorAll('table')).filter(isVis);
                for (const tbl of tables) {
                    const rows = Array.from(tbl.querySelectorAll('tbody tr')).filter(isVis);
                    if (rows.length > 0) return rows.map(r => r.textContent || '');
                }
                const root = document.querySelector('main') || document.body;
                const stars = Array.from(root.querySelectorAll('button')).filter(isStarBtn);
                const seen = [];
                for (const btn of stars) {
                    let card = btn.parentElement ? btn.parentElement.closest('article, li, .product-card, .card') : null;
                    if (!card) {
                        let cur = btn.parentElement;
                        while (cur && cur !== root && cur.parentElement && cur.parentElement !== root) {
                            const insideStars = Array.from(cur.parentElement.querySelectorAll('button')).filter(isStarBtn);
                            if (insideStars.length === 1) cur = cur.parentElement;
                            else break;
                        }
                        card = cur;
                    }
                    if (card && !seen.includes(card)) seen.push(card);
                }
                return seen.map(c => c.textContent || '');
            }"""
        )
    )


# ---------------------------------------------------------------------------
# Scenario 1: Mobile shopper searches accented/unaccented Hungarian brands
# ---------------------------------------------------------------------------


@pytest.mark.tier4
def test_t4_scenario1_mobile_shopper_hungarian_search_and_touch_ux(
    mobile_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(mobile_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    assert get_grid_column_count(mobile_page) == 1
    assert_no_horizontal_overflow(mobile_page)
    assert_min_touch_targets(mobile_page, min_size=44.0)

    # Search unaccented Hungarian phrase
    search_products(mobile_page, "oszi szuz")
    assert parse_result_counter(mobile_page) == (1, 25)
    oszi_card = _visible_product_container(
        mobile_page, "Őszi Szűz Fűszeres Pipadohány 50 g Extra Hosszú Kiszerelésben"
    )
    expect(oszi_card).to_be_visible()
    marks = get_mark_texts(mobile_page, oszi_card)
    assert "Őszi" in marks and "Szűz" in marks, marks

    # Clear with × button and search order-independent multi-word query
    clear_search(mobile_page)
    assert get_search_input(mobile_page).input_value() == ""
    assert parse_result_counter(mobile_page) == (25, 25)

    search_products(mobile_page, "red marlboro")
    assert parse_result_counter(mobile_page)[0] >= 1
    marl_card = _visible_product_container(mobile_page, "Marlboro Red 20 szálas")
    marl_marks = [m.lower() for m in get_mark_texts(mobile_page, marl_card)]
    assert any("marlboro" in m for m in marl_marks) and any("red" in m for m in marl_marks)

    clear_search(mobile_page)
    mobile_page.evaluate("() => window.scrollTo(0, 600)")
    mobile_page.wait_for_timeout(100)
    search_box = get_search_input(mobile_page).bounding_box()
    assert search_box is not None and 0 <= search_box["y"] < 220
    assert_no_horizontal_overflow(mobile_page)


# ---------------------------------------------------------------------------
# Scenario 2: Favorites across categories + reload persistence
# ---------------------------------------------------------------------------


@pytest.mark.tier4
def test_t4_scenario2_favorites_across_categories_and_reload_persistence(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))

    fav_names = [
        ("Marlboro Red 20 szálas", "20 szál"),
        ("TEREA Amber", "20 szál"),
        ("VELO Freezing Peppermint 14 g", "14 g"),
    ]
    for name, _ in fav_names:
        assert toggle_favorite(desktop_page, name) is True

    assert get_favorites_count_from_tab(desktop_page) == 3
    get_favorites_tab(desktop_page).click()
    assert parse_result_counter(desktop_page) == (3, 25)

    expect(
        _visible_product_container(desktop_page, "Marlboro Red 20 szálas").get_by_text(
            re.compile(r"\+100\s*Ft")
        ).first
    ).to_be_visible()
    expect(
        _visible_product_container(desktop_page, "VELO Freezing Peppermint 14 g").get_by_text(
            re.compile(r"^\s*[ÚU]j\s*$")
        ).first
    ).to_be_visible()

    # Reload browser and confirm full persistence
    desktop_page.reload(wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)

    expected_ids = {compute_item_id(n, u) for n, u in fav_names}
    assert set(get_stored_favorites(desktop_page)) == expected_ids
    assert get_favorites_count_from_tab(desktop_page) == 3

    get_favorites_tab(desktop_page).click()
    assert parse_result_counter(desktop_page) == (3, 25)

    # Remove one favorite while filtered to Kedvencek and confirm live update
    terea_card = _visible_product_container(desktop_page, "TEREA Amber")
    terea_star = terea_card.locator("button[aria-pressed]").or_(
        terea_card.get_by_role("button", name=re.compile(r"(kedvenc|★|☆)", re.I))
    ).first
    terea_star.click()
    assert compute_item_id("TEREA Amber", "20 szál") not in get_stored_favorites(desktop_page)
    assert get_favorites_count_from_tab(desktop_page) == 2
    assert parse_result_counter(desktop_page) == (2, 25)


# ---------------------------------------------------------------------------
# Scenario 3: Desktop user toggles light/dark theme and grid/table view
# ---------------------------------------------------------------------------


@pytest.mark.tier4
def test_t4_scenario3_desktop_theme_and_view_toggle_persistence(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    max_w = get_max_content_width(desktop_page)
    assert max_w is not None and max_w <= 1280.0
    assert get_current_theme(desktop_page) == "dark"
    assert detect_active_view(desktop_page) == "grid"

    get_theme_toggle(desktop_page).click()
    _set_view_mode(desktop_page, "table")
    assert get_current_theme(desktop_page) == "light"
    assert detect_active_view(desktop_page) == "table"

    desktop_page.reload(wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)
    assert get_current_theme(desktop_page) == "light"
    assert get_stored_theme(desktop_page) == "light"
    assert detect_active_view(desktop_page) == "table"
    assert get_stored_view(desktop_page) == "table"
    assert_no_horizontal_overflow(desktop_page)

    # Toggle back to dark & grid
    get_theme_toggle(desktop_page).click()
    _set_view_mode(desktop_page, "grid")
    assert get_current_theme(desktop_page) == "dark"
    assert detect_active_view(desktop_page) == "grid"


# ---------------------------------------------------------------------------
# Scenario 4: Price-watcher inspects price changes, badges & sorting
# ---------------------------------------------------------------------------


@pytest.mark.tier4
def test_t4_scenario4_price_watcher_changes_and_sorting(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    set_price_change_filter(desktop_page, True)
    expected_changed_count = sum(
        1
        for item in _REALISTIC_25["items"]
        if (item["price_diff"] is not None and item["price_diff"] != 0) or item["is_new"]
    )
    assert parse_result_counter(desktop_page) == (expected_changed_count, 25)

    _select_sort(desktop_page, "price-desc")
    texts_desc = _get_visible_product_texts(desktop_page)
    assert "Őszi Szűz Fűszeres Pipadohány 50 g Extra Hosszú Kiszerelésben" in texts_desc[0]  # 5490 Ft
    assert "VELO Freezing Peppermint 14 g" in texts_desc[-1]  # 1890 Ft

    # Inspect increase, decrease, and strikethrough prior prices
    marl = _visible_product_container(desktop_page, "Marlboro Red 20 szálas")
    assert re.search(r"\+100\s*Ft", marl.inner_text())
    assert marl.evaluate(
        """el => Array.from(el.querySelectorAll('*')).some(n =>
            /2[\\s\\u00a0\\u202f]?350\\s*Ft/.test(n.textContent || '') &&
            (n.tagName.toLowerCase() === 'del' || n.tagName.toLowerCase() === 's' ||
             (window.getComputedStyle(n).textDecorationLine || '').includes('line-through'))
        )"""
    )

    sop = _visible_product_container(desktop_page, "Sopianae Multifilter Piros 20")
    assert re.search(r"[-−]50\s*Ft", sop.inner_text())
    assert re.search(r"2[\s\u00a0\u202f]?200\s*Ft", sop.inner_text())


# ---------------------------------------------------------------------------
# Scenario 5: High-volume 4,610-item catalog pagination, category, search
# ---------------------------------------------------------------------------


@pytest.mark.tier4
def test_t4_scenario5_high_volume_4610_catalog_pagination_and_search(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, _LARGE_4610)
    assert parse_result_counter(desktop_page) == (4610, 4610)
    batch1 = count_rendered_products(desktop_page)
    assert 20 <= batch1 <= 200

    btn = get_load_more_button(desktop_page)
    expect(btn).to_be_visible()
    btn.click()
    batch2 = count_rendered_products(desktop_page)
    assert batch2 > batch1
    btn.click()
    batch3 = count_rendered_products(desktop_page)
    assert batch3 > batch2

    # Category filter on 4,610 items
    select_category(desktop_page, "Cigaretta")
    cat_shown, cat_total = parse_result_counter(desktop_page)
    assert cat_total == 4610 and 500 < cat_shown < 1000

    # Reset category to Összes and search item near end of catalog (#4500)
    select_category(desktop_page, "Összes")
    target = next(i for i in _LARGE_4610["items"] if "#4500" in i["name"])
    search_products(desktop_page, "#4500")
    assert parse_result_counter(desktop_page) == (1, 4610)
    expect(_visible_product_container(desktop_page, target["name"])).to_be_visible()

    clear_search(desktop_page)
    assert parse_result_counter(desktop_page) == (4610, 4610)


# ---------------------------------------------------------------------------
# Scenario 6: Full Scraper-to-Served-UI Pipeline (M1 -> data/prices.json -> M2)
# ---------------------------------------------------------------------------


@pytest.mark.tier4
def test_t4_scenario6_full_scraper_to_served_ui_pipeline(
    tmp_path: Path,
    mock_sztfh_server: Any,
    scraper_runner: Any,
    desktop_page: Page,
    app_base_url: str,
    route_prices_json: Any,
) -> None:
    workdir = tmp_path / "pipeline_run"
    mock_sztfh_server.require_custom_ua = True

    # Snapshot 1
    file_v1 = "alkalmazando_dohanygyartmany_kiskereskedelmi_eladasi_arak_20261101100001.xlsx"
    xlsx_v1 = build_xlsx_bytes(
        [
            {
                "name": "Marlboro Red 20 szálas",
                "category": "Cigaretta",
                "quantity": 20,
                "unit_name": "szál",
                "price": 2350,
                "valid_from": "2026-11-01",
            },
            {
                "name": "Sopianae Multifilter Piros 20",
                "category": "Cigaretta",
                "quantity": 20,
                "unit_name": "szál",
                "price": 2200,
                "valid_from": "2026-11-01",
            },
            {
                "name": "Dunhill Fine Cut Blue 20",
                "category": "Cigaretta",
                "quantity": 20,
                "unit_name": "szál",
                "price": 2550,
                "valid_from": "2026-11-01",
            },
        ]
    )
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[f"/downloads/{file_v1}"]))
    mock_sztfh_server.set_xlsx(f"/downloads/{file_v1}", xlsx_v1)

    res1 = scraper_runner.run(mock_sztfh_server, workdir=workdir)
    assert res1.returncode == 0, f"Snapshot 1 scraper failed: {res1.stderr}\n{res1.stdout}"
    data1 = res1.load_prices_json()
    validate_prices_schema(data1)
    assert data1["meta"]["total_count"] == 3

    # Snapshot 2: +100 increase, -50 decrease, unchanged item, new ő/ű item
    file_v2 = "alkalmazando_dohanygyartmany_kiskereskedelmi_eladasi_arak_20261126100001.xlsx"
    xlsx_v2 = build_xlsx_bytes(
        [
            {
                "name": "Marlboro Red 20 szálas",
                "category": "Cigaretta",
                "quantity": 20,
                "unit_name": "szál",
                "price": 2450,
                "valid_from": "2026-11-26",
            },
            {
                "name": "Sopianae Multifilter Piros 20",
                "category": "Cigaretta",
                "quantity": 20,
                "unit_name": "szál",
                "price": 2150,
                "valid_from": "2026-11-26",
            },
            {
                "name": "Dunhill Fine Cut Blue 20",
                "category": "Cigaretta",
                "quantity": 20,
                "unit_name": "szál",
                "price": 2550,
                "valid_from": "2026-11-01",
            },
            {
                "name": "Őszi Szűz Fűszeres Pipadohány 50 g",
                "category": "Egyéb fogyasztási dohány",
                "quantity": 50,
                "unit_name": "g",
                "price": 5490,
                "valid_from": "2026-11-26",
            },
        ]
    )
    mock_sztfh_server.set_html(build_sztfh_html(alkalmazando_links=[f"/downloads/{file_v2}"]))
    mock_sztfh_server.set_xlsx(f"/downloads/{file_v2}", xlsx_v2)

    res2 = scraper_runner.run(mock_sztfh_server, workdir=workdir)
    assert res2.returncode == 0, f"Snapshot 2 scraper failed: {res2.stderr}\n{res2.stdout}"
    data2 = res2.load_prices_json()
    validate_prices_schema(data2)
    assert data2["meta"]["total_count"] == 4

    by_name = {item["name"]: item for item in data2["items"]}
    assert by_name["Marlboro Red 20 szálas"]["price_diff"] == 100
    assert by_name["Sopianae Multifilter Piros 20"]["price_diff"] == -50
    assert by_name["Dunhill Fine Cut Blue 20"]["price_diff"] is None
    assert by_name["Őszi Szűz Fűszeres Pipadohány 50 g"]["is_new"] is True

    # Serve exact scraper output to real frontend via Playwright route
    _load_catalog(desktop_page, app_base_url, route_prices_json, data2)
    assert parse_result_counter(desktop_page) == (4, 4)

    local_dt = datetime.fromisoformat(
        data2["meta"]["last_updated"].replace("Z", "+00:00")
    ).astimezone()
    assert re.search(
        rf"{local_dt.year:04d}[-.\s]+{local_dt.month:02d}[-.\s]+{local_dt.day:02d}",
        desktop_page.locator("body").inner_text(),
    )

    expect(
        _visible_product_container(desktop_page, "Marlboro Red 20 szálas").get_by_text(
            re.compile(r"\+100\s*Ft")
        ).first
    ).to_be_visible()
    expect(
        _visible_product_container(desktop_page, "Sopianae Multifilter Piros 20").get_by_text(
            re.compile(r"[-−]50\s*Ft")
        ).first
    ).to_be_visible()
    expect(
        _visible_product_container(desktop_page, "Őszi Szűz Fűszeres Pipadohány 50 g").get_by_text(
            re.compile(r"^\s*[ÚU]j\s*$")
        ).first
    ).to_be_visible()

    search_products(desktop_page, "oszi szuz")
    assert parse_result_counter(desktop_page) == (1, 4)
    marks = get_mark_texts(
        desktop_page, _visible_product_container(desktop_page, "Őszi Szűz Fűszeres Pipadohány 50 g")
    )
    assert "Őszi" in marks and "Szűz" in marks


# ---------------------------------------------------------------------------
# Scenario 7: Live production data/prices.json & served app smoke check
# ---------------------------------------------------------------------------


@pytest.mark.tier4
def test_t4_scenario7_live_production_artifact_and_served_app_smoke(
    desktop_page: Page, mobile_page: Page, app_base_url: str
) -> None:
    prices_file = APP_ROOT / "data" / "prices.json"
    assert prices_file.exists(), f"Missing production {prices_file}"
    real_data = json.loads(prices_file.read_text(encoding="utf-8"))
    validate_prices_schema(real_data)
    expected_total = real_data["meta"]["total_count"]
    assert expected_total > 0

    # Desktop smoke against unmodified ./data/prices.json
    desktop_page.goto(app_base_url, wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)
    assert parse_result_counter(desktop_page) == (expected_total, expected_total)
    assert count_rendered_products(desktop_page) > 0
    expect(get_sztfh_source_link(desktop_page)).to_be_visible()
    assert_no_horizontal_overflow(desktop_page)
    max_w = get_max_content_width(desktop_page)
    assert max_w is not None and max_w <= 1280.0

    # Mobile smoke against unmodified ./data/prices.json
    mobile_page.goto(app_base_url, wait_until="domcontentloaded")
    _wait_for_catalog_ready(mobile_page)
    assert parse_result_counter(mobile_page) == (expected_total, expected_total)
    assert count_rendered_products(mobile_page) > 0
    assert_no_horizontal_overflow(mobile_page)
    assert_min_touch_targets(mobile_page, min_size=44.0)
