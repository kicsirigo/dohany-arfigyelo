from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Browser, Page, Playwright, sync_playwright

APP_ROOT_DEFAULT = Path("/home/mate/dohany_app")
DEFAULT_SCRAPER_SCRIPT = APP_ROOT_DEFAULT / "scripts" / "update_prices.py"
DEFAULT_SZTFH_PAGE_PATH = "/tevekenysegek/dohanyugyi-felugyelet/arak-kozzetetele/"
VENV_PYTHON = Path(sys.executable)


# ---------------------------------------------------------------------------
# Local Static HTTP Server
# ---------------------------------------------------------------------------


class _NoCacheStaticHandler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
    }

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextmanager
def start_static_server(app_dir: Path | str) -> Iterator[str]:
    """Serve `app_dir` over 127.0.0.1:<ephemeral_port> with Cache-Control: no-store."""
    root = Path(app_dir).resolve()
    handler = functools.partial(_NoCacheStaticHandler, directory=str(root))
    server = _QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def app_base_url() -> Iterator[str]:
    """Session-scoped static HTTP server serving DOHANY_E2E_APP_DIR or /home/mate/dohany_app."""
    app_dir = Path(os.environ.get("DOHANY_E2E_APP_DIR", str(APP_ROOT_DEFAULT))).resolve()
    with start_static_server(app_dir) as base_url:
        yield base_url


# ---------------------------------------------------------------------------
# Playwright Browser & Viewport Fixtures
# ---------------------------------------------------------------------------


def _launch_browser(playwright: Playwright) -> Browser:
    requested = os.environ.get("DOHANY_E2E_BROWSER", "").strip().lower()
    if requested == "firefox":
        try:
            return playwright.firefox.launch(headless=True)
        except Exception:
            return playwright.firefox.launch(executable_path="/usr/bin/firefox", headless=True)

    chromium_args = ["--no-sandbox", "--disable-dev-shm-usage"]
    if requested == "chromium":
        return playwright.chromium.launch(headless=True, args=chromium_args)

    try:
        return playwright.chromium.launch(headless=True, args=chromium_args)
    except Exception:
        try:
            return playwright.firefox.launch(headless=True)
        except Exception:
            return playwright.firefox.launch(executable_path="/usr/bin/firefox", headless=True)


@pytest.fixture(scope="session")
def playwright_instance() -> Iterator[Playwright]:
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(playwright_instance: Playwright) -> Iterator[Browser]:
    b = _launch_browser(playwright_instance)
    try:
        yield b
    finally:
        b.close()


def _is_expected_network_console_error(text: str) -> bool:
    lowered = text.lower()
    return (
        "failed to load resource" in lowered
        or "status of 4" in lowered
        or "status of 5" in lowered
        or "networkerror" in lowered
        or "err_failed" in lowered
        or "ns_error_" in lowered
    )


@contextmanager
def _viewport_page(
    browser: Browser,
    width: int,
    height: int,
    *,
    touch: bool = False,
    mobile: bool = False,
) -> Iterator[Page]:
    ctx_kwargs: dict[str, Any] = {
        "viewport": {"width": width, "height": height},
        "locale": "hu-HU",
    }
    if touch:
        ctx_kwargs["has_touch"] = True
    if mobile and browser.browser_type.name == "chromium":
        ctx_kwargs["is_mobile"] = True

    context = browser.new_context(**ctx_kwargs)
    page = context.new_page()
    page._e2e_console_errors = []  # type: ignore[attr-defined]
    page._e2e_page_errors = []  # type: ignore[attr-defined]
    page._e2e_allow_network_console_errors = False  # type: ignore[attr-defined]

    def _on_console(msg: Any) -> None:
        if msg.type == "error":
            page._e2e_console_errors.append(msg.text)  # type: ignore[attr-defined]

    def _on_pageerror(err: Any) -> None:
        page._e2e_page_errors.append(str(err))  # type: ignore[attr-defined]

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)

    try:
        yield page
        page_errors = list(page._e2e_page_errors)  # type: ignore[attr-defined]
        console_errors = list(page._e2e_console_errors)  # type: ignore[attr-defined]
        if page._e2e_allow_network_console_errors:  # type: ignore[attr-defined]
            console_errors = [
                e for e in console_errors if not _is_expected_network_console_error(e)
            ]
        assert not page_errors, f"Uncaught pageerror(s): {page_errors}"
        assert not console_errors, f"Browser console.error(s): {console_errors}"
    finally:
        context.close()


