from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any, Literal

import pytest
from playwright.sync_api import Locator, Page, expect

from helpers import (
    LS_FAVORITES_KEY,
    LS_THEME_KEY,
    LS_VIEW_KEY,
    assert_min_touch_targets,
    assert_no_horizontal_overflow,
    build_large_catalog,
    build_prices_json,
    build_realistic_catalog,
    compute_item_id,
    count_rendered_products,
    detect_active_view,
    get_clear_search_button,
    get_current_theme,
    get_favorites_count_from_tab,
    get_favorites_tab,
    get_grid_column_count,
    get_load_more_button,
    get_mark_texts,
    get_max_content_width,
    get_reset_filters_button,
    get_result_counter_locator,
    get_retry_button,
    get_search_input,
    get_stored_favorites,
    get_stored_theme,
    get_sztfh_source_link,
    get_theme_toggle,
    make_catalog_item,
    parse_result_counter,
    search_products,
    select_category,
    set_price_change_filter,
    set_stored_favorites,
    toggle_favorite,
)

_REALISTIC_22 = build_realistic_catalog(count=22)
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
    expect(counter).to_be_visible(timeout=10000)
    if expect_nonzero:
        expect(counter).not_to_have_text(re.compile(r"^\s*0\s*/\s*0\s*term[eé]k\s*$", re.I), timeout=10000)


def _load_catalog(
    page: Page,
    base_url: str,
    route_prices_json: Any,
    payload: dict[str, Any] | list[Any] | str | int | None = None,
    *,
    wait_for_counter: bool = True,
) -> None:
    if payload is not None:
        route_prices_json(page, payload)
    page.goto(base_url, wait_until="domcontentloaded")
    if wait_for_counter:
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


# ===========================================================================
# F11 Boundary & Corner Cases (5 tests)
# ===========================================================================


