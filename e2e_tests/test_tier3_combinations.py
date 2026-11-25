from __future__ import annotations

import copy
import re
from typing import Any, Literal

import pytest
from playwright.sync_api import Locator, Page, expect

from helpers import (
    assert_min_touch_targets,
    assert_no_horizontal_overflow,
    build_large_catalog,
    build_realistic_catalog,
    clear_search,
    compute_item_id,
    count_rendered_products,
    detect_active_view,
    get_current_theme,
    get_favorites_count_from_tab,
    get_favorites_tab,
    get_load_more_button,
    get_mark_texts,
    get_reset_filters_button,
    get_result_counter_locator,
    get_stored_favorites,
    get_stored_theme,
    get_stored_view,
    get_theme_toggle,
    parse_result_counter,
    search_products,
    select_category,
    set_price_change_filter,
    set_stored_favorites,
    toggle_favorite,
)

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
    expect(counter).to_be_visible(timeout=10000)
    if expect_nonzero:
        expect(counter).not_to_have_text(re.compile(r"^\s*0\s*/\s*0\s*term[eé]k\s*$", re.I), timeout=10000)


def _load_catalog(
    page: Page,
    base_url: str,
    route_prices_json: Any,
    payload: dict[str, Any],
) -> None:
    route_prices_json(page, payload)
    page.goto(base_url, wait_until="domcontentloaded")
    _wait_for_catalog_ready(page, expect_nonzero=len(payload.get("items", [])) > 0)


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
# 1. Search + Category + Result Counter (3 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier3
def test_t3_search_plus_category_matching_intersection(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    select_category(desktop_page, "Cigaretta")
    assert parse_result_counter(desktop_page) == (7, 25)
    search_products(desktop_page, "red")
    # Marlboro Red 20 szálas, Lucky Strike Original Red 20, Winston Classic Red 20
    assert parse_result_counter(desktop_page) == (3, 25)
    texts = _get_visible_product_texts(desktop_page)
    assert any("Marlboro Red 20 szálas" in t for t in texts)
    assert any("Lucky Strike Original Red 20" in t for t in texts)
    assert any("Winston Classic Red 20" in t for t in texts)


@pytest.mark.tier3
def test_t3_search_plus_category_disjoint_shows_empty_state(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    select_category(desktop_page, "Szivarka")
    search_products(desktop_page, "Marlboro")
    assert parse_result_counter(desktop_page) == (0, 25)
    expect(get_reset_filters_button(desktop_page)).to_be_visible()


@pytest.mark.tier3
def test_t3_switching_category_preserves_active_search_query(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    search_products(desktop_page, "Sopianae")
    assert parse_result_counter(desktop_page) == (2, 25)

    select_category(desktop_page, "Finomra vágott fogyasztási dohány")
    assert parse_result_counter(desktop_page) == (1, 25)
    expect(_visible_product_container(desktop_page, "Sopianae Kék Vágott Dohány 30 g")).to_be_visible()


# ---------------------------------------------------------------------------
# 2. Search + "Csak árváltozások" + <mark> highlighting (3 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier3
def test_t3_search_plus_price_change_filter_with_mark_highlight(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    set_price_change_filter(desktop_page, True)
    search_products(desktop_page, "TEREA")
    # Only TEREA Amber changed; TEREA Turquoise is unchanged
    assert parse_result_counter(desktop_page) == (1, 25)
    card = _visible_product_container(desktop_page, "TEREA Amber")
    marks = get_mark_texts(desktop_page, card)
    assert any("terea" in m.lower() for m in marks), marks


@pytest.mark.tier3
def test_t3_search_plus_price_change_excludes_unchanged_matches(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    search_products(desktop_page, "Sopianae")
    assert parse_result_counter(desktop_page) == (2, 25)
    set_price_change_filter(desktop_page, True)
    # Only Sopianae Multifilter Piros 20 changed (-50 Ft); Sopianae Kék Vágott is unchanged
    assert parse_result_counter(desktop_page) == (1, 25)
    expect(_visible_product_container(desktop_page, "Sopianae Multifilter Piros 20")).to_be_visible()


@pytest.mark.tier3
def test_t3_search_accent_insensitive_plus_price_change_includes_new_items(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    set_price_change_filter(desktop_page, True)
    search_products(desktop_page, "oszi")
    assert parse_result_counter(desktop_page) == (1, 25)
    card = _visible_product_container(
        desktop_page, "Őszi Szűz Fűszeres Pipadohány 50 g Extra Hosszú Kiszerelésben"
    )
    assert "Őszi" in get_mark_texts(desktop_page, card)


# ---------------------------------------------------------------------------
# 3. Search + "Kedvencek" + Empty-state Reset (3 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier3
def test_t3_search_inside_kedvencek_narrows_starred_items(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    toggle_favorite(desktop_page, "Marlboro Red 20 szálas")
    toggle_favorite(desktop_page, "TEREA Amber")
    get_favorites_tab(desktop_page).click()
    assert parse_result_counter(desktop_page) == (2, 25)

    search_products(desktop_page, "amber")
    assert parse_result_counter(desktop_page) == (1, 25)
    expect(_visible_product_container(desktop_page, "TEREA Amber")).to_be_visible()


@pytest.mark.tier3
def test_t3_search_inside_kedvencek_zero_match_reset_restores_full_catalog(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    toggle_favorite(desktop_page, "Marlboro Red 20 szálas")
    get_favorites_tab(desktop_page).click()
    search_products(desktop_page, "Moods")
    assert parse_result_counter(desktop_page) == (0, 25)

    get_reset_filters_button(desktop_page).click()
    assert parse_result_counter(desktop_page) == (25, 25)


@pytest.mark.tier3
def test_t3_unstarring_last_item_while_in_kedvencek_updates_count_and_empty_state(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    toggle_favorite(desktop_page, "Moods Filter 10")
    get_favorites_tab(desktop_page).click()
    assert parse_result_counter(desktop_page) == (1, 25)

    card = _visible_product_container(desktop_page, "Moods Filter 10")
    card.locator("button[aria-pressed]").or_(
        card.get_by_role("button", name=re.compile(r"(kedvenc|★|☆)", re.I))
    ).first.click()
    assert get_favorites_count_from_tab(desktop_page) == 0
    assert parse_result_counter(desktop_page) == (0, 25)


# ---------------------------------------------------------------------------
# 4. Category + Sort (4 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier3
def test_t3_category_cigaretta_plus_price_asc(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    select_category(desktop_page, "Cigaretta")
    _select_sort(desktop_page, "price-asc")
    texts = _get_visible_product_texts(desktop_page)
    assert len(texts) == 7
    assert "Sopianae Multifilter Piros 20" in texts[0]  # 2150
    assert "Dunhill Fine Cut Blue 20" in texts[-1]  # 2550


@pytest.mark.tier3
def test_t3_category_cigaretta_plus_price_desc(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    select_category(desktop_page, "Cigaretta")
    _select_sort(desktop_page, "price-desc")
    texts = _get_visible_product_texts(desktop_page)
    assert "Dunhill Fine Cut Blue 20" in texts[0]  # 2550
    assert "Sopianae Multifilter Piros 20" in texts[-1]  # 2150


@pytest.mark.tier3
def test_t3_category_szivarka_plus_name_asc_and_desc(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    select_category(desktop_page, "Szivarka")
    _select_sort(desktop_page, "name-asc")
    asc = _get_visible_product_texts(desktop_page)
    assert len(asc) == 4
    assert "Café Crème Original 10" in asc[0]
    assert "Villiger Mini Espresso Flavour 20" in asc[-1]

    _select_sort(desktop_page, "name-desc")
    desc = _get_visible_product_texts(desktop_page)
    assert "Villiger Mini Espresso Flavour 20" in desc[0]
    assert "Café Crème Original 10" in desc[-1]


@pytest.mark.tier3
def test_t3_category_hevites_plus_recently_changed(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    select_category(desktop_page, "Hevítésre szánt dohánytermék (zárjegyes)")
    _select_sort(desktop_page, "changed-desc")
    texts = _get_visible_product_texts(desktop_page)
    assert len(texts) == 5
    # First 3 have changed_at="2026-11-26"
    top3 = " ".join(texts[:3])
    assert "TEREA Amber" in top3
    assert "Hűvös Kék Áfonya Hevítőrúd 20 szálas" in top3
    assert "NEO Terracotta Tobacco 20" in top3


# ---------------------------------------------------------------------------
# 5. "Csak árváltozások" + Sort (2 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier3
def test_t3_price_change_filter_plus_price_desc(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    set_price_change_filter(desktop_page, True)
    _select_sort(desktop_page, "price-desc")
    texts = _get_visible_product_texts(desktop_page)
    assert "Őszi Szűz Fűszeres Pipadohány 50 g Extra Hosszú Kiszerelésben" in texts[0]  # 5490
    assert "VELO Freezing Peppermint 14 g" in texts[-1]  # 1890


@pytest.mark.tier3
def test_t3_price_change_filter_plus_price_asc(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    set_price_change_filter(desktop_page, True)
    _select_sort(desktop_page, "price-asc")
    texts = _get_visible_product_texts(desktop_page)
    assert "VELO Freezing Peppermint 14 g" in texts[0]  # 1890
    assert "Őszi Szűz Fűszeres Pipadohány 50 g Extra Hosszú Kiszerelésben" in texts[-1]  # 5490


# ---------------------------------------------------------------------------
# 6. "Kedvencek" + Category + Sort + Reload Persistence (3 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier3
def test_t3_kedvencek_plus_category_intersection(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    toggle_favorite(desktop_page, "Marlboro Red 20 szálas")
    toggle_favorite(desktop_page, "Moods Filter 10")
    get_favorites_tab(desktop_page).click()
    select_category(desktop_page, "Cigaretta")
    assert parse_result_counter(desktop_page) == (1, 25)
    expect(_visible_product_container(desktop_page, "Marlboro Red 20 szálas")).to_be_visible()


@pytest.mark.tier3
def test_t3_kedvencek_plus_price_asc_sort(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    toggle_favorite(desktop_page, "Cohiba Siglo II Tubos 3 szálas Prémium Kézzel Sodort Szivar")  # 18500
    toggle_favorite(desktop_page, "VELO Freezing Peppermint 14 g")  # 1890
    toggle_favorite(desktop_page, "Marlboro Red 20 szálas")  # 2450
    get_favorites_tab(desktop_page).click()
    _select_sort(desktop_page, "price-asc")
    texts = _get_visible_product_texts(desktop_page)
    assert "VELO Freezing Peppermint 14 g" in texts[0]
    assert "Marlboro Red 20 szálas" in texts[1]
    assert "Cohiba Siglo II Tubos 3 szálas Prémium Kézzel Sodort Szivar" in texts[2]


@pytest.mark.tier3
def test_t3_kedvencek_plus_sort_survives_reload(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    toggle_favorite(desktop_page, "Marlboro Red 20 szálas")
    toggle_favorite(desktop_page, "TEREA Amber")
    desktop_page.reload(wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)

    assert get_favorites_count_from_tab(desktop_page) == 2
    get_favorites_tab(desktop_page).click()
    _select_sort(desktop_page, "price-desc")
    texts = _get_visible_product_texts(desktop_page)
    assert "Marlboro Red 20 szálas" in texts[0]  # 2450
    assert "TEREA Amber" in texts[1]  # 2390


# ---------------------------------------------------------------------------
# 7. Pagination + Search / Category / Favorites on 4,610 items (3 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier3
def test_t3_pagination_search_finds_item_beyond_first_page(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, _LARGE_4610)
    # Item #4500 is well past initial ~60 item batch
    target = next(i for i in _LARGE_4610["items"] if "#4500" in i["name"])
    search_products(desktop_page, "#4500")
    assert parse_result_counter(desktop_page) == (1, 4610)
    expect(_visible_product_container(desktop_page, target["name"])).to_be_visible()
    clear_search(desktop_page)
    assert parse_result_counter(desktop_page) == (4610, 4610)


@pytest.mark.tier3
def test_t3_pagination_plus_category_filter_loads_more_within_category(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, _LARGE_4610)
    select_category(desktop_page, "Szivarka")
    shown, total = parse_result_counter(desktop_page)
    assert total == 4610 and 500 < shown < 1000
    initial = count_rendered_products(desktop_page)
    assert initial < shown
    get_load_more_button(desktop_page).click()
    assert count_rendered_products(desktop_page) > initial


@pytest.mark.tier3
def test_t3_pagination_plus_preseeded_favorites_beyond_first_page(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    target = next(i for i in _LARGE_4610["items"] if "#4500" in i["name"])
    _load_catalog(desktop_page, app_base_url, route_prices_json, _LARGE_4610)
    set_stored_favorites(desktop_page, [target["id"]])
    desktop_page.reload(wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)

    get_favorites_tab(desktop_page).click()
    assert parse_result_counter(desktop_page) == (1, 4610)
    expect(_visible_product_container(desktop_page, target["name"])).to_be_visible()


# ---------------------------------------------------------------------------
# 8. Desktop Grid vs Table View + Search <mark> + Favorites + Reload (3 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier3
def test_t3_table_view_plus_search_mark_highlighting(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    _set_view_mode(desktop_page, "table")
    assert detect_active_view(desktop_page) == "table"
    search_products(desktop_page, "oszi")
    row = _visible_product_container(
        desktop_page, "Őszi Szűz Fűszeres Pipadohány 50 g Extra Hosszú Kiszerelésben"
    )
    assert "Őszi" in get_mark_texts(desktop_page, row)


@pytest.mark.tier3
def test_t3_table_view_star_favorite_persists_back_to_grid_and_reload(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    _set_view_mode(desktop_page, "table")
    assert toggle_favorite(desktop_page, "Marlboro Red 20 szálas") is True

    _set_view_mode(desktop_page, "grid")
    assert detect_active_view(desktop_page) == "grid"
    assert get_favorites_count_from_tab(desktop_page) == 1

    desktop_page.reload(wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)
    assert get_stored_view(desktop_page) == "grid"
    assert compute_item_id("Marlboro Red 20 szálas", "20 szál") in get_stored_favorites(desktop_page)


@pytest.mark.tier3
def test_t3_table_view_plus_category_and_price_desc_sort(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    _set_view_mode(desktop_page, "table")
    select_category(desktop_page, "Cigaretta")
    _select_sort(desktop_page, "price-desc")
    texts = _get_visible_product_texts(desktop_page)
    assert len(texts) == 7
    assert "Dunhill Fine Cut Blue 20" in texts[0]
    assert "Sopianae Multifilter Piros 20" in texts[-1]


# ---------------------------------------------------------------------------
# 9. Dark vs Light Theme + Badges + <mark> + Reload (2 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier3
def test_t3_light_theme_plus_price_change_badges_and_reload(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    get_theme_toggle(desktop_page).click()
    assert get_current_theme(desktop_page) == "light"
    set_price_change_filter(desktop_page, True)

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

    desktop_page.reload(wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)
    assert get_current_theme(desktop_page) == "light"
    assert get_stored_theme(desktop_page) == "light"


@pytest.mark.tier3
def test_t3_light_theme_plus_search_mark_highlight(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    get_theme_toggle(desktop_page).click()
    search_products(desktop_page, "afónya")
    mark_el = desktop_page.locator("mark:visible").first
    expect(mark_el).to_be_visible()
    assert "Áfonya" in mark_el.inner_text()


# ---------------------------------------------------------------------------
# 10. Mobile 375x812 + Search + Category Chips + Star + Zero Overflow (2 tests)
# ---------------------------------------------------------------------------


@pytest.mark.tier3
def test_t3_mobile_search_plus_star_favorite_plus_kedvencek_zero_overflow(
    mobile_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(mobile_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    search_products(mobile_page, "huvos")
    assert toggle_favorite(mobile_page, "Hűvös Kék Áfonya Hevítőrúd 20 szálas") is True
    clear_search(mobile_page)
    get_favorites_tab(mobile_page).click()
    assert parse_result_counter(mobile_page) == (1, 25)
    assert_no_horizontal_overflow(mobile_page)
    assert_min_touch_targets(mobile_page, min_size=44.0)


@pytest.mark.tier3
def test_t3_mobile_category_chip_scroll_plus_price_change_filter_zero_overflow(
    mobile_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(mobile_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_25))
    select_category(mobile_page, "Hevítésre szánt dohánytermék (zárjegyes)")
    set_price_change_filter(mobile_page, True)
    assert parse_result_counter(mobile_page) == (3, 25)
    assert_no_horizontal_overflow(mobile_page)
    assert_min_touch_targets(mobile_page, min_size=44.0)
