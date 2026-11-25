from __future__ import annotations

import copy
import re
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from helpers import (
    assert_no_horizontal_overflow,
    build_prices_json,
    build_realistic_catalog,
    detect_active_view,
    get_current_theme,
    get_favorites_count_from_tab,
    get_favorites_tab,
    get_product_container,
    get_product_star_button,
    get_result_counter_locator,
    get_search_input,
    get_sztfh_source_link,
    get_theme_toggle,
    make_catalog_item,
    parse_result_counter,
    search_products,
    set_view_mode,
)

_REALISTIC_22 = build_realistic_catalog(count=22)


def _wait_for_catalog_ready(page: Page, *, expect_nonzero: bool = True) -> None:
    counter = get_result_counter_locator(page)
    expect(counter).to_be_visible(timeout=10000)
    if expect_nonzero:
        expect(counter).not_to_have_text(
            re.compile(r"^\s*0\s*/\s*0\s*term[eé]k\s*$", re.I),
            timeout=10000,
        )


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


def _measure_table_cells_overflow(page: Page) -> dict[str, Any]:
    return dict(
        page.evaluate(
            """() => {
                const thViolations = [];
                const tdViolations = [];
                const ths = Array.from(document.querySelectorAll('.products-table th'));
                for (const th of ths) {
                    const thRect = th.getBoundingClientRect();
                    const range = document.createRange();
                    range.selectNodeContents(th);
                    const textRect = range.getBoundingClientRect();
                    if (th.scrollWidth > th.clientWidth || textRect.right > thRect.right + 0.5) {
                        thViolations.push({
                            className: th.className,
                            text: (th.textContent || '').trim(),
                            scrollWidth: th.scrollWidth,
                            clientWidth: th.clientWidth,
                            thRight: Math.round(thRect.right * 100) / 100,
                            textRight: Math.round(textRect.right * 100) / 100,
                        });
                    }
                }
                const tds = Array.from(document.querySelectorAll('.products-table tbody tr td'));
                for (const td of tds) {
                    if (td.scrollWidth > td.clientWidth + 1) {
                        tdViolations.push({
                            className: td.className,
                            text: (td.textContent || '').trim().slice(0, 60),
                            scrollWidth: td.scrollWidth,
                            clientWidth: td.clientWidth,
                        });
                    }
                }
                return {
                    thCount: ths.length,
                    tdCount: tds.length,
                    thViolations,
                    tdViolations,
                };
            }"""
        )
    )


@pytest.mark.tier5
@pytest.mark.parametrize(("width", "height"), [(1025, 800), (1280, 900)])
def test_t5_desktop_table_cells_no_horizontal_overflow(
    desktop_page: Page, app_base_url: str, width: int, height: int
) -> None:
    desktop_page.set_viewport_size({"width": width, "height": height})
    desktop_page.goto(app_base_url, wait_until="domcontentloaded")
    _wait_for_catalog_ready(desktop_page, expect_nonzero=True)

    set_view_mode(desktop_page, "table")
    assert detect_active_view(desktop_page) == "table"
    assert_no_horizontal_overflow(desktop_page)

    report = _measure_table_cells_overflow(desktop_page)
    assert report["thCount"] == 7
    assert report["tdCount"] > 0
    assert not report["thViolations"], f"Header overflow at {width}x{height}: {report['thViolations']}"
    assert not report["tdViolations"], f"Cell overflow at {width}x{height}: {report['tdViolations']}"

    desktop_page.locator("#sort-select").select_option("price-desc")
    report_sorted = _measure_table_cells_overflow(desktop_page)
    assert not report_sorted["thViolations"], (
        f"Header overflow (price-desc) at {width}x{height}: {report_sorted['thViolations']}"
    )
    assert not report_sorted["tdViolations"], (
        f"Cell overflow (price-desc) at {width}x{height}: {report_sorted['tdViolations']}"
    )


