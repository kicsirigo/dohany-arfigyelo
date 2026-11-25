from __future__ import annotations

import copy
import re
import time
from typing import Any, Literal

import pytest
from playwright.sync_api import Locator, Page, expect

from helpers import (
    assert_min_touch_targets,
    assert_no_horizontal_overflow,
    build_large_catalog,
    build_prices_json,
    build_realistic_catalog,
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
    get_reset_filters_button,
    get_result_counter_locator,
    get_retry_button,
    get_search_input,
    get_stored_favorites,
    get_stored_theme,
    get_stored_view,
    get_sztfh_source_link,
    get_theme_toggle,
    make_catalog_item,
    parse_result_counter,
    search_products,
    select_category,
    set_price_change_filter,
    toggle_favorite,
)

# ---------------------------------------------------------------------------
# Module-level cached catalogs (deep-copied when mutated) & local opaque helpers
# ---------------------------------------------------------------------------

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
    """Locate the currently VISIBLE card or table row for product_name."""
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
    """Return textContent of each visible product container in DOM order."""
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


def _parse_css_rgb(color_str: str) -> tuple[int, int, int, float]:
    m = re.match(
        r"rgba?\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)"
        r"(?:\s*,\s*(\d+(?:\.\d+)?))?\s*\)",
        color_str.strip(),
        re.I,
    )
    if not m:
        raise AssertionError(f"Could not parse CSS color {color_str!r}")
    r = int(round(float(m.group(1))))
    g = int(round(float(m.group(2))))
    b = int(round(float(m.group(3))))
    a = float(m.group(4)) if m.group(4) is not None else 1.0
    return r, g, b, a


def _luminance(r: int, g: int, b: int) -> float:
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


# ===========================================================================
# F11: Visual theme & latin-ext typography (5 tests)
# ===========================================================================