@pytest.fixture
def desktop_page(browser: Browser) -> Iterator[Page]:
    """1440x900 desktop viewport page with strict console/pageerror assertions."""
    with _viewport_page(browser, 1440, 900) as page:
        yield page


@pytest.fixture
def tablet_page(browser: Browser) -> Iterator[Page]:
    """768x1024 tablet viewport page with strict console/pageerror assertions."""
    with _viewport_page(browser, 768, 1024) as page:
        yield page


@pytest.fixture
def mobile_page(browser: Browser) -> Iterator[Page]:
    """375x812 touch mobile viewport page with strict console/pageerror assertions."""
    with _viewport_page(browser, 375, 812, touch=True, mobile=True) as page:
        yield page


@pytest.fixture
def route_prices_json():
    """Return a helper `_route(page, payload_or_status)` to mock `**/data/prices.json*`."""

    def _route(page: Page, payload_or_status: dict[str, Any] | list[Any] | str | int) -> None:
        try:
            page.unroute("**/data/prices.json*")
        except Exception:
            pass

        if isinstance(payload_or_status, int) and payload_or_status >= 400:
            page._e2e_allow_network_console_errors = True  # type: ignore[attr-defined]
        elif payload_or_status == "abort":
            page._e2e_allow_network_console_errors = True  # type: ignore[attr-defined]

        def _handler(route: Any) -> None:
            if payload_or_status == "abort":
                route.abort("failed")
            elif isinstance(payload_or_status, int):
                route.fulfill(
                    status=payload_or_status,
                    content_type="application/json; charset=utf-8",
                    body=json.dumps({"error": f"Simulated HTTP {payload_or_status}"}),
                )
            elif isinstance(payload_or_status, str):
                route.fulfill(
                    status=200,
                    content_type="application/json; charset=utf-8",
                    body=payload_or_status,
                )
            elif isinstance(payload_or_status, (dict, list)):
                route.fulfill(
                    status=200,
                    content_type="application/json; charset=utf-8",
                    body=json.dumps(payload_or_status, ensure_ascii=False),
                )
            else:
                raise TypeError(f"Unsupported payload_or_status: {type(payload_or_status)!r}")

        page.route("**/data/prices.json*", _handler)

    return _route


# ---------------------------------------------------------------------------
# Mock SZTFH Server & Opaque-Box Scraper Runner
# ---------------------------------------------------------------------------


@dataclass
class _MockRoute:
    status: int
    headers: dict[str, str]
    body: bytes
    require_custom_ua: bool | None = None


class MockSztfhServer:
    """Local HTTP server emulating https://sztfh.hu/... endpoints."""

    def __init__(self, *, require_custom_ua: bool = False) -> None:
        self.require_custom_ua = require_custom_ua
        self._lock = threading.Lock()
        self.routes: dict[str, _MockRoute] = {}
        self.recorded_requests: list[dict[str, Any]] = []
        self._server: _QuietThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def origin(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    def set_route(
        self,
        path: str,
        body: bytes | str,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        require_custom_ua: bool | None = None,
    ) -> None:
        norm_path = path if path.startswith("/") else f"/{path}"
        raw = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        with self._lock:
            self.routes[norm_path] = _MockRoute(
                status=status,
                headers=dict(headers or {}),
                body=raw,
                require_custom_ua=require_custom_ua,
            )

    def set_html(
        self,
        html: str,
        path: str = DEFAULT_SZTFH_PAGE_PATH,
        *,
        status: int = 200,
        require_custom_ua: bool | None = None,
    ) -> None:
        self.set_route(
            path,
            html,
            status=status,
            headers={"Content-Type": "text/html; charset=utf-8"},
            require_custom_ua=require_custom_ua,
        )

    def set_xlsx(
        self,
        path: str,
        xlsx_bytes: bytes,
        *,
        status: int = 200,
        content_type: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        require_custom_ua: bool | None = None,
    ) -> None:
        self.set_route(
            path,
            xlsx_bytes,
            status=status,
            headers={"Content-Type": content_type},
            require_custom_ua=require_custom_ua,
        )

    def set_redirect(self, path: str, location: str, *, status: int = 302) -> None:
        self.set_route(
            path,
            b"",
            status=status,
            headers={"Location": location},
        )

    def start(self) -> MockSztfhServer:
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                headers_lower = {k.lower(): v for k, v in self.headers.items()}
                ua = self.headers.get("User-Agent", "")
                orig_url = self.headers.get("X-Original-URL") or f"https://sztfh.hu{self.path}"

                with parent._lock:
                    parent.recorded_requests.append(
                        {
                            "method": "GET",
                            "path": self.path,
                            "url": orig_url,
                            "headers": headers_lower,
                            "user_agent": ua,
                        }
                    )
                    route = parent.routes.get(self.path)
                    if route is None:
                        path_only = urlsplit(self.path).path
                        route = parent.routes.get(path_only)
                    global_ua_req = parent.require_custom_ua

                if route is None:
                    payload = b"Not Found"
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                enforce_ua = global_ua_req if route.require_custom_ua is None else route.require_custom_ua
                if enforce_ua:
                    ua_l = ua.strip().lower()
                    if not ua_l or ua_l.startswith("python-requests/") or ua_l.startswith("python-urllib/"):
                        payload = b"403 Forbidden: custom User-Agent required"
                        self.send_response(403)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return

                self.send_response(route.status)
                for k, v in route.headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(route.body)))
                self.end_headers()
                if route.body:
                    self.wfile.write(route.body)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = _QuietThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