@pytest.mark.tier5
def test_t5_blocked_local_storage_falls_back_cleanly(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    desktop_page.add_init_script(
        """() => {
            Storage.prototype.getItem = function () {
                throw new DOMException('Blocked by policy', 'SecurityError');
            };
            Storage.prototype.setItem = function () {
                throw new DOMException('Storage quota exceeded', 'QuotaExceededError');
            };
        }"""
    )

    _load_catalog(desktop_page, app_base_url, route_prices_json, copy.deepcopy(_REALISTIC_22))

    assert get_current_theme(desktop_page) == "dark"
    get_theme_toggle(desktop_page).click()
    assert get_current_theme(desktop_page) == "light"
    get_theme_toggle(desktop_page).click()
    assert get_current_theme(desktop_page) == "dark"

    target_name = "Marlboro Red 20 szálas"
    star_btn = get_product_star_button(get_product_container(desktop_page, target_name))
    expect(star_btn).to_have_attribute("aria-pressed", "false")
    star_btn.click()
    expect(star_btn).to_have_attribute("aria-pressed", "true")
    assert get_favorites_count_from_tab(desktop_page) == 1

    get_favorites_tab(desktop_page).click()
    assert parse_result_counter(desktop_page) == (1, 22)

    get_product_star_button(get_product_container(desktop_page, target_name)).click()
    assert get_favorites_count_from_tab(desktop_page) == 0


@pytest.mark.tier5
def test_t5_escape_clears_search_and_unsafe_page_url_sanitized(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    payload = copy.deepcopy(_REALISTIC_22)
    payload["meta"]["page_url"] = "javascript:window.__xss=3"

    _load_catalog(desktop_page, app_base_url, route_prices_json, payload)

    xss_flag = desktop_page.evaluate("() => window.__xss || 0")
    assert xss_flag == 0

    source_href = get_sztfh_source_link(desktop_page).get_attribute("href")
    assert source_href is not None
    assert source_href.startswith("https://sztfh.hu/"), f"Unexpected href: {source_href!r}"
    assert not source_href.lower().startswith("javascript:")

    search_input = get_search_input(desktop_page)
    search_products(desktop_page, "Marlboro")
    shown, total = parse_result_counter(desktop_page)
    assert total == 22
    assert 0 < shown < total

    search_input.press("Escape")
    assert search_input.input_value() == ""
    assert parse_result_counter(desktop_page) == (22, 22)
    expect(desktop_page.locator("#search-clear")).to_be_hidden()


@pytest.mark.tier5
def test_t5_favorite_toggle_with_selector_metacharacter_id(
    desktop_page: Page, app_base_url: str, route_prices_json: Any
) -> None:
    tricky_id = 'id"with\'quote&<tag>>chars'
    tricky_name = "Speciális Azonosítós Termék 20"
    tricky_item = make_catalog_item(
        name=tricky_name,
        category="Cigaretta",
        unit="20 szál",
        price=2490,
    )
    tricky_item["id"] = tricky_id
    normal_item = make_catalog_item(
        name="Normál Referencia Termék 20",
        category="Cigaretta",
        unit="20 szál",
        price=2190,
    )
    payload = build_prices_json([tricky_item, normal_item])

    _load_catalog(desktop_page, app_base_url, route_prices_json, payload)

    card = get_product_container(desktop_page, tricky_name)
    star_btn = get_product_star_button(card)
    expect(star_btn).to_have_attribute("aria-pressed", "false")

    star_btn.click()
    expect(star_btn).to_have_attribute("aria-pressed", "true")
    assert desktop_page.locator("#favorites-count").inner_text() == "1"

    set_view_mode(desktop_page, "table")
    assert detect_active_view(desktop_page) == "table"
    row_star_btn = get_product_star_button(get_product_container(desktop_page, tricky_name))
    expect(row_star_btn).to_have_attribute("aria-pressed", "true")

    row_star_btn.click()
    expect(row_star_btn).to_have_attribute("aria-pressed", "false")
    assert desktop_page.locator("#favorites-count").inner_text() == "0"
