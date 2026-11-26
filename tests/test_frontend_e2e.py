from __future__ import annotations

import functools
import http.server
from pathlib import Path
import socketserver
import threading

import pytest
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def static_server_url() -> str:
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

    handler = functools.partial(QuietHandler, directory=str(REPO_ROOT))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_desktop_core_flows_and_new_features(static_server_url: str) -> None:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # URL hydration + unit price + native <dialog> price history
        page.goto(f"{static_server_url}/?q=Borkum+Riff+Cherry&sort=price-asc", wait_until="networkidle")
        page.wait_for_selector(".product-card")
        assert page.locator("#search-input").input_value() == "Borkum Riff Cherry"
        assert page.locator("#sort-select").input_value() == "price-asc"
        assert page.locator(".product-card mark.search-mark").count() >= 1
        assert "Ft/g" in page.locator(".product-card .unit-price").first.inner_text()

        # Open native <dialog> history modal & verify focus (not on X), unclipped SVG chart + table
        page.locator(".product-card .history-btn").first.click()
        page.wait_for_selector("dialog#history-modal[open]")
        page.wait_for_selector(".history-chart-svg")
        assert page.locator("#history-table-body tr").count() >= 5
        active_id = page.evaluate("document.activeElement && document.activeElement.id")
        assert active_id != "history-modal-close", f"Close button should not grab focus: {active_id}"
        wrap_box = page.locator(".history-chart-wrap").bounding_box()
        svg_box = page.locator(".history-chart-svg").bounding_box()
        assert wrap_box and svg_box and wrap_box["height"] >= svg_box["height"], f"Chart clipped: wrap={wrap_box}, svg={svg_box}"
        page.keyboard.press("Escape")
        assert page.locator("dialog#history-modal[open]").count() == 0

        # Compact category chips + "Továbbiak" toggle
        more_btn = page.locator("#toggle-more-cats")
        assert more_btn.is_visible()
        assert more_btn.get_attribute("aria-expanded") == "false"
        initial_chip_count = page.locator("#category-chips .chip[data-category]").count()
        more_btn.click()
        assert page.locator("#toggle-more-cats").get_attribute("aria-expanded") == "true"
        assert page.locator("#category-chips .chip[data-category]").count() > initial_chip_count

        # Quick brand chip + recent search + URL sync + unit price (Ft/szál)
        page.locator('.quick-chip[data-quick-query="TEREA"]').click()
        page.wait_for_timeout(200)
        assert "q=TEREA" in page.url
        assert page.locator("#recent-searches-group").is_visible()
        assert "Ft/szál" in page.locator(".product-card .unit-price").first.inner_text()

        # Favorites toggle + persistence
        fav_btn = page.locator(".product-card .fav-btn").first
        fav_btn.click()
        assert fav_btn.get_attribute("aria-pressed") == "true"
        assert page.locator("#favorites-count").inner_text() == "1"
        page.locator("#favorites-toggle").click()
        assert page.locator(".product-card").count() == 1
        page.locator("#favorites-toggle").click()

        # Desktop table view toggle
        page.locator("#view-table-btn").click()
        page.wait_for_selector("#products-table-body tr.product-row")
        assert page.locator("#products-table-body .table-unit-price").first.is_visible()
        page.locator("#view-grid-btn").click()

        # Theme toggle + persistence
        page.locator("#theme-toggle").click()
        assert page.locator("html").get_attribute("data-theme") == "light"

        # Clear search + empty state reset + SVG icons only
        page.locator("#search-clear").click()
        assert page.locator("#search-input").input_value() == ""
        assert page.locator("#theme-toggle svg.icon").count() == 1
        assert page.locator("#share-link-btn svg.icon").count() == 1

        # Zero desktop horizontal overflow
        assert not page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
        browser.close()

    assert not errors, f"Console/page errors: {errors}"


def test_mobile_and_tablet_responsiveness(static_server_url: str) -> None:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width, height in ((375, 812), (768, 1024)):
            page = browser.new_page(viewport={"width": width, "height": height})
            page.on("pageerror", lambda e: errors.append(f"pageerror({width}): {e}"))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.goto(static_server_url, wait_until="networkidle")
            page.wait_for_selector(".product-card")
            assert not page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
            if width == 375:
                for sel in ("#theme-toggle", "#favorites-toggle", "#share-link-btn", ".product-card .fav-btn", ".product-card .history-btn"):
                    box = page.locator(sel).first.bounding_box()
                    assert box is not None and box["width"] >= 44 and box["height"] >= 44, f"{sel} box={box}"
            page.close()
        browser.close()

    assert not errors, f"Console/page errors: {errors}"