@pytest.mark.tier2
def test_t2_f11_very_long_unbroken_product_name_wraps_without_overflow_mobile(
    mobile_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    long_unbroken = "ŐrültHosszúÖsszefüggőMárkanévŰrhajóCigaretta" + ("X" * 70)
    payload = build_prices_json(
        [make_catalog_item(name=long_unbroken, category="Cigaretta", unit="20 szál", price=2500)]
    )
    _load_catalog(mobile_page, app_base_url, route_prices_json, payload)
    assert_no_horizontal_overflow(mobile_page)
    card = _visible_product_container(mobile_page, long_unbroken)
    assert long_unbroken in card.inner_text()


@pytest.mark.tier2
def test_t2_f11_long_name_with_slashes_and_hyphens_wraps_on_desktop_card_and_table(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    long_name = (
        "Prémium Kézzel Sodort Extra-Hosszú/Fűszeres-Őszi-Űr-Variáns "
        "Különleges Kiadás 2026/II - 100s Filteres Szivar"
    )
    payload = build_prices_json(
        [make_catalog_item(name=long_name, category="Szivar", unit="5 szál", price=12900)]
    )
    _load_catalog(desktop_page, app_base_url, route_prices_json, payload)
    assert long_name in _visible_product_container(desktop_page, long_name).inner_text()
    assert_no_horizontal_overflow(desktop_page)

    _set_view_mode(desktop_page, "table")
    assert detect_active_view(desktop_page) == "table"
    assert long_name in _visible_product_container(desktop_page, long_name).inner_text()
    assert_no_horizontal_overflow(desktop_page)


@pytest.mark.tier2
def test_t2_f11_all_hungarian_diacritics_render_intact(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    diacritics_name = "Árvíztűrő Tükörfúrógép áéíóöőúüű ÁÉÍÓÖŐÚÜŰ 20"
    payload = build_prices_json(
        [make_catalog_item(name=diacritics_name, category="Egyéb fogyasztási dohány", unit="50 g", price=4990)]
    )
    _load_catalog(desktop_page, app_base_url, route_prices_json, payload)
    txt = _visible_product_container(desktop_page, diacritics_name).inner_text()
    assert diacritics_name in txt
    assert "\ufffd" not in txt


@pytest.mark.tier2
def test_t2_f11_extreme_price_values_tabular_alignment(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    items = [
        make_catalog_item(name="Olcsó Minta 1", category="Cigaretta", unit="1 szál", price=10),
        make_catalog_item(name="Luxus Szivar Doboz", category="Szivar", unit="50 szál", price=999990),
    ]
    _load_catalog(desktop_page, app_base_url, route_prices_json, build_prices_json(items))
    assert_no_horizontal_overflow(desktop_page)
    for name in ("Olcsó Minta 1", "Luxus Szivar Doboz"):
        card = _visible_product_container(desktop_page, name)
        has_tab = card.evaluate(
            """el => Array.from(el.querySelectorAll('*')).some(n =>
                /\\d+\\s*Ft$/i.test((n.textContent || '').trim()) &&
                window.getComputedStyle(n).fontVariantNumeric.includes('tabular-nums')
            )"""
        )
        assert has_tab, f"Expected tabular-nums on extreme price for {name}"


@pytest.mark.tier2
def test_t2_f11_long_category_name_wraps_or_fits_cleanly(
    mobile_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    long_cat = "Különleges Füstmentes Hevítésre Szánt Dohánygyártmányok És Nikotinos Termékek"
    payload = build_prices_json(
        [make_catalog_item(name="Teszt Termék", category=long_cat, unit="20 szál", price=2300)]
    )
    _load_catalog(mobile_page, app_base_url, route_prices_json, payload)
    assert_no_horizontal_overflow(mobile_page)
    assert "Teszt Termék" in _visible_product_container(mobile_page, "Teszt Termék").inner_text()


# ===========================================================================
# F12 Boundary & Corner Cases (5 tests)
# ===========================================================================


@pytest.mark.tier2
def test_t2_f12_corrupt_localstorage_theme_value_falls_back_gracefully(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    desktop_page.evaluate("k => window.localStorage.setItem(k, 'invalid-theme-xyz')", LS_THEME_KEY)
    desktop_page.reload(wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)
    assert get_current_theme(desktop_page) in ("dark", "light")
    assert parse_result_counter(desktop_page) == (22, 22)


@pytest.mark.tier2
def test_t2_f12_empty_localstorage_theme_value_uses_default_dark(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    desktop_page.evaluate("k => window.localStorage.setItem(k, '')", LS_THEME_KEY)
    desktop_page.reload(wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)
    assert get_current_theme(desktop_page) == "dark"


@pytest.mark.tier2
def test_t2_f12_rapid_multiple_theme_toggles_stay_consistent(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    toggle = get_theme_toggle(desktop_page)
    for _ in range(5):
        toggle.click()
    assert get_current_theme(desktop_page) == "light"
    assert get_stored_theme(desktop_page) == "light"


@pytest.mark.tier2
def test_t2_f12_preseeded_light_theme_on_initial_load(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    route_prices_json(desktop_page, copy.deepcopy(_REALISTIC_22))
    desktop_page.add_init_script(
        f"window.localStorage.setItem({LS_THEME_KEY!r}, 'light');"
    )
    desktop_page.goto(app_base_url, wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)
    assert get_current_theme(desktop_page) == "light"


@pytest.mark.tier2
def test_t2_f12_theme_persists_even_when_prices_json_empty(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, build_prices_json([]))
    get_theme_toggle(desktop_page).click()
    assert get_stored_theme(desktop_page) == "light"
    desktop_page.reload(wait_until="domcontentloaded")
    expect(get_result_counter_locator(desktop_page)).to_be_visible(timeout=10000)
    assert get_current_theme(desktop_page) == "light"


# ===========================================================================
# F13 Boundary & Corner Cases (5 tests)
# ===========================================================================


@pytest.mark.tier2
def test_t2_f13_mobile_375_all_controls_touch_targets_and_zero_overflow(
    mobile_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(mobile_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    search_products(mobile_page, "Marlboro")
    # Clear button is now visible too
    expect(get_clear_search_button(mobile_page)).to_be_visible()
    assert_min_touch_targets(mobile_page, min_size=44.0)
    assert_no_horizontal_overflow(mobile_page)


@pytest.mark.tier2
def test_t2_f13_mobile_narrow_320px_viewport_zero_overflow(
    mobile_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    mobile_page.set_viewport_size({"width": 320, "height": 568})
    _load_catalog(mobile_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    assert get_grid_column_count(mobile_page) == 1
    assert_no_horizontal_overflow(mobile_page)


@pytest.mark.tier2
def test_t2_f13_tablet_640px_and_1024px_boundaries_no_overflow(
    tablet_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(tablet_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    for w in (640, 1024):
        tablet_page.set_viewport_size({"width": w, "height": 900})
        tablet_page.wait_for_timeout(100)
        assert_no_horizontal_overflow(tablet_page)
        assert get_grid_column_count(tablet_page) == 2


@pytest.mark.tier2
def test_t2_f13_desktop_wide_1920px_maintains_max_width_1280_centered(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    desktop_page.set_viewport_size({"width": 1920, "height": 1080})
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    max_w = get_max_content_width(desktop_page)
    assert max_w is not None and max_w <= 1280.0
    box = desktop_page.locator("main").first.bounding_box()
    assert box is not None
    left_margin = box["x"]
    right_margin = 1920.0 - (box["x"] + box["width"])
    assert abs(left_margin - right_margin) <= 24.0, (
        f"Expected centered container at 1920px: left={left_margin}, right={right_margin}"
    )


@pytest.mark.tier2
def test_t2_f13_corrupt_localstorage_view_value_falls_back_to_valid_view(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    desktop_page.evaluate("k => window.localStorage.setItem(k, 'broken-view-mode')", LS_VIEW_KEY)
    desktop_page.reload(wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)
    assert detect_active_view(desktop_page) in ("grid", "table")
    assert parse_result_counter(desktop_page) == (22, 22)


# ===========================================================================
# F14 Boundary & Corner Cases (5 tests)
# ===========================================================================


@pytest.mark.tier2
def test_t2_f14_empty_and_whitespace_only_query_shows_all_items(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    search_products(desktop_page, "     ")
    assert parse_result_counter(desktop_page) == (22, 22)
    search_products(desktop_page, "\t  \n  ")
    assert parse_result_counter(desktop_page) == (22, 22)


@pytest.mark.tier2
def test_t2_f14_leading_trailing_multiple_spaces_between_words(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    search_products(desktop_page, "   red    marlboro   20   ")
    assert parse_result_counter(desktop_page) == (1, 22)
    expect(_visible_product_container(desktop_page, "Marlboro Red 20 szálas")).to_be_visible()


@pytest.mark.tier2
def test_t2_f14_regex_metacharacters_in_query_do_not_throw_or_break(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    items = [
        make_catalog_item(name="Special (Gold) [20]+ *Mix*?", category="Cigaretta", unit="20 szál", price=2500),
        make_catalog_item(name="Normal Brand 20", category="Cigaretta", unit="20 szál", price=2300),
    ]
    _load_catalog(desktop_page, app_base_url, route_prices_json, build_prices_json(items))
    for query in ("(Gold)", "[20]+", "*Mix*?", "\\", ".*", "(("):
        search_products(desktop_page, query)
        shown, total = parse_result_counter(desktop_page)
        assert total == 2
        assert 0 <= shown <= 2


@pytest.mark.tier2
def test_t2_f14_unicode_nfd_decomposed_input_matches_nfc_catalog(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    nfd_query = unicodedata.normalize("NFD", "Őszi Szűz")
    search_products(desktop_page, nfd_query)
    assert parse_result_counter(desktop_page) == (1, 22)
    expect(
        _visible_product_container(
            desktop_page, "Őszi Szűz Fűszeres Pipadohány 50 g Extra Hosszú Kiszerelésben"
        )
    ).to_be_visible()


@pytest.mark.tier2
def test_t2_f14_rapid_typing_burst_debounces_to_final_query(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    inp = get_search_input(desktop_page)
    for q in ("Marlboro", "TEREA", "Dunhill", "Moods"):
        inp.fill(q)
    desktop_page.wait_for_timeout(300)
    assert parse_result_counter(desktop_page) == (1, 22)
    expect(_visible_product_container(desktop_page, "Moods Filter 10")).to_be_visible()


# ===========================================================================
# F15 Boundary & Corner Cases (5 tests)
# ===========================================================================


@pytest.mark.tier2
def test_t2_f15_html_metacharacters_and_script_tags_safely_escaped(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    items = [
        make_catalog_item(name="Alpha & Beta <Test> 20", category="Cigaretta", unit="20 szál", price=2400),
    ]
    _load_catalog(desktop_page, app_base_url, route_prices_json, build_prices_json(items))
    for xss_q in ("<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "&", "<Test>"):
        search_products(desktop_page, xss_q)
        injected = desktop_page.evaluate(
            "() => document.querySelectorAll('main script, main img').length"
        )
        assert injected == 0, f"Unexpected HTML injection for query {xss_q!r}"
    search_products(desktop_page, "Alpha & Beta")
    assert "Alpha & Beta <Test> 20" in _visible_product_container(
        desktop_page, "Alpha & Beta <Test> 20"
    ).inner_text()


@pytest.mark.tier2
def test_t2_f15_regex_special_chars_in_product_name_highlighted_safely(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    pname = "Special (Gold) [20]+ *Mix*?"
    payload = build_prices_json(
        [make_catalog_item(name=pname, category="Cigaretta", unit="20 szál", price=2400)]
    )
    _load_catalog(desktop_page, app_base_url, route_prices_json, payload)
    search_products(desktop_page, "(Gold)")
    card = _visible_product_container(desktop_page, pname)
    assert pname in card.inner_text()
    marks = get_mark_texts(desktop_page, card)
    assert any("gold" in m.lower() for m in marks), marks


@pytest.mark.tier2
def test_t2_f15_overlapping_or_duplicate_query_tokens_handled(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    search_products(desktop_page, "red red 20")
    card = _visible_product_container(desktop_page, "Marlboro Red 20 szálas")
    assert "Marlboro Red 20 szálas" in card.inner_text()
    marks = get_mark_texts(desktop_page, card)
    assert marks


@pytest.mark.tier2
def test_t2_f15_accented_query_matches_unaccented_or_mixed_accent_name(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    search_products(desktop_page, "afónya")
    card = _visible_product_container(desktop_page, "Hűvös Kék Áfonya Hevítőrúd 20 szálas")
    marks = get_mark_texts(desktop_page, card)
    assert "Áfonya" in marks, f"Expected original 'Áfonya' in <mark>, got {marks}"


@pytest.mark.tier2
def test_t2_f15_category_only_match_does_not_corrupt_product_name(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    search_products(desktop_page, "szivarka")
    assert parse_result_counter(desktop_page) == (3, 22)
    assert "Moods Filter 10" in _visible_product_container(desktop_page, "Moods Filter 10").inner_text()


# ===========================================================================
# F16 Boundary & Corner Cases (5 tests)
# ===========================================================================


@pytest.mark.tier2
def test_t2_f16_empty_catalog_items_array_shows_zero_of_zero(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, build_prices_json([]))
    assert parse_result_counter(desktop_page) == (0, 0)
    assert count_rendered_products(desktop_page) == 0


@pytest.mark.tier2
def test_t2_f16_single_item_catalog_shows_one_of_one(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    payload = build_prices_json(
        [make_catalog_item(name="Egyetlen Termék 20", category="Cigaretta", unit="20 szál", price=2200)]
    )
    _load_catalog(desktop_page, app_base_url, route_prices_json, payload)
    assert parse_result_counter(desktop_page) == (1, 1)
    assert count_rendered_products(desktop_page) == 1


@pytest.mark.tier2
def test_t2_f16_large_count_thousands_formatted_counter_parses_cleanly(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, _LARGE_4610)
    shown, total = parse_result_counter(desktop_page)
    assert shown == 4610 and total == 4610


@pytest.mark.tier2
def test_t2_f16_reset_button_when_favorites_tab_empty_restores_all(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    get_favorites_tab(desktop_page).click()
    assert parse_result_counter(desktop_page) == (0, 22)
    reset_btn = get_reset_filters_button(desktop_page)
    expect(reset_btn).to_be_visible()
    reset_btn.click()
    assert parse_result_counter(desktop_page) == (22, 22)


@pytest.mark.tier2
def test_t2_f16_clearing_search_via_backspace_restores_counter_from_empty(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    search_products(desktop_page, "NincsIlyenTermék999")
    assert parse_result_counter(desktop_page) == (0, 22)
    search_products(desktop_page, "")
    assert parse_result_counter(desktop_page) == (22, 22)


# ===========================================================================
# F17 Boundary & Corner Cases (5 tests)
# ===========================================================================


def _select_category_safe(page: Page, category_name: str) -> None:
    escaped = re.escape(category_name).replace("/", "\\/")
    pat = re.compile(rf"^\s*{escaped}(?:\s*[\(\[]?\d+[\)\]]?)?\s*$", re.I)
    btn = page.get_by_role("button", name=pat).or_(page.get_by_role("tab", name=pat)).or_(
        page.get_by_role("radio", name=pat)
    ).first
    btn.click()


@pytest.mark.tier2
def test_t2_f17_category_egyeb_and_special_chars_handled(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    items = [
        make_catalog_item(name="Alap Egyéb", category="Egyéb", unit="10 g", price=1900),
        make_catalog_item(name="Spec Termék", category="Speciális (A/B) & Co.", unit="20 szál", price=2500),
    ]
    _load_catalog(desktop_page, app_base_url, route_prices_json, build_prices_json(items))
    _select_category_safe(desktop_page, "Egyéb")
    assert parse_result_counter(desktop_page) == (1, 2)
    expect(_visible_product_container(desktop_page, "Alap Egyéb")).to_be_visible()

    _select_category_safe(desktop_page, "Speciális (A/B) & Co.")
    assert parse_result_counter(desktop_page) == (1, 2)
    expect(_visible_product_container(desktop_page, "Spec Termék")).to_be_visible()


@pytest.mark.tier2
def test_t2_f17_price_change_filter_excludes_zero_diff_items(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    items = [
        make_catalog_item(
            name="Nulla Diff Termék",
            category="Cigaretta",
            unit="20 szál",
            price=2350,
            previous_price=2350,
            price_diff=0,
            is_new=False,
        ),
        make_catalog_item(
            name="Valódi Emelkedés",
            category="Cigaretta",
            unit="20 szál",
            price=2450,
            previous_price=2350,
            changed_at="2026-11-26",
            is_new=False,
        ),
        make_catalog_item(
            name="Új Termék",
            category="Cigaretta",
            unit="20 szál",
            price=2500,
            changed_at="2026-11-26",
            is_new=True,
        ),
    ]
    _load_catalog(desktop_page, app_base_url, route_prices_json, build_prices_json(items))
    set_price_change_filter(desktop_page, True)
    assert parse_result_counter(desktop_page) == (2, 3)
    texts = _get_visible_product_texts(desktop_page)
    assert not any("Nulla Diff Termék" in t for t in texts)


@pytest.mark.tier2
def test_t2_f17_hungarian_accented_alphabetical_collation(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    items = [
        make_catalog_item(name="Űrhajó Brand", category="Cigaretta", unit="20 szál", price=2100),
        make_catalog_item(name="Álom Brand", category="Cigaretta", unit="20 szál", price=2200),
        make_catalog_item(name="Bátor Brand", category="Cigaretta", unit="20 szál", price=2300),
        make_catalog_item(name="Őszi Brand", category="Cigaretta", unit="20 szál", price=2400),
    ]
    _load_catalog(desktop_page, app_base_url, route_prices_json, build_prices_json(items))
    _select_sort(desktop_page, "name-asc")
    texts = _get_visible_product_texts(desktop_page)
    assert "Álom Brand" in texts[0]
    assert "Bátor Brand" in texts[1]
    assert "Őszi Brand" in texts[2]
    assert "Űrhajó Brand" in texts[3]


@pytest.mark.tier2
def test_t2_f17_identical_prices_sort_stably_without_error(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    items = [
        make_catalog_item(name=f"Azonos Ár {i}", category="Cigaretta", unit="20 szál", price=2350)
        for i in range(5)
    ]
    _load_catalog(desktop_page, app_base_url, route_prices_json, build_prices_json(items))
    _select_sort(desktop_page, "price-asc")
    assert parse_result_counter(desktop_page) == (5, 5)
    _select_sort(desktop_page, "price-desc")
    assert parse_result_counter(desktop_page) == (5, 5)


@pytest.mark.tier2
def test_t2_f17_single_item_and_empty_catalog_all_sort_modes_safe(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    payload_one = build_prices_json(
        [make_catalog_item(name="Solo Item", category="Cigaretta", unit="20 szál", price=2350)]
    )
    _load_catalog(desktop_page, app_base_url, route_prices_json, payload_one)
    for mode in ("name-asc", "name-desc", "price-asc", "price-desc", "changed-desc"):
        _select_sort(desktop_page, mode)  # type: ignore[arg-type]
        assert parse_result_counter(desktop_page) == (1, 1)

    _load_catalog(desktop_page, app_base_url, route_prices_json, build_prices_json([]))
    for mode in ("name-asc", "name-desc", "price-asc", "price-desc", "changed-desc"):
        _select_sort(desktop_page, mode)  # type: ignore[arg-type]
        assert parse_result_counter(desktop_page) == (0, 0)


# ===========================================================================
# F18 Boundary & Corner Cases (5 tests)
# ===========================================================================


@pytest.mark.tier2
def test_t2_f18_corrupt_json_in_dohany_favorites_handled_gracefully(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    desktop_page.evaluate("k => window.localStorage.setItem(k, '{not-valid-json')", LS_FAVORITES_KEY)
    desktop_page.reload(wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)
    assert get_favorites_count_from_tab(desktop_page) == 0
    assert parse_result_counter(desktop_page) == (22, 22)


@pytest.mark.tier2
def test_t2_f18_non_array_json_in_dohany_favorites_handled_gracefully(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    desktop_page.evaluate("k => window.localStorage.setItem(k, '{\"a\": 123}')", LS_FAVORITES_KEY)
    desktop_page.reload(wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)
    assert get_favorites_count_from_tab(desktop_page) == 0


@pytest.mark.tier2
def test_t2_f18_stale_unknown_favorite_ids_do_not_crash_kedvencek(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    set_stored_favorites(desktop_page, ["0000000000000001", "deadbeefcafebabe"])
    desktop_page.reload(wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)
    get_favorites_tab(desktop_page).click()
    assert parse_result_counter(desktop_page) == (0, 22)


@pytest.mark.tier2
def test_t2_f18_duplicate_ids_in_dohany_favorites_deduplicated(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    target_id = compute_item_id("Marlboro Red 20 szálas", "20 szál")
    set_stored_favorites(desktop_page, [target_id, target_id, target_id])
    desktop_page.reload(wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)
    get_favorites_tab(desktop_page).click()
    assert parse_result_counter(desktop_page) == (1, 22)
    assert count_rendered_products(desktop_page) == 1


@pytest.mark.tier2
def test_t2_f18_favorite_star_touch_target_on_mobile_at_least_44px(
    mobile_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(mobile_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    card = _visible_product_container(mobile_page, "Marlboro Red 20 szálas")
    star = card.locator("button[aria-pressed]").or_(
        card.get_by_role("button", name=re.compile(r"(kedvenc|★|☆)", re.I))
    ).first
    box = star.bounding_box()
    assert box is not None and box["width"] >= 44.0 and box["height"] >= 44.0, box
    assert toggle_favorite(mobile_page, "Marlboro Red 20 szálas") is True
    assert compute_item_id("Marlboro Red 20 szálas", "20 szál") in get_stored_favorites(mobile_page)


# ===========================================================================
# F19 Boundary & Corner Cases (5 tests)
# ===========================================================================


@pytest.mark.tier2
def test_t2_f19_zero_price_diff_shows_no_plus_minus_badge(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    payload = build_prices_json(
        [
            make_catalog_item(
                name="Változatlan 0 Ft Diff",
                category="Cigaretta",
                unit="20 szál",
                price=2350,
                previous_price=2350,
                price_diff=0,
                is_new=False,
            )
        ]
    )
    _load_catalog(desktop_page, app_base_url, route_prices_json, payload)
    txt = _visible_product_container(desktop_page, "Változatlan 0 Ft Diff").inner_text()
    assert not re.search(r"[+\-−]\s*0\s*Ft", txt), txt


@pytest.mark.tier2
def test_t2_f19_new_item_with_null_previous_price_shows_uj_without_strikethrough(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    card = _visible_product_container(desktop_page, "VELO Freezing Peppermint 14 g")
    expect(card.get_by_text(re.compile(r"^\s*[ÚU]j\s*$", re.I)).first).to_be_visible()
    has_strike = card.evaluate(
        """el => Array.from(el.querySelectorAll('*')).some(n => {
            const tag = n.tagName.toLowerCase();
            const td = window.getComputedStyle(n).textDecorationLine || '';
            return tag === 'del' || tag === 's' || td.includes('line-through');
        })"""
    )
    assert not has_strike


@pytest.mark.tier2
def test_t2_f19_large_price_diff_formatting(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    items = [
        make_catalog_item(
            name="Nagy Emelés Szivar",
            category="Szivar",
            unit="5 szál",
            price=18500,
            previous_price=17000,
            changed_at="2026-11-26",
        ),
        make_catalog_item(
            name="Nagy Csökkenés Szivar",
            category="Szivar",
            unit="5 szál",
            price=15000,
            previous_price=16200,
            changed_at="2026-11-26",
        ),
    ]
    _load_catalog(desktop_page, app_base_url, route_prices_json, build_prices_json(items))
    inc_txt = _visible_product_container(desktop_page, "Nagy Emelés Szivar").inner_text()
    dec_txt = _visible_product_container(desktop_page, "Nagy Csökkenés Szivar").inner_text()
    assert re.search(r"\+1[\s\u00a0\u202f]?500\s*Ft", inc_txt), inc_txt
    assert re.search(r"[-−]1[\s\u00a0\u202f]?200\s*Ft", dec_txt), dec_txt


@pytest.mark.tier2
def test_t2_f19_empty_valid_from_string_does_not_crash(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    payload = build_prices_json(
        [make_catalog_item(name="Dátum Nélküli", category="Cigaretta", unit="20 szál", price=2100, valid_from="")]
    )
    _load_catalog(desktop_page, app_base_url, route_prices_json, payload)
    txt = _visible_product_container(desktop_page, "Dátum Nélküli").inner_text()
    assert "Dátum Nélküli" in txt
    assert "undefined" not in txt.lower()
    assert "invalid date" not in txt.lower()


@pytest.mark.tier2
def test_t2_f19_price_diff_badges_visible_in_desktop_table_view(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    _set_view_mode(desktop_page, "table")
    assert detect_active_view(desktop_page) == "table"
    row_inc = _visible_product_container(desktop_page, "Marlboro Red 20 szálas")
    assert re.search(r"\+100\s*Ft", row_inc.inner_text())
    row_dec = _visible_product_container(desktop_page, "Sopianae Multifilter Piros 20")
    assert re.search(r"[-−]50\s*Ft", row_dec.inner_text())
    row_new = _visible_product_container(desktop_page, "VELO Freezing Peppermint 14 g")
    assert re.search(r"\b[ÚU]j\b", row_new.inner_text())


# ===========================================================================
# F20 Boundary & Corner Cases (5 tests)
# ===========================================================================


@pytest.mark.tier2
def test_t2_f20_http_404_shows_error_and_recovers_on_retry(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, 404, wait_for_counter=False)
    retry_btn = get_retry_button(desktop_page)
    expect(retry_btn).to_be_visible(timeout=10000)
    route_prices_json(desktop_page, copy.deepcopy(_REALISTIC_22))
    retry_btn.click()
    _wait_for_catalog_ready(desktop_page, expect_nonzero=True)
    assert parse_result_counter(desktop_page) == (22, 22)


@pytest.mark.tier2
def test_t2_f20_network_abort_shows_error_and_recovers_on_retry(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, "abort", wait_for_counter=False)
    retry_btn = get_retry_button(desktop_page)
    expect(retry_btn).to_be_visible(timeout=10000)
    route_prices_json(desktop_page, copy.deepcopy(_REALISTIC_22))
    retry_btn.click()
    _wait_for_catalog_ready(desktop_page, expect_nonzero=True)
    assert parse_result_counter(desktop_page) == (22, 22)


@pytest.mark.tier2
def test_t2_f20_malformed_json_body_shows_error_and_recovers_on_retry(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, "{corrupt json", wait_for_counter=False)
    retry_btn = get_retry_button(desktop_page)
    expect(retry_btn).to_be_visible(timeout=10000)
    route_prices_json(desktop_page, copy.deepcopy(_REALISTIC_22))
    retry_btn.click()
    _wait_for_catalog_ready(desktop_page, expect_nonzero=True)
    assert parse_result_counter(desktop_page) == (22, 22)


@pytest.mark.tier2
def test_t2_f20_missing_meta_fields_handled_without_crash(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    payload = {
        "meta": {"last_updated": "", "total_count": 1},
        "items": [make_catalog_item(name="Minimál Meta Termék", category="Cigaretta", unit="20 szál", price=2200)],
    }
    _load_catalog(desktop_page, app_base_url, route_prices_json, payload)
    assert parse_result_counter(desktop_page) == (1, 1)
    assert "undefined" not in desktop_page.locator("body").inner_text().lower()


@pytest.mark.tier2
def test_t2_f20_zero_items_catalog_displays_zero_count_and_source_link(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, build_prices_json([]))
    assert parse_result_counter(desktop_page) == (0, 0)
    expect(get_sztfh_source_link(desktop_page)).to_be_visible()
    assert re.search(r"t[aá]j[eé]koztat[oó]", desktop_page.locator("body").inner_text(), re.I)


# ===========================================================================
# F21 Boundary & Corner Cases (5 tests)
# ===========================================================================


@pytest.mark.tier2
def test_t2_f21_catalog_below_page_size_hides_load_more(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    btn = get_load_more_button(desktop_page)
    assert btn.count() == 0 or not btn.is_visible()


@pytest.mark.tier2
def test_t2_f21_catalog_exact_page_size_boundary_probe(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    # Probe page size P dynamically using 250-item catalog
    _load_catalog(desktop_page, app_base_url, route_prices_json, build_large_catalog(250))
    page_size = count_rendered_products(desktop_page)
    assert 20 <= page_size < 250

    # Exactly P items: Load More must NOT be visible
    _load_catalog(desktop_page, app_base_url, route_prices_json, build_large_catalog(page_size))
    btn = get_load_more_button(desktop_page)
    assert count_rendered_products(desktop_page) == page_size
    assert btn.count() == 0 or not btn.is_visible()

    # P + 1 items: Load More IS visible, clicking loads the 1 extra item and hides button
    _load_catalog(desktop_page, app_base_url, route_prices_json, build_large_catalog(page_size + 1))
    expect(btn).to_be_visible()
    assert count_rendered_products(desktop_page) == page_size
    btn.click()
    assert count_rendered_products(desktop_page) == page_size + 1
    assert btn.count() == 0 or not btn.is_visible()


@pytest.mark.tier2
def test_t2_f21_filtering_after_pagination_resets_page_cleanly(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, _LARGE_4610)
    btn = get_load_more_button(desktop_page)
    btn.click()
    btn.click()
    assert count_rendered_products(desktop_page) > 100

    search_products(desktop_page, "Marlboro Red 20 szálas")
    assert parse_result_counter(desktop_page) == (1, 4610)
    assert count_rendered_products(desktop_page) == 1
    assert btn.count() == 0 or not btn.is_visible()


@pytest.mark.tier2
def test_t2_f21_pagination_works_in_desktop_table_view(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, _LARGE_4610)
    _set_view_mode(desktop_page, "table")
    assert detect_active_view(desktop_page) == "table"
    initial = count_rendered_products(desktop_page)
    assert 20 <= initial <= 200
    get_load_more_button(desktop_page).click()
    after = count_rendered_products(desktop_page)
    assert after > initial


@pytest.mark.tier2
def test_t2_f21_pagination_works_on_mobile_viewport_with_zero_overflow(
    mobile_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(mobile_page, app_base_url, route_prices_json, _LARGE_4610)
    initial = count_rendered_products(mobile_page)
    assert_no_horizontal_overflow(mobile_page)
    btn = get_load_more_button(mobile_page)
    btn.scroll_into_view_if_needed()
    btn.click()
    assert count_rendered_products(mobile_page) > initial
    assert_no_horizontal_overflow(mobile_page)