@pytest.mark.tier1
def test_t1_f11_dark_default_palette_background_and_text_colors(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    styles = desktop_page.evaluate(
        """() => {
            const bodyStyle = window.getComputedStyle(document.body);
            const htmlStyle = window.getComputedStyle(document.documentElement);
            const mainEl = document.querySelector('main');
            const mainStyle = mainEl ? window.getComputedStyle(mainEl) : null;
            return {
                bodyBg: bodyStyle.backgroundColor,
                htmlBg: htmlStyle.backgroundColor,
                bodyColor: bodyStyle.color,
                mainBg: mainStyle ? mainStyle.backgroundColor : null,
            };
        }"""
    )
    bg_str = styles["bodyBg"]
    if _parse_css_rgb(bg_str)[3] == 0.0:
        bg_str = styles["htmlBg"]
    r, g, b, a = _parse_css_rgb(bg_str)
    assert a > 0.5, f"Expected opaque dark background, got {bg_str!r}"
    # Dark espresso/tobacco brown (#1E140F = 30,20,15 or #2B1B12 = 43,27,18)
    assert _luminance(r, g, b) < 70, f"Expected dark background, got rgb({r},{g},{b})"
    assert r >= g >= b, f"Expected warm tobacco-brown background hue, got rgb({r},{g},{b})"

    tr, tg, tb, _ = _parse_css_rgb(styles["bodyColor"])
    assert _luminance(tr, tg, tb) > 190, f"Expected cream light text on dark bg, got rgb({tr},{tg},{tb})"


@pytest.mark.tier1
def test_t1_f11_card_surface_border_and_gold_accent_colors(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    card = _visible_product_container(desktop_page, "Marlboro Red 20 szálas")
    expect(card).to_be_visible()
    card_styles = card.evaluate(
        """el => {
            const s = window.getComputedStyle(el);
            return {
                bg: s.backgroundColor,
                borderColor: s.borderTopColor,
                borderWidth: parseFloat(s.borderTopWidth || '0'),
            };
        }"""
    )
    cr, cg, cb, ca = _parse_css_rgb(card_styles["bg"])
    assert ca > 0.5 and _luminance(cr, cg, cb) < 90, f"Expected dark card surface, got {card_styles}"
    assert cr >= cg >= cb, f"Expected warm brown card surface, got {card_styles}"

    # Verify gold accent (#D4AF37 = 212,175,55 or #E5C158) is actively used on page elements
    has_gold = desktop_page.evaluate(
        """() => {
            const isGold = (col) => {
                const m = /rgba?\\(\\s*(\\d+),\\s*(\\d+),\\s*(\\d+)/i.exec(col || '');
                if (!m) return false;
                const r = +m[1], g = +m[2], b = +m[3];
                return r >= 180 && g >= 140 && b <= 120 && r > b + 80;
            };
            for (const el of document.querySelectorAll('h1, h2, a, button, span, div, article, header')) {
                const s = window.getComputedStyle(el);
                if (isGold(s.color) || isGold(s.backgroundColor) || isGold(s.borderTopColor)) {
                    return true;
                }
            }
            return false;
        }"""
    )
    assert has_gold, "Expected Nemzeti Dohánybolt gold accent (#D4AF37 / warm ochre) in rendered UI"


@pytest.mark.tier1
def test_t1_f11_headings_oswald_uppercase_and_letter_spacing(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    font_links = desktop_page.evaluate(
        """() => Array.from(document.querySelectorAll('link[rel="stylesheet"], style'))
            .map(el => el.getAttribute('href') || el.textContent || '')"""
    )
    joined_links = " ".join(font_links)
    assert re.search(r"(Oswald|Barlow\+Condensed|Barlow%20Condensed)", joined_links, re.I), (
        "Expected Oswald or Barlow Condensed Google Font reference"
    )
    assert "latin-ext" in joined_links.lower() or "fonts.googleapis.com" in joined_links.lower()

    h1_style = desktop_page.locator("h1").first.evaluate(
        """el => {
            const s = window.getComputedStyle(el);
            return {
                fontFamily: s.fontFamily,
                textTransform: s.textTransform,
                letterSpacing: s.letterSpacing,
            };
        }"""
    )
    assert re.search(r"(Oswald|Barlow Condensed)", h1_style["fontFamily"], re.I), (
        f"Expected h1 font-family to include Oswald or Barlow Condensed, got {h1_style!r}"
    )
    assert h1_style["textTransform"] == "uppercase", (
        f"Expected h1 uppercase transform, got {h1_style['textTransform']!r}"
    )
    ls_px = 0.0 if h1_style["letterSpacing"] == "normal" else float(
        re.sub(r"[^\d.\-]", "", h1_style["letterSpacing"]) or 0
    )
    assert ls_px > 0.0, f"Expected positive letter-spacing on h1, got {h1_style['letterSpacing']!r}"


@pytest.mark.tier1
def test_t1_f11_body_barlow_latin_ext_hungarian_diacritics(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    body_font = desktop_page.evaluate("() => window.getComputedStyle(document.body).fontFamily")
    assert re.search(r"(Barlow|PT Sans)", body_font, re.I), (
        f"Expected body font-family to include Barlow or PT Sans, got {body_font!r}"
    )
    card_text = _visible_product_container(
        desktop_page, "Őszi Szűz Fűszeres Pipadohány 50 g Extra Hosszú Kiszerelésben"
    ).inner_text()
    assert "Őszi Szűz Fűszeres Pipadohány 50 g Extra Hosszú Kiszerelésben" in card_text
    assert "\ufffd" not in card_text


@pytest.mark.tier1
def test_t1_f11_prices_tabular_nums_and_multiline_name_wrapping(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    card = _visible_product_container(
        desktop_page, "Őszi Szűz Fűszeres Pipadohány 50 g Extra Hosszú Kiszerelésben"
    )
    metrics = card.evaluate(
        """el => {
            const all = [el, ...el.querySelectorAll('*')];
            let hasTabular = false;
            let nameHasEllipsis = false;
            let nameNoWrap = false;
            let nameHeight = 0;
            let lineHeight = 0;
            for (const node of all) {
                const s = window.getComputedStyle(node);
                const txt = (node.textContent || '').trim();
                if (/\\d+\\s*Ft$/i.test(txt) && s.fontVariantNumeric.includes('tabular-nums')) {
                    hasTabular = true;
                }
                if (txt.includes('Őszi Szűz Fűszeres Pipadohány') && node.children.length === 0) {
                    if (s.textOverflow === 'ellipsis') nameHasEllipsis = true;
                    if (s.whiteSpace === 'nowrap') nameNoWrap = true;
                    nameHeight = node.getBoundingClientRect().height;
                    lineHeight = parseFloat(s.lineHeight) || parseFloat(s.fontSize) * 1.2;
                }
            }
            return { hasTabular, nameHasEllipsis, nameNoWrap, nameHeight, lineHeight };
        }"""
    )
    assert metrics["hasTabular"], f"Expected tabular-nums on price element: {metrics}"
    assert not metrics["nameHasEllipsis"], f"Product name must NOT use text-overflow: ellipsis: {metrics}"
    assert not metrics["nameNoWrap"], f"Product name must allow wrapping (not nowrap): {metrics}"


# ===========================================================================
# F12: Dark/light theme toggle (5 tests)
# ===========================================================================


@pytest.mark.tier1
def test_t1_f12_default_theme_is_dark(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    assert get_current_theme(desktop_page) == "dark"


@pytest.mark.tier1
def test_t1_f12_toggle_switches_to_light_theme_and_palette(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    get_theme_toggle(desktop_page).click()
    assert get_current_theme(desktop_page) == "light"

    light_styles = desktop_page.evaluate(
        """() => {
            const b = window.getComputedStyle(document.body);
            const h = window.getComputedStyle(document.documentElement);
            return { bodyBg: b.backgroundColor, htmlBg: h.backgroundColor, bodyColor: b.color };
        }"""
    )
    bg_str = light_styles["bodyBg"]
    if _parse_css_rgb(bg_str)[3] == 0.0:
        bg_str = light_styles["htmlBg"]
    r, g, b, _ = _parse_css_rgb(bg_str)
    assert _luminance(r, g, b) > 190, f"Expected cream/sand light background, got rgb({r},{g},{b})"

    tr, tg, tb, _ = _parse_css_rgb(light_styles["bodyColor"])
    assert _luminance(tr, tg, tb) < 95, f"Expected dark brown text in light mode, got rgb({tr},{tg},{tb})"


@pytest.mark.tier1
def test_t1_f12_theme_persists_in_localstorage_across_reload(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    get_theme_toggle(desktop_page).click()
    assert get_stored_theme(desktop_page) == "light"

    desktop_page.reload(wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)
    assert get_current_theme(desktop_page) == "light"
    assert get_stored_theme(desktop_page) == "light"


@pytest.mark.tier1
def test_t1_f12_toggle_back_to_dark_restores_dark_palette_and_storage(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    toggle = get_theme_toggle(desktop_page)
    toggle.click()
    assert get_current_theme(desktop_page) == "light"
    toggle.click()
    assert get_current_theme(desktop_page) == "dark"
    assert get_stored_theme(desktop_page) == "dark"

    desktop_page.reload(wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)
    assert get_current_theme(desktop_page) == "dark"


@pytest.mark.tier1
def test_t1_f12_theme_toggle_accessible_name_and_touch_size(
    mobile_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(mobile_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    toggle = get_theme_toggle(mobile_page)
    expect(toggle).to_be_visible()
    box = toggle.bounding_box()
    assert box is not None and box["width"] >= 44.0 and box["height"] >= 44.0
    before_label = (toggle.get_attribute("aria-label") or "") + " " + toggle.inner_text()
    toggle.click()
    after_label = (toggle.get_attribute("aria-label") or "") + " " + toggle.inner_text()
    assert before_label.strip() and after_label.strip()
    assert get_current_theme(mobile_page) == "light"


# ===========================================================================
# F13: Mobile-first responsive layouts (5 tests)
# ===========================================================================


@pytest.mark.tier1
def test_t1_f13_mobile_single_column_zero_overflow_and_touch_targets(
    mobile_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(mobile_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    assert get_grid_column_count(mobile_page) == 1
    assert_no_horizontal_overflow(mobile_page)
    assert_min_touch_targets(mobile_page, min_size=44.0)


@pytest.mark.tier1
def test_t1_f13_mobile_sticky_search_bar_stays_visible_on_scroll(
    mobile_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(mobile_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    mobile_page.evaluate("() => window.scrollTo(0, 700)")
    mobile_page.wait_for_timeout(100)
    search_box = get_search_input(mobile_page).bounding_box()
    assert search_box is not None
    assert 0 <= search_box["y"] < 220, f"Expected sticky search bar near top after scroll, got {search_box}"


@pytest.mark.tier1
def test_t1_f13_mobile_category_chips_scrollable_without_page_overflow(
    mobile_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(mobile_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    assert_no_horizontal_overflow(mobile_page)
    chip = mobile_page.get_by_role("button", name=re.compile(r"Szivarka", re.I)).or_(
        mobile_page.get_by_role("tab", name=re.compile(r"Szivarka", re.I))
    ).first
    chip.scroll_into_view_if_needed()
    chip.click()
    assert_no_horizontal_overflow(mobile_page)
    shown, total = parse_result_counter(mobile_page)
    assert 0 < shown < total


@pytest.mark.tier1
def test_t1_f13_tablet_two_column_grid_layout(
    tablet_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(tablet_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    assert_no_horizontal_overflow(tablet_page)
    assert get_grid_column_count(tablet_page) == 2


@pytest.mark.tier1
def test_t1_f13_desktop_max_width_1280_and_grid_table_view_toggle_persists(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    assert_no_horizontal_overflow(desktop_page)
    max_w = get_max_content_width(desktop_page)
    assert max_w is not None and max_w <= 1280.0
    assert get_grid_column_count(desktop_page) in (3, 4)

    _set_view_mode(desktop_page, "table")
    assert detect_active_view(desktop_page) == "table"
    assert get_stored_view(desktop_page) == "table"

    desktop_page.reload(wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)
    assert detect_active_view(desktop_page) == "table"

    _set_view_mode(desktop_page, "grid")
    assert detect_active_view(desktop_page) == "grid"
    assert get_stored_view(desktop_page) == "grid"


# ===========================================================================
# F14: Debounced normalized multi-word search (5 tests)
# ===========================================================================


@pytest.mark.tier1
def test_t1_f14_single_word_search_filters_matching_products(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    search_products(desktop_page, "Marlboro")
    assert parse_result_counter(desktop_page) == (1, 22)
    expect(_visible_product_container(desktop_page, "Marlboro Red 20 szálas")).to_be_visible()


@pytest.mark.tier1
def test_t1_f14_multi_word_order_independent_search(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    search_products(desktop_page, "red marlboro")
    assert parse_result_counter(desktop_page)[0] >= 1
    expect(_visible_product_container(desktop_page, "Marlboro Red 20 szálas")).to_be_visible()

    search_products(desktop_page, "20 blue fine")
    assert parse_result_counter(desktop_page) == (1, 22)
    expect(_visible_product_container(desktop_page, "Dunhill Fine Cut Blue 20")).to_be_visible()


@pytest.mark.tier1
def test_t1_f14_accent_insensitive_search_across_name_and_category(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    # Search category keyword without accents
    search_products(desktop_page, "hevites")
    shown, total = parse_result_counter(desktop_page)
    assert shown == 5 and total == 22
    expect(_visible_product_container(desktop_page, "TEREA Amber")).to_be_visible()

    # Search ő/ű product without accents
    search_products(desktop_page, "oszi szuz")
    assert parse_result_counter(desktop_page) == (1, 22)
    expect(
        _visible_product_container(
            desktop_page, "Őszi Szűz Fűszeres Pipadohány 50 g Extra Hosszú Kiszerelésben"
        )
    ).to_be_visible()


@pytest.mark.tier1
def test_t1_f14_case_insensitive_search(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    search_products(desktop_page, "sOpIaNaE pIrOs")
    assert parse_result_counter(desktop_page) == (1, 22)
    expect(_visible_product_container(desktop_page, "Sopianae Multifilter Piros 20")).to_be_visible()


@pytest.mark.tier1
def test_t1_f14_debounced_realtime_and_clear_button_resets(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    inp = get_search_input(desktop_page)
    inp.type("szivarka", delay=10)
    # Wait past ~150ms debounce without pressing Enter
    desktop_page.wait_for_timeout(300)
    assert parse_result_counter(desktop_page) == (3, 22)

    clear_search(desktop_page)
    assert inp.input_value() == ""
    assert parse_result_counter(desktop_page) == (22, 22)


# ===========================================================================
# F15: <mark> gold highlighting (5 tests)
# ===========================================================================


@pytest.mark.tier1
def test_t1_f15_single_term_wrapped_in_mark_inside_name(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    search_products(desktop_page, "Marlboro")
    container = _visible_product_container(desktop_page, "Marlboro Red 20 szálas")
    marks = get_mark_texts(desktop_page, container)
    assert any("marlboro" in m.lower() for m in marks), f"Expected <mark> for Marlboro, got {marks}"


@pytest.mark.tier1
def test_t1_f15_mark_preserves_original_hungarian_accents(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    search_products(desktop_page, "oszi")
    container = _visible_product_container(
        desktop_page, "Őszi Szűz Fűszeres Pipadohány 50 g Extra Hosszú Kiszerelésben"
    )
    marks = get_mark_texts(desktop_page, container)
    assert "Őszi" in marks, f"Expected accented 'Őszi' preserved inside <mark>, got {marks}"
    assert "Őszi Szűz Fűszeres Pipadohány 50 g Extra Hosszú Kiszerelésben" in container.inner_text()


@pytest.mark.tier1
def test_t1_f15_multi_word_highlights_multiple_tokens(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    search_products(desktop_page, "red marlboro")
    container = _visible_product_container(desktop_page, "Marlboro Red 20 szálas")
    marks_lower = [m.lower() for m in get_mark_texts(desktop_page, container)]
    assert any("marlboro" in m for m in marks_lower), marks_lower
    assert any("red" in m for m in marks_lower), marks_lower


@pytest.mark.tier1
def test_t1_f15_mark_has_gold_accent_styling(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    search_products(desktop_page, "TEREA")
    mark_el = desktop_page.locator("mark:visible").first
    expect(mark_el).to_be_visible()
    bg = mark_el.evaluate("el => window.getComputedStyle(el).backgroundColor")
    r, g, b, a = _parse_css_rgb(bg)
    assert a > 0.05 and r > b, f"Expected warm gold highlight background on <mark>, got {bg!r}"


@pytest.mark.tier1
def test_t1_f15_clearing_search_removes_all_mark_elements(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    search_products(desktop_page, "TEREA")
    assert len(get_mark_texts(desktop_page)) >= 2
    clear_search(desktop_page)
    assert get_mark_texts(desktop_page) == []


# ===========================================================================
# F16: Result counter & empty-state reset (5 tests)
# ===========================================================================


@pytest.mark.tier1
def test_t1_f16_initial_counter_shows_total_of_total(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    assert parse_result_counter(desktop_page) == (22, 22)


@pytest.mark.tier1
def test_t1_f16_counter_updates_on_search_and_category_filter(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    select_category(desktop_page, "Szivarka")
    assert parse_result_counter(desktop_page) == (3, 22)
    search_products(desktop_page, "Moods")
    assert parse_result_counter(desktop_page) == (1, 22)


@pytest.mark.tier1
def test_t1_f16_empty_state_message_shown_on_zero_matches(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    search_products(desktop_page, "NincsIlyenTermekXYZ999")
    assert parse_result_counter(desktop_page) == (0, 22)
    assert count_rendered_products(desktop_page) == 0
    reset_btn = get_reset_filters_button(desktop_page)
    expect(reset_btn).to_be_visible()


@pytest.mark.tier1
def test_t1_f16_reset_button_clears_search_and_restores_full_count(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    search_products(desktop_page, "NemLetezoMarka123")
    expect(get_reset_filters_button(desktop_page)).to_be_visible()
    get_reset_filters_button(desktop_page).click()
    assert get_search_input(desktop_page).input_value() == ""
    assert parse_result_counter(desktop_page) == (22, 22)


@pytest.mark.tier1
def test_t1_f16_reset_button_clears_combined_category_and_price_change_filters(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    select_category(desktop_page, "Szivar")
    set_price_change_filter(desktop_page, True)
    assert parse_result_counter(desktop_page) == (0, 22)
    get_reset_filters_button(desktop_page).click()
    assert parse_result_counter(desktop_page) == (22, 22)


# ===========================================================================
# F17: Category, price-change filter & sorting (5 tests)
# ===========================================================================


@pytest.mark.tier1
def test_t1_f17_category_chips_populated_and_filter_accurately(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    select_category(desktop_page, "Cigaretta")
    assert parse_result_counter(desktop_page) == (7, 22)
    texts = _get_visible_product_texts(desktop_page)
    assert len(texts) == 7
    assert any("Marlboro Red 20 szálas" in t for t in texts)
    assert not any("Moods Filter 10" in t for t in texts)

    select_category(desktop_page, "Összes")
    assert parse_result_counter(desktop_page) == (22, 22)


@pytest.mark.tier1
def test_t1_f17_price_change_filter_shows_only_changed_and_new(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    expected_changed = [
        item["name"]
        for item in _REALISTIC_22["items"]
        if (item["price_diff"] is not None and item["price_diff"] != 0) or item["is_new"]
    ]
    set_price_change_filter(desktop_page, True)
    shown, total = parse_result_counter(desktop_page)
    assert shown == len(expected_changed) and total == 22
    texts = _get_visible_product_texts(desktop_page)
    for name in expected_changed:
        assert any(name in t for t in texts), f"Missing changed/new item {name!r}"


@pytest.mark.tier1
def test_t1_f17_sort_name_asc_and_desc_hungarian_order(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    items = [
        make_catalog_item(name="Zebra Termék", category="Cigaretta", unit="20 szál", price=2000),
        make_catalog_item(name="Alfa Termék", category="Cigaretta", unit="20 szál", price=2200),
        make_catalog_item(name="Márka Termék", category="Cigaretta", unit="20 szál", price=2100),
    ]
    _load_catalog(desktop_page, app_base_url, route_prices_json, build_prices_json(items))

    _select_sort(desktop_page, "name-asc")
    asc_texts = _get_visible_product_texts(desktop_page)
    assert "Alfa Termék" in asc_texts[0]
    assert "Márka Termék" in asc_texts[1]
    assert "Zebra Termék" in asc_texts[2]

    _select_sort(desktop_page, "name-desc")
    desc_texts = _get_visible_product_texts(desktop_page)
    assert "Zebra Termék" in desc_texts[0]
    assert "Márka Termék" in desc_texts[1]
    assert "Alfa Termék" in desc_texts[2]


@pytest.mark.tier1
def test_t1_f17_sort_price_asc_and_desc(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    # Cheapest is 1890 (VELO Freezing Peppermint 14 g), most expensive is 18500 (Cohiba Siglo II...)
    _select_sort(desktop_page, "price-asc")
    asc_texts = _get_visible_product_texts(desktop_page)
    assert "VELO Freezing Peppermint 14 g" in asc_texts[0]
    assert "Cohiba Siglo II Tubos 3 szálas Prémium Kézzel Sodort Szivar" in asc_texts[-1]

    _select_sort(desktop_page, "price-desc")
    desc_texts = _get_visible_product_texts(desktop_page)
    assert "Cohiba Siglo II Tubos 3 szálas Prémium Kézzel Sodort Szivar" in desc_texts[0]
    assert "VELO Freezing Peppermint 14 g" in desc_texts[-1]


@pytest.mark.tier1
def test_t1_f17_sort_recently_changed_prioritizes_changed_items(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    items = [
        make_catalog_item(name="Régi Változatlan", category="Cigaretta", unit="20 szál", price=2000),
        make_catalog_item(
            name="Korábbi Változott",
            category="Cigaretta",
            unit="20 szál",
            price=2100,
            previous_price=2000,
            changed_at="2026-10-15",
        ),
        make_catalog_item(
            name="Legfrissebb Változott",
            category="Cigaretta",
            unit="20 szál",
            price=2300,
            previous_price=2100,
            changed_at="2026-11-26",
        ),
    ]
    _load_catalog(desktop_page, app_base_url, route_prices_json, build_prices_json(items))
    _select_sort(desktop_page, "changed-desc")
    texts = _get_visible_product_texts(desktop_page)
    assert "Legfrissebb Változott" in texts[0]
    assert "Korábbi Változott" in texts[1]
    assert "Régi Változatlan" in texts[2]


# ===========================================================================
# F18: Favorites localStorage (5 tests)
# ===========================================================================


@pytest.mark.tier1
def test_t1_f18_star_toggle_updates_aria_pressed_and_localstorage(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    expected_id = compute_item_id("Marlboro Red 20 szálas", "20 szál")
    assert toggle_favorite(desktop_page, "Marlboro Red 20 szálas") is True
    assert expected_id in get_stored_favorites(desktop_page)


@pytest.mark.tier1
def test_t1_f18_unstar_removes_id_from_localstorage(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    expected_id = compute_item_id("TEREA Amber", "20 szál")
    assert toggle_favorite(desktop_page, "TEREA Amber") is True
    assert expected_id in get_stored_favorites(desktop_page)
    assert toggle_favorite(desktop_page, "TEREA Amber") is False
    assert expected_id not in get_stored_favorites(desktop_page)


@pytest.mark.tier1
def test_t1_f18_kedvencek_tab_filters_to_starred_items_only(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    toggle_favorite(desktop_page, "Marlboro Red 20 szálas")
    toggle_favorite(desktop_page, "Moods Filter 10")
    get_favorites_tab(desktop_page).click()
    assert parse_result_counter(desktop_page) == (2, 22)
    texts = _get_visible_product_texts(desktop_page)
    assert len(texts) == 2
    assert any("Marlboro Red 20 szálas" in t for t in texts)
    assert any("Moods Filter 10" in t for t in texts)


@pytest.mark.tier1
def test_t1_f18_kedvencek_live_count_updates_dynamically(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    assert get_favorites_count_from_tab(desktop_page) == 0
    toggle_favorite(desktop_page, "Marlboro Red 20 szálas")
    assert get_favorites_count_from_tab(desktop_page) == 1
    toggle_favorite(desktop_page, "TEREA Amber")
    assert get_favorites_count_from_tab(desktop_page) == 2
    toggle_favorite(desktop_page, "Marlboro Red 20 szálas")
    assert get_favorites_count_from_tab(desktop_page) == 1


@pytest.mark.tier1
def test_t1_f18_favorites_persist_across_page_reload(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    expected_id = compute_item_id("Camel Yellow 20 szálas", "20 szál")
    toggle_favorite(desktop_page, "Camel Yellow 20 szálas")

    desktop_page.reload(wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page)
    assert get_stored_favorites(desktop_page) == [expected_id]
    assert get_favorites_count_from_tab(desktop_page) == 1
    get_favorites_tab(desktop_page).click()
    assert parse_result_counter(desktop_page) == (1, 22)
    expect(_visible_product_container(desktop_page, "Camel Yellow 20 szálas")).to_be_visible()


# ===========================================================================
# F19: Price-change badges & metadata (5 tests)
# ===========================================================================


@pytest.mark.tier1
def test_t1_f19_price_increase_badge_and_brick_red_color(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    card = _visible_product_container(desktop_page, "Marlboro Red 20 szálas")
    badge = card.get_by_text(re.compile(r"\+100\s*Ft", re.I)).first
    expect(badge).to_be_visible()
    color_info = badge.evaluate(
        "el => { const s = window.getComputedStyle(el); return { color: s.color, bg: s.backgroundColor }; }"
    )
    r, g, b, _ = _parse_css_rgb(color_info["color"])
    # Brick red (#E05A47 = 224, 90, 71) on text or background
    if not (r > 160 and r > g + 40 and r > b + 40):
        r, g, b, _ = _parse_css_rgb(color_info["bg"])
    assert r > 160 and r > g + 40 and r > b + 40, f"Expected brick-red increase badge, got {color_info}"


@pytest.mark.tier1
def test_t1_f19_price_decrease_badge_and_olive_green_color(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    card = _visible_product_container(desktop_page, "Sopianae Multifilter Piros 20")
    badge = card.get_by_text(re.compile(r"[-−]50\s*Ft", re.I)).first
    expect(badge).to_be_visible()
    color_info = badge.evaluate(
        "el => { const s = window.getComputedStyle(el); return { color: s.color, bg: s.backgroundColor }; }"
    )
    r, g, b, _ = _parse_css_rgb(color_info["color"])
    if not (g > r and g >= b):
        r, g, b, _ = _parse_css_rgb(color_info["bg"])
    assert g > r and g >= b, f"Expected olive-green decrease badge, got {color_info}"


@pytest.mark.tier1
def test_t1_f19_new_item_badge_gold(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    card = _visible_product_container(desktop_page, "VELO Freezing Peppermint 14 g")
    badge = card.get_by_text(re.compile(r"^\s*[ÚU]j\s*$", re.I)).first
    expect(badge).to_be_visible()
    color_info = badge.evaluate(
        "el => { const s = window.getComputedStyle(el); return { color: s.color, bg: s.backgroundColor }; }"
    )
    r, g, b, _ = _parse_css_rgb(color_info["color"])
    if not (r >= 180 and g >= 130 and r > b + 50):
        r, g, b, _ = _parse_css_rgb(color_info["bg"])
    assert r >= 180 and g >= 130 and r > b + 50, f"Expected gold 'Új' badge, got {color_info}"


@pytest.mark.tier1
def test_t1_f19_strikethrough_previous_price_and_valid_from_rendered(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    card = _visible_product_container(desktop_page, "Marlboro Red 20 szálas")
    txt = card.inner_text()
    assert re.search(r"2[\s\u00a0\u202f]?450\s*Ft", txt)
    assert "20 szál" in txt
    assert re.search(r"2026[-.\s]+11[-.\s]+26", txt), f"Expected valid_from date in card: {txt!r}"

    has_strike = card.evaluate(
        """el => {
            for (const node of el.querySelectorAll('*')) {
                const t = (node.textContent || '').replace(/[\\s\\u00a0\\u202f]+/g, ' ');
                if (/2\\s?350\\s*Ft/.test(t)) {
                    const tag = node.tagName.toLowerCase();
                    const td = window.getComputedStyle(node).textDecorationLine || '';
                    if (tag === 'del' || tag === 's' || td.includes('line-through')) return true;
                }
            }
            return false;
        }"""
    )
    assert has_strike, f"Expected strikethrough previous_price 2350 Ft in card text: {txt!r}"


@pytest.mark.tier1
def test_t1_f19_unchanged_item_has_no_spurious_change_badge(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    card = _visible_product_container(desktop_page, "Dunhill Fine Cut Blue 20")
    txt = card.inner_text()
    assert re.search(r"2[\s\u00a0\u202f]?550\s*Ft", txt)
    assert not re.search(r"[+\-−]\d+\s*Ft", txt), f"Unexpected diff badge on unchanged item: {txt!r}"
    assert not re.search(r"\b[ÚU]j\b", txt), f"Unexpected 'Új' badge on unchanged item: {txt!r}"


# ===========================================================================
# F20: Status, source info & error handling (5 tests)
# ===========================================================================


@pytest.mark.tier1
def test_t1_f20_last_updated_and_total_count_displayed(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    payload = copy.deepcopy(_REALISTIC_22)
    payload["meta"]["last_updated"] = "2026-11-25T22:00:00Z"
    _load_catalog(desktop_page, app_base_url, route_prices_json, payload)
    page_text = desktop_page.locator("body").inner_text()
    assert re.search(r"2026[-.\s]+11[-.\s]+25", page_text), "Expected last_updated date rendered"
    assert "22" in page_text


@pytest.mark.tier1
def test_t1_f20_official_sztfh_source_link_present(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    link = get_sztfh_source_link(desktop_page)
    expect(link).to_be_visible()
    href = link.get_attribute("href") or ""
    assert "sztfh.hu" in href.lower()


@pytest.mark.tier1
def test_t1_f20_legal_disclaimer_present(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))
    body_text = desktop_page.locator("body").inner_text()
    assert re.search(r"t[aá]j[eé]koztat[oó]", body_text, re.I), (
        "Expected Hungarian legal disclaimer ('tájékoztató jellegű') on page"
    )


@pytest.mark.tier1
def test_t1_f20_loading_state_transitions_cleanly_to_products(
    desktop_page: Page, app_base_url: str
) -> None:
    import json

    payload_str = json.dumps(_REALISTIC_22, ensure_ascii=False)

    def _delayed_handler(route: Any) -> None:
        time.sleep(0.35)
        route.fulfill(
            status=200,
            content_type="application/json; charset=utf-8",
            body=payload_str,
        )

    desktop_page.route("**/data/prices.json*", _delayed_handler)
    desktop_page.goto(app_base_url, wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page, expect_nonzero=True)
    assert parse_result_counter(desktop_page) == (22, 22)
    desktop_page.unroute_all(behavior="ignoreErrors")


@pytest.mark.tier1
def test_t1_f20_http_error_shows_retry_ui_and_recovers_on_retry(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, 500, wait_for_counter=False)
    retry_btn = get_retry_button(desktop_page)
    expect(retry_btn).to_be_visible(timeout=10000)

    # Switch to valid 200 catalog and click retry
    route_prices_json(desktop_page, copy.deepcopy(_REALISTIC_22))
    retry_btn.click()
    _wait_for_catalog_ready(desktop_page, expect_nonzero=True)
    assert parse_result_counter(desktop_page) == (22, 22)


# ===========================================================================
# F21: Pagination / Load More (5 tests)
# ===========================================================================


@pytest.mark.tier1
def test_t1_f21_large_catalog_renders_bounded_initial_batch(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, _LARGE_4610)
    assert parse_result_counter(desktop_page) == (4610, 4610)
    rendered = count_rendered_products(desktop_page)
    assert 20 <= rendered <= 200, f"Expected bounded initial batch, got {rendered} rendered items"


@pytest.mark.tier1
def test_t1_f21_load_more_button_visible_when_more_items_exist(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, _LARGE_4610)
    expect(get_load_more_button(desktop_page)).to_be_visible()


@pytest.mark.tier1
def test_t1_f21_clicking_load_more_appends_next_batch(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, _LARGE_4610)
    initial = count_rendered_products(desktop_page)
    get_load_more_button(desktop_page).click()
    after = count_rendered_products(desktop_page)
    assert after > initial, f"Expected rendered count to grow after Load More: {initial} -> {after}"
    assert after - initial == initial or after >= initial + 20


@pytest.mark.tier1
def test_t1_f21_multiple_load_more_clicks_fast_no_freeze(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    _load_catalog(desktop_page, app_base_url, route_prices_json, _LARGE_4610)
    counts = [count_rendered_products(desktop_page)]
    btn = get_load_more_button(desktop_page)
    for _ in range(3):
        btn.click()
        counts.append(count_rendered_products(desktop_page))
    assert counts[0] < counts[1] < counts[2] < counts[3], f"Expected strict growth: {counts}"


@pytest.mark.tier1
def test_t1_f21_load_more_hides_when_all_items_loaded(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    catalog_90 = build_large_catalog(90)
    _load_catalog(desktop_page, app_base_url, route_prices_json, catalog_90)
    btn = get_load_more_button(desktop_page)
    clicks = 0
    while btn.count() > 0 and btn.is_visible() and clicks < 10:
        btn.click()
        clicks += 1
    assert count_rendered_products(desktop_page) == 90
    assert btn.count() == 0 or not btn.is_visible()