@pytest.fixture
def mock_sztfh_server() -> Iterator[MockSztfhServer]:
    srv = MockSztfhServer().start()
    try:
        yield srv
    finally:
        srv.stop()


_SHIM_SOURCE = r'''
import http.client
import os
import runpy
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import requests.adapters

MOCK_ORIGIN = os.environ["DOHANY_E2E_MOCK_ORIGIN"].rstrip("/")
INTERCEPT_HOSTS = {
    h.strip().lower()
    for h in os.environ.get("DOHANY_E2E_INTERCEPT_HOSTS", "sztfh.hu,www.sztfh.hu").split(",")
    if h.strip()
}
ALLOW_EXTERNAL = os.environ.get("DOHANY_E2E_ALLOW_EXTERNAL", "") == "1"


def _rewrite_url(url: str):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in INTERCEPT_HOSTS:
        orig_origin = f"{parts.scheme}://{parts.netloc}"
        path_qs = parts.path or "/"
        if parts.query:
            path_qs += f"?{parts.query}"
        if parts.fragment:
            path_qs += f"#{parts.fragment}"
        return f"{MOCK_ORIGIN}{path_qs}", orig_origin
    if host and host not in ("127.0.0.1", "localhost") and not ALLOW_EXTERNAL:
        raise ConnectionError(f"Blocked unexpected external network call in E2E test: {url}")
    return url, None


_orig_http_adapter_send = requests.adapters.HTTPAdapter.send


def _patched_http_adapter_send(self, request, *args, **kwargs):
    orig_url = request.url
    rewritten, orig_origin = _rewrite_url(orig_url)
    if orig_origin is not None:
        if "X-Original-URL" not in request.headers:
            request.headers["X-Original-URL"] = orig_url
        request.url = rewritten
    try:
        resp = _orig_http_adapter_send(self, request, *args, **kwargs)
    finally:
        request.url = orig_url

    if orig_origin is not None:
        if isinstance(resp.url, str) and resp.url.startswith(MOCK_ORIGIN):
            resp.url = orig_origin + resp.url[len(MOCK_ORIGIN):]
        if resp.request is not None:
            resp.request.url = orig_url
    return resp


requests.adapters.HTTPAdapter.send = _patched_http_adapter_send

_orig_https_open = urllib.request.HTTPSHandler.https_open
_orig_http_open = urllib.request.HTTPHandler.http_open


def _open_with_rewrite(orig_fn, self, req):
    orig_url = req.full_url
    rewritten, orig_origin = _rewrite_url(orig_url)
    if orig_origin is not None:
        if not req.has_header("X-original-url"):
            req.add_unredirected_header("X-Original-URL", orig_url)
        req.full_url = rewritten
        try:
            resp = self.do_open(http.client.HTTPConnection, req)
        finally:
            req.full_url = orig_url
        resp.url = orig_url
        return resp
    return orig_fn(self, req)


urllib.request.HTTPSHandler.https_open = lambda self, req: _open_with_rewrite(_orig_https_open, self, req)
urllib.request.HTTPHandler.http_open = lambda self, req: _open_with_rewrite(_orig_http_open, self, req)


def main() -> None:
    target_script = sys.argv[1]
    extra_args = sys.argv[2:]
    orig_scripts_dir = os.environ.get("DOHANY_E2E_ORIG_SCRIPTS_DIR", "")
    if orig_scripts_dir and orig_scripts_dir not in sys.path:
        sys.path.append(orig_scripts_dir)
    sys.argv = [target_script, *extra_args]
    sys.path[0] = str(Path(target_script).resolve().parent)
    runpy.run_path(target_script, run_name="__main__")


if __name__ == "__main__":
    main()
'''


@dataclass
class ScraperRunResult:
    returncode: int
    stdout: str
    stderr: str
    workdir: Path
    prices_json_path: Path
    requests: list[dict[str, Any]]

    @property
    def prices_path(self) -> Path:
        return self.prices_json_path

    @property
    def requests_log(self) -> list[dict[str, Any]]:
        return self.requests

    @property
    def raw_bytes(self) -> bytes | None:
        return self.prices_json_path.read_bytes() if self.prices_json_path.exists() else None

    @property
    def mtime_ns(self) -> int | None:
        return self.prices_json_path.stat().st_mtime_ns if self.prices_json_path.exists() else None

    def load_prices_json(self) -> dict[str, Any]:
        return json.loads(self.prices_json_path.read_text(encoding="utf-8"))

    @property
    def data(self) -> dict[str, Any] | None:
        if not self.prices_json_path.exists():
            return None
        try:
            return self.load_prices_json()
        except Exception:
            return None


class ScraperRunner:
    """Executes a scraper script as __main__ in an isolated tmp_path workdir."""

    def __init__(self, tmp_root: Path) -> None:
        self._tmp_root = tmp_root
        self._counter = 0
        self._shim_path = tmp_root / "_e2e_scraper_shim.py"
        self._shim_path.write_text(_SHIM_SOURCE, encoding="utf-8")

    def run(
        self,
        mock_server: MockSztfhServer,
        *,
        workdir: Path | None = None,
        script_path: Path | str | None = None,
        args: list[str] | None = None,
        seed_prices_json: dict[str, Any] | str | bytes | None = None,
        create_data_dir: bool = True,
        timeout: float = 30.0,
        intercept_hosts: str = "sztfh.hu,www.sztfh.hu",
    ) -> ScraperRunResult:
        if workdir is None:
            workdir = self._tmp_root / f"run_{self._counter}"
            self._counter += 1
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)

        src_script = Path(script_path) if script_path is not None else DEFAULT_SCRAPER_SCRIPT
        if not src_script.exists():
            raise FileNotFoundError(f"Target scraper script does not exist: {src_script}")

        dst_scripts_dir = workdir / "scripts"
        dst_scripts_dir.mkdir(parents=True, exist_ok=True)
        dst_script = dst_scripts_dir / src_script.name
        if src_script.resolve() != dst_script.resolve():
            shutil.copy2(src_script, dst_script)

        data_dir = workdir / "data"
        prices_path = data_dir / "prices.json"
        if seed_prices_json is not None:
            data_dir.mkdir(parents=True, exist_ok=True)
            if isinstance(seed_prices_json, dict):
                prices_path.write_text(
                    json.dumps(seed_prices_json, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            elif isinstance(seed_prices_json, str):
                prices_path.write_text(seed_prices_json, encoding="utf-8")
            elif isinstance(seed_prices_json, (bytes, bytearray)):
                prices_path.write_bytes(bytes(seed_prices_json))
            else:
                raise TypeError(f"Unsupported seed_prices_json type: {type(seed_prices_json)!r}")
        elif create_data_dir:
            data_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["DOHANY_E2E_MOCK_ORIGIN"] = mock_server.origin
        env["DOHANY_E2E_INTERCEPT_HOSTS"] = intercept_hosts
        env["DOHANY_E2E_ORIG_SCRIPTS_DIR"] = str(src_script.resolve().parent)
        env["PYTHONUTF8"] = "1"

        req_start_idx = len(mock_server.recorded_requests)
        cmd = [str(VENV_PYTHON), str(self._shim_path), str(dst_script), *(args or [])]
        proc = subprocess.run(
            cmd,
            cwd=str(workdir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

        return ScraperRunResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            workdir=workdir,
            prices_json_path=prices_path,
            requests=list(mock_server.recorded_requests[req_start_idx:]),
        )


@pytest.fixture
def scraper_runner(tmp_path: Path) -> ScraperRunner:
    """Fixture providing an isolated opaque-box scraper runner."""
    return ScraperRunner(tmp_path)
