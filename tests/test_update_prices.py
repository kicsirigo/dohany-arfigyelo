from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
import time

import openpyxl
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import update_prices as up  # noqa: E402


def _make_xlsx_bytes(
    rows: list[list[object]],
    sheet_title: str = "Munka1",
    extra_sheets: dict[str, list[list[object]]] | None = None,
) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title
    for row in rows:
        ws.append(row)
    if extra_sheets:
        for title, sheet_rows in extra_sheets.items():
            other = wb.create_sheet(title=title)
            for row in sheet_rows:
                other.append(row)
    buf = BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        content: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.content = content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self, routes: dict[str, _FakeResponse], require_custom_ua: bool = True) -> None:
        self.routes = routes
        self.require_custom_ua = require_custom_ua
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, headers: dict[str, str] | None = None, timeout: float = 30):
        hdrs = dict(headers or {})
        self.calls.append((url, hdrs))
        ua = hdrs.get("User-Agent", "")
        if self.require_custom_ua and (not ua or "python-requests" in ua.lower()):
            return _FakeResponse(status_code=403, text="Forbidden")
        if url not in self.routes:
            return _FakeResponse(status_code=404, text="Not Found")
        return self.routes[url]

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# F1: Link discovery & HTTP User-Agent
# ---------------------------------------------------------------------------


def test_find_xlsx_url_prefers_alkalmazando_over_delta_and_2017() -> None:
    page_url = "https://sztfh.hu/tevekenysegek/dohanyugyi-felugyelet/arak-kozzetetele/"
    html = """
    <html><body>
      <ul>
        <li><a href="/downloads/dohany/arkozzetetel/2017_januar_1-jet_kovetoen_alkalmazott_dohanygyartmany_kiskereskedelmi_eladasi_arak_listaja_20261125120001.xlsx">2017-től</a></li>
        <li><a href="/downloads/dohany/arkozzetetel/kiskereskedelmi_eladasi_ar_valtozassal_erintett_dohanygyartmanyok_20261125120001.xlsx">Változással érintett</a></li>
        <li><a href="/downloads/dohany/arkozzetetel/alkalmazando_dohanygyartmany_kiskereskedelmi_eladasi_arak_20261125120001.xlsx?v=1">Alkalmazandó árak</a></li>
      </ul>
    </body></html>
    """
    resolved = up.find_xlsx_url(html, page_url)
    assert resolved == (
        "https://sztfh.hu/downloads/dohany/arkozzetetel/"
        "alkalmazando_dohanygyartmany_kiskereskedelmi_eladasi_arak_20261125120001.xlsx?v=1"
    )


def test_find_xlsx_url_fallback_when_alkalmazando_absent() -> None:
    page_url = "https://example.org/prices/"
    html = """
    <html><body>
      <a href="files/valtozas_2026.xlsx">Változás</a>
      <a href="files/2017_archivum.xlsx">Archív</a>
      <a href="files/teljes_dohany_arlista_2026.xlsx">Teljes lista</a>
    </body></html>
    """
    assert up.find_xlsx_url(html, page_url) == "https://example.org/prices/files/teljes_dohany_arlista_2026.xlsx"


def test_find_xlsx_url_raises_on_no_xlsx() -> None:
    html = '<html><body><a href="/doc.pdf">PDF</a><a href="/2017_old.xlsx">2017</a></body></html>'
    with pytest.raises((ValueError, RuntimeError)):
        up.find_xlsx_url(html, up.SZTFH_PAGE_URL)


def test_fetch_sends_custom_user_agent_and_raises_on_403(tmp_path: Path) -> None:
    page_url = "https://sztfh.hu/tevekenysegek/dohanyugyi-felugyelet/arak-kozzetetele/"
    xlsx_url = "https://sztfh.hu/downloads/alkalmazando_dohanygyartmany_kiskereskedelmi_eladasi_arak_20261125.xlsx"
    html = f'<a href="{xlsx_url}">Lista</a>'
    xlsx_bytes = _make_xlsx_bytes([
        ["Termék megnevezése", "Kiskereskedelmi eladási ár", "Mennyiség", "Mennyiségi egység", "Érvényesség kezdete"],
        ["Marlboro Red 20", 2350, 20, "szál", date(2026, 11, 26)],
    ])

    session = _FakeSession(
        {
            page_url: _FakeResponse(status_code=200, text=html),
            xlsx_url: _FakeResponse(status_code=200, content=xlsx_bytes),
        },
        require_custom_ua=True,
    )
    out = tmp_path / "prices.json"
    written = up.update_prices(page_url=page_url, output_path=out, session=session)
    assert written is True
    assert len(session.calls) == 2
    for _, hdrs in session.calls:
        assert hdrs["User-Agent"] == up.DEFAULT_USER_AGENT

    # Verify non-2xx raises HTTPError
    import requests

    bad_session = _FakeSession({page_url: _FakeResponse(status_code=403)}, require_custom_ua=False)
    with pytest.raises(requests.HTTPError):
        up.update_prices(page_url=page_url, output_path=out, session=bad_session)


# ---------------------------------------------------------------------------
# F2: Dynamic header scan & workbook parsing
# ---------------------------------------------------------------------------


def test_parse_shifted_header_and_reordered_columns() -> None:
    xlsx = _make_xlsx_bytes(
        [
            ["Alkalmazható dohánygyártmány kiskereskedelmi eladási árak 2026. november 26-től"],
            [None],
            ["Frissítve: 2026.11.25."],
            [],
            [
                "Érvényesség kezdete",
                "Kiskereskedelmi eladási ár",
                "Mennyiségi egység",
                "Mennyiség",
                "Termék típus",
                "Termék megnevezése",
            ],
            [date(2026, 11, 26), 2450, "szál", 20, "Cigaretta", "Dunhill Fine Cut Blue 20"],
        ]
    )
    items = up.parse_workbook(xlsx)
    assert len(items) == 1
    assert items[0]["name"] == "Dunhill Fine Cut Blue 20"
    assert items[0]["category"] == "Cigaretta"
    assert items[0]["unit"] == "20 szál"
    assert items[0]["price"] == 2450
    assert items[0]["valid_from"] == "2026-11-26"


def test_parse_skips_empty_footer_and_invalid_price_rows() -> None:
    xlsx = _make_xlsx_bytes(
        [
            ["Termék megnevezése", "Kiskereskedelmi eladási ár", "Mennyiség", "Mennyiségi egység"],
            [None, None, None, None],
            ["   ", "  ", None, None],
            ["TEREA Amber", 2100, 20, "szál"],
            ["Hibás Null Ár", 0, 20, "szál"],
            ["Negatív Ár", -500, 20, "szál"],
            ["Szöveges Ár", "N/A", 20, "szál"],
            ["Összesen", 2100, None, None],
        ]
    )
    items = up.parse_workbook(xlsx)
    assert len(items) == 1
    assert items[0]["name"] == "TEREA Amber"


def test_parse_category_separator_rows_and_default_egyeb() -> None:
    xlsx = _make_xlsx_bytes(
        [
            ["Termék megnevezése", "Mennyiség", "Mennyiségi egység", "Kiskereskedelmi eladási ár", "Érvényesség kezdete"],
            ["Kezdeti Termék", 10, "g", 1800, "2026-11-01"],
            ["Cigaretta", None, None, None, None],
            ["Sopianae Multifilter Piros 20", 20, "szál", 2250, "2026-11-26"],
            ["Szivarka", None, None, None, None],
            ["Handelsgold Classic", 5, "szál", 1600, "2026-11-20"],
            ["Összesen:", None, None, None, None],
        ]
    )
    items = up.parse_workbook(xlsx)
    assert [it["category"] for it in items] == ["Egyéb", "Cigaretta", "Szivarka"]


def test_parse_raises_when_header_missing() -> None:
    xlsx = _make_xlsx_bytes([["Véletlen oszlop", "Másik oszlop"], ["A", 123]])
    with pytest.raises(ValueError, match="header row"):
        up.parse_workbook(xlsx)


# ---------------------------------------------------------------------------
# F3: Exact official product name preservation
# ---------------------------------------------------------------------------


def test_name_preserves_official_text_and_collapses_spaces() -> None:
    raw = "  Borkum\xa0Riff  Cherry\tRuby\nŐűŐŰ 'Special'  "
    assert up.normalize_name(raw) == "Borkum Riff Cherry Ruby ŐűŐŰ 'Special'"
    assert up.normalize_name(None) == ""


# ---------------------------------------------------------------------------
# F4: Deterministic SHA-256 ID & Deduplication
# ---------------------------------------------------------------------------


def test_compute_item_id_deterministic_sha256() -> None:
    name = "Marlboro Red 30g"
    unit = "30 g"
    expected = hashlib.sha256(f"{name}|{unit}".encode("utf-8")).hexdigest()[:16]
    item_id = up.compute_item_id(name, unit)
    assert item_id == expected
    assert re.fullmatch(r"[0-9a-f]{16}", item_id)
    assert up.compute_item_id("AB", "C") != up.compute_item_id("A", "BC")


def test_dedup_keeps_latest_valid_from_regardless_of_order() -> None:
    # Newer date first, older date second (as seen in live SZTFH Alsbo Ruby)
    xlsx1 = _make_xlsx_bytes(
        [
            ["Termék megnevezése", "Mennyiség", "Mennyiségi egység", "Kiskereskedelmi eladási ár", "Érvényesség kezdete"],
            ["Alsbo Ruby", 50, "g", 4500, date(2026, 10, 1)],
            ["Alsbo Ruby", 50, "g", 2550, date(2017, 6, 19)],
        ]
    )
    items1 = up.parse_workbook(xlsx1)
    assert len(items1) == 1
    assert items1[0]["price"] == 4500
    assert items1[0]["valid_from"] == "2026-10-01"

    # Older date first, newer date second
    xlsx2 = _make_xlsx_bytes(
        [
            ["Termék megnevezése", "Mennyiség", "Mennyiségi egység", "Kiskereskedelmi eladási ár", "Érvényesség kezdete"],
            ["Alsbo Ruby", 50, "g", 2550, date(2017, 6, 19)],
            ["Alsbo Ruby", 50, "g", 4500, date(2026, 10, 1)],
        ]
    )
    items2 = up.parse_workbook(xlsx2)
    assert len(items2) == 1
    assert items2[0]["price"] == 4500
    assert items2[0]["valid_from"] == "2026-10-01"


def test_dedup_tie_breaks_with_later_workbook_row() -> None:
    xlsx = _make_xlsx_bytes(
        [
            ["Termék típus", "Termék megnevezése", "Mennyiség", "Mennyiségi egység", "Kiskereskedelmi eladási ár", "Érvényesség kezdete"],
            ["Egyéb nikotinos", "Shiro Virginia Classic", 12, "g", 2300, date(2020, 7, 1)],
            ["Egyéb nikotinos (zárjegyes)", "Shiro Virginia Classic", 12, "g", 2300, date(2020, 7, 1)],
        ]
    )
    items = up.parse_workbook(xlsx)
    assert len(items) == 1
    assert items[0]["category"] == "Egyéb nikotinos (zárjegyes)"


def test_dedup_handles_missing_or_unparseable_valid_from() -> None:
    xlsx = _make_xlsx_bytes(
        [
            ["Termék megnevezése", "Mennyiség", "Mennyiségi egység", "Kiskereskedelmi eladási ár", "Érvényesség kezdete"],
            ["Alpha", 20, "szál", 2100, date(2026, 10, 1)],
            ["Alpha", 20, "szál", 1900, "N/A"],
            ["Beta", 20, "szál", 1500, None],
            ["Beta", 20, "szál", 1800, date(2026, 5, 1)],
            ["Gamma", 20, "szál", 1100, ""],
            ["Gamma", 20, "szál", 1200, None],
        ]
    )
    items = {it["name"]: it for it in up.parse_workbook(xlsx)}
    assert items["Alpha"]["price"] == 2100
    assert items["Alpha"]["valid_from"] == "2026-10-01"
    assert items["Beta"]["price"] == 1800
    assert items["Beta"]["valid_from"] == "2026-05-01"
    assert items["Gamma"]["price"] == 1200


def test_dedup_after_whitespace_normalization() -> None:
    xlsx = _make_xlsx_bytes(
        [
            ["Termék megnevezése", "Mennyiség", "Mennyiségi egység", "Kiskereskedelmi eladási ár", "Érvényesség kezdete"],
            ["Vdg  Claro", 1, "szál", 1200, date(2022, 1, 1)],
            ["Vdg\xa0Claro", 1.0, "szál", 1500, date(2025, 1, 1)],
        ]
    )
    items = up.parse_workbook(xlsx)
    assert len(items) == 1
    assert items[0]["name"] == "Vdg Claro"
    assert items[0]["unit"] == "1 szál"
    assert items[0]["price"] == 1500


# ---------------------------------------------------------------------------
# F5 & F6: Unit, Price & Date normalization
# ---------------------------------------------------------------------------


def test_unit_strips_dot_zero_and_preserves_decimals() -> None:
    assert up.normalize_unit(20.0, "szál") == "20 szál"
    assert up.normalize_unit(16.5, "g") == "16.5 g"
    assert up.normalize_unit(13.68, "g") == "13.68 g"
    assert up.normalize_unit("20.0", "szál") == "20 szál"
    assert up.normalize_unit("17,6", "g") == "17.6 g"
    assert up.normalize_unit(None, None, combined="20  szál") == "20 szál"
    assert up.normalize_unit(None, None, None) == ""


def test_price_parses_int_float_and_ft_strings() -> None:
    assert up.parse_price(2350) == 2350
    assert up.parse_price(2350.0) == 2350
    assert up.parse_price("2 350 Ft") == 2350
    assert up.parse_price("2\xa0350\u202fHUF") == 2350
    assert up.parse_price(0) is None
    assert up.parse_price(-100) is None
    assert up.parse_price(23.5) is None
    assert up.parse_price(True) is None
    assert up.parse_price("abc") is None


def test_valid_from_normalizes_datetime_serial_and_strings() -> None:
    assert up.parse_valid_from(datetime(2026, 11, 26, 14, 30)) == "2026-11-26"
    assert up.parse_valid_from(date(2026, 11, 26)) == "2026-11-26"
    assert up.parse_valid_from(46296) == "2026-10-01"
    assert up.parse_valid_from("2026.11.26.") == "2026-11-26"
    assert up.parse_valid_from("26.11.2026") == "2026-11-26"
    assert up.parse_valid_from("2026-11-26T00:00:00Z") == "2026-11-26"
    assert up.parse_valid_from(None) == ""
    assert up.parse_valid_from("invalid") == ""


# ---------------------------------------------------------------------------
# F8 & F9: Price diffs, Idempotency & Atomic write
# ---------------------------------------------------------------------------


def test_diff_baseline_run_sets_nulls_and_is_new_false() -> None:
    parsed = [
        {
            "id": "a" * 16,
            "name": "Item A",
            "category": "Cigaretta",
            "unit": "20 szál",
            "price": 2350,
            "valid_from": "2026-11-26",
        }
    ]
    out = up.apply_price_diffs(parsed, [], has_prior_catalog=False, today_utc="2026-11-25")
    assert out[0]["previous_price"] is None
    assert out[0]["price_diff"] is None
    assert out[0]["changed_at"] is None
    assert out[0]["is_new"] is False


def test_diff_corrupt_or_empty_prior_falls_back_to_baseline(tmp_path: Path) -> None:
    page_url = "https://sztfh.hu/prices/"
    xlsx_url = "https://sztfh.hu/alkalmazando_dohanygyartmany_kiskereskedelmi_eladasi_arak_20261125.xlsx"
    xlsx_bytes = _make_xlsx_bytes(
        [
            ["Termék megnevezése", "Mennyiség", "Mennyiségi egység", "Kiskereskedelmi eladási ár", "Érvényesség kezdete"],
            ["Marlboro Red", 20, "szál", 2350, "2026-11-26"],
        ]
    )
    out_path = tmp_path / "prices.json"
    out_path.write_text("{corrupt json", encoding="utf-8")

    session = _FakeSession(
        {
            page_url: _FakeResponse(text=f'<a href="{xlsx_url}">A</a>'),
            xlsx_url: _FakeResponse(content=xlsx_bytes),
        }
    )
    written = up.update_prices(page_url=page_url, output_path=out_path, session=session)
    assert written is True
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["items"][0]["is_new"] is False
    assert data["items"][0]["previous_price"] is None


def test_diff_price_increase_decrease_and_new_item() -> None:
    prior = [
        {
            "id": "1" * 16,
            "name": "Up",
            "category": "Cigaretta",
            "unit": "20 szál",
            "price": 2000,
            "valid_from": "2026-10-01",
            "previous_price": None,
            "price_diff": None,
            "changed_at": None,
            "is_new": False,
        },
        {
            "id": "2" * 16,
            "name": "Down",
            "category": "Cigaretta",
            "unit": "20 szál",
            "price": 2500,
            "valid_from": "2026-10-01",
            "previous_price": None,
            "price_diff": None,
            "changed_at": None,
            "is_new": False,
        },
    ]
    parsed = [
        {"id": "1" * 16, "name": "Up", "category": "Cigaretta", "unit": "20 szál", "price": 2050, "valid_from": "2026-11-26"},
        {"id": "2" * 16, "name": "Down", "category": "Cigaretta", "unit": "20 szál", "price": 2470, "valid_from": "2026-11-26"},
        {"id": "3" * 16, "name": "New", "category": "Szivar", "unit": "1 szál", "price": 5000, "valid_from": "2026-11-26"},
    ]
    out = up.apply_price_diffs(parsed, prior, has_prior_catalog=True, today_utc="2026-11-25")
    by_id = {it["id"]: it for it in out}
    assert by_id["1" * 16]["previous_price"] == 2000
    assert by_id["1" * 16]["price_diff"] == 50
    assert by_id["1" * 16]["changed_at"] == "2026-11-25"
    assert by_id["1" * 16]["is_new"] is False

    assert by_id["2" * 16]["previous_price"] == 2500
    assert by_id["2" * 16]["price_diff"] == -30
    assert by_id["2" * 16]["changed_at"] == "2026-11-25"
    assert by_id["2" * 16]["is_new"] is False

    assert by_id["3" * 16]["previous_price"] is None
    assert by_id["3" * 16]["price_diff"] is None
    assert by_id["3" * 16]["changed_at"] == "2026-11-25"
    assert by_id["3" * 16]["is_new"] is True


def test_diff_unchanged_price_preserves_prior_diff_and_clears_is_new() -> None:
    prior = [
        {
            "id": "1" * 16,
            "name": "SamePriceChangedEarlier",
            "category": "Cigaretta",
            "unit": "20 szál",
            "price": 2050,
            "valid_from": "2026-11-20",
            "previous_price": 2000,
            "price_diff": 50,
            "changed_at": "2026-11-20",
            "is_new": False,
        },
        {
            "id": "2" * 16,
            "name": "WasNewYesterday",
            "category": "Szivar",
            "unit": "1 szál",
            "price": 5000,
            "valid_from": "2026-11-20",
            "previous_price": None,
            "price_diff": None,
            "changed_at": "2026-11-20",
            "is_new": True,
        },
        {
            "id": "3" * 16,
            "name": "RemovedItem",
            "category": "Szivar",
            "unit": "1 szál",
            "price": 1000,
            "valid_from": "2026-10-01",
            "previous_price": None,
            "price_diff": None,
            "changed_at": None,
            "is_new": False,
        },
    ]
    parsed = [
        {"id": "1" * 16, "name": "SamePriceChangedEarlier", "category": "Cigaretta", "unit": "20 szál", "price": 2050, "valid_from": "2026-11-20"},
        {"id": "2" * 16, "name": "WasNewYesterday", "category": "Szivar", "unit": "1 szál", "price": 5000, "valid_from": "2026-11-20"},
    ]
    out = up.apply_price_diffs(parsed, prior, has_prior_catalog=True, today_utc="2026-11-25")
    assert len(out) == 2
    by_id = {it["id"]: it for it in out}
    assert by_id["1" * 16]["previous_price"] == 2000
    assert by_id["1" * 16]["price_diff"] == 50
    assert by_id["1" * 16]["changed_at"] == "2026-11-20"
    assert by_id["1" * 16]["is_new"] is False

    assert by_id["2" * 16]["is_new"] is False
    assert by_id["2" * 16]["changed_at"] == "2026-11-20"


def test_idempotent_noop_preserves_mtime_bytes_and_is_new(tmp_path: Path) -> None:
    page_url = "https://sztfh.hu/prices/"
    xlsx_url = "https://sztfh.hu/alkalmazando_dohanygyartmany_kiskereskedelmi_eladasi_arak_20261125.xlsx"
    xlsx_v1 = _make_xlsx_bytes(
        [
            ["Termék megnevezése", "Mennyiség", "Mennyiségi egység", "Kiskereskedelmi eladási ár", "Érvényesség kezdete"],
            ["Existing", 20, "szál", 2300, "2026-11-20"],
        ]
    )
    xlsx_v2 = _make_xlsx_bytes(
        [
            ["Termék megnevezése", "Mennyiség", "Mennyiségi egység", "Kiskereskedelmi eladási ár", "Érvényesség kezdete"],
            ["Existing", 20, "szál", 2300, "2026-11-20"],
            ["Brand New", 10, "g", 1500, "2026-11-26"],
        ]
    )
    out_path = tmp_path / "data" / "prices.json"
    xlsx_url_v1 = "https://sztfh.hu/alkalmazando_dohanygyartmany_kiskereskedelmi_eladasi_arak_20261124.xlsx"

    session1 = _FakeSession(
        {
            page_url: _FakeResponse(text=f'<a href="{xlsx_url_v1}">V1</a>'),
            xlsx_url_v1: _FakeResponse(content=xlsx_v1),
        }
    )
    assert up.update_prices(
        page_url=page_url,
        output_path=out_path,
        now=datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc),
        session=session1,
    ) is True

    # Run 2 introduces Brand New (is_new=True)
    session2 = _FakeSession(
        {
            page_url: _FakeResponse(text=f'<a href="{xlsx_url}">V2</a>'),
            xlsx_url: _FakeResponse(content=xlsx_v2),
        }
    )
    assert up.update_prices(
        page_url=page_url,
        output_path=out_path,
        now=datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc),
        session=session2,
    ) is True

    before_bytes = out_path.read_bytes()
    before_mtime_ns = out_path.stat().st_mtime_ns
    time.sleep(0.01)

    # Run 3 processes exact same workbook/source_file -> must be idempotent no-op!
    assert up.update_prices(
        page_url=page_url,
        output_path=out_path,
        now=datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc),
        session=session2,
    ) is False

    assert out_path.stat().st_mtime_ns == before_mtime_ns
    assert out_path.read_bytes() == before_bytes
    data = json.loads(before_bytes.decode("utf-8"))
    new_item = next(it for it in data["items"] if it["name"] == "Brand New")
    assert new_item["is_new"] is True
    assert data["meta"]["last_updated"] == "2026-11-25T10:00:00Z"


def test_new_source_file_with_same_prices_updates_meta(tmp_path: Path) -> None:
    page_url = "https://sztfh.hu/prices/"
    url1 = "https://sztfh.hu/alkalmazando_dohanygyartmany_kiskereskedelmi_eladasi_arak_20261124.xlsx"
    url2 = "https://sztfh.hu/alkalmazando_dohanygyartmany_kiskereskedelmi_eladasi_arak_20261125.xlsx"
    xlsx_bytes = _make_xlsx_bytes(
        [
            ["Termék megnevezése", "Mennyiség", "Mennyiségi egység", "Kiskereskedelmi eladási ár", "Érvényesség kezdete"],
            ["Marlboro Red", 20, "szál", 2350, "2026-11-26"],
        ]
    )
    out_path = tmp_path / "prices.json"

    s1 = _FakeSession({page_url: _FakeResponse(text=f'<a href="{url1}">1</a>'), url1: _FakeResponse(content=xlsx_bytes)})
    assert up.update_prices(page_url=page_url, output_path=out_path, now=datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc), session=s1) is True

    s2 = _FakeSession({page_url: _FakeResponse(text=f'<a href="{url2}">2</a>'), url2: _FakeResponse(content=xlsx_bytes)})
    assert up.update_prices(page_url=page_url, output_path=out_path, now=datetime(2026, 11, 25, 8, 0, tzinfo=timezone.utc), session=s2) is True

    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["meta"]["source_file"] == "alkalmazando_dohanygyartmany_kiskereskedelmi_eladasi_arak_20261125.xlsx"
    assert data["meta"]["source_url"] == url2
    assert data["meta"]["last_updated"] == "2026-11-25T08:00:00Z"


def test_atomic_write_utf8_and_preserves_prior_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_path = tmp_path / "prices.json"
    initial = {"meta": {"total_count": 1}, "items": [{"name": "ŐűÁÉ"}]}
    up.atomic_write_json(out_path, initial)
    raw = out_path.read_text(encoding="utf-8")
    assert "ŐűÁÉ" in raw
    assert "\\u0150" not in raw
    assert raw.endswith("\n")

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError, match="disk full"):
        up.atomic_write_json(out_path, {"meta": {"total_count": 0}, "items": []})

    assert json.loads(out_path.read_text(encoding="utf-8")) == initial
    leftover_tmps = list(tmp_path.glob("*.tmp"))
    assert leftover_tmps == []


def test_cwd_relative_default_output_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert up.DEFAULT_OUTPUT_PATH == Path("data/prices.json")
    assert not (tmp_path / "data" / "prices.json").exists()
    up.atomic_write_json(up.DEFAULT_OUTPUT_PATH, {"meta": {"total_count": 0}, "items": []})
    assert (tmp_path / "data" / "prices.json").exists()


def test_repo_data_prices_json_schema_if_present() -> None:
    prices_path = REPO_ROOT / "data" / "prices.json"
    if not prices_path.exists():
        pytest.skip("data/prices.json not generated yet")

    data = json.loads(prices_path.read_text(encoding="utf-8"))
    meta = data["meta"]
    items = data["items"]

    for key in ("last_updated", "source_file", "source_url", "page_url", "total_count"):
        assert key in meta
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", meta["last_updated"])
    assert meta["source_file"].endswith(".xlsx")
    assert meta["source_url"].startswith("https://")
    assert meta["page_url"] == up.SZTFH_PAGE_URL
    assert isinstance(items, list) and len(items) > 0
    assert meta["total_count"] == len(items)

    seen_ids: set[str] = set()
    for item in items:
        assert set(item.keys()) == {
            "id",
            "name",
            "category",
            "unit",
            "price",
            "valid_from",
            "previous_price",
            "price_diff",
            "changed_at",
            "is_new",
        }
        assert re.fullmatch(r"[0-9a-f]{16}", item["id"])
        assert item["id"] not in seen_ids
        seen_ids.add(item["id"])
        assert item["name"] == " ".join(item["name"].split()) and len(item["name"]) > 0
        assert isinstance(item["category"], str) and len(item["category"]) > 0
        assert isinstance(item["unit"], str)
        assert type(item["price"]) is int and item["price"] > 0
        if item["valid_from"] != "":
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["valid_from"])
        assert isinstance(item["is_new"], bool)
        if item["previous_price"] is None:
            assert item["price_diff"] is None
        else:
            assert type(item["previous_price"]) is int
            assert type(item["price_diff"]) is int
            assert item["price_diff"] == item["price"] - item["previous_price"]
        if item["changed_at"] is not None:
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["changed_at"])


def test_find_history_xlsx_url() -> None:
    html = """
    <html><body>
      <a href="/downloads/dohany/arkozzetetel/alkalmazando_dohanygyartmany_kiskereskedelmi_eladasi_arak_20261125120001.xlsx">Aktív</a>
      <a href="/downloads/dohany/arkozzetetel/kiskereskedelmi_eladasi_ar_valtozassal_erintett_20261125120001.xlsx">Változás</a>
      <a href="/downloads/dohany/arkozzetetel/2017_januar_1-jet_kovetoen_alkalmazott_dohanygyartmany_kiskereskedelmi_eladasi_arak_listaja_20261125120001.xlsx">2017+ lista</a>
    </body></html>
    """
    found = up.find_history_xlsx_url(html, up.SZTFH_PAGE_URL)
    assert found is not None
    assert "2017_januar_1-jet_kovetoen" in found
    assert up.find_history_xlsx_url("<html><body><a href='/a.xlsx'>Only active</a></body></html>", up.SZTFH_PAGE_URL) is None


def test_parse_history_workbook_dedupes_and_filters() -> None:
    wb_bytes = _make_xlsx_bytes([
        ["2017+ lista"],
        ["Termék típus", "Termék megnevezése", "Mennyiség", "Mennyiségi egység", "Kiskereskedelmi eladási ár", "Érvényesség kezdete"],
        ["Cigaretta", "Marlboro  Red", 20, "szál", 1200, "2018-01-01"],
        ["Cigaretta", "Marlboro Red", 20, "szál", 1200, "2018-06-01"],  # same consecutive price -> compacted
        ["Cigaretta", "Marlboro Red", 20, "szál", 1500, "2020-01-01"],
        ["Cigaretta", "Marlboro Red", 20, "szál", 2300, "2025-05-01"],
        ["Cigaretta", "Inactive Brand", 20, "szál", 900, "2019-01-01"],
    ])
    marlboro_id = up.compute_item_id("Marlboro Red", "20 szál")
    hist = up.parse_history_workbook(wb_bytes, active_ids={marlboro_id})
    assert list(hist.keys()) == [marlboro_id]
    assert hist[marlboro_id] == [
        ["2018-01-01", 1200],
        ["2020-01-01", 1500],
        ["2025-05-01", 2300],
    ]


def test_repo_price_history_json_schema_if_present() -> None:
    history_path = REPO_ROOT / "data" / "price_history.json"
    if not history_path.exists():
        pytest.skip("data/price_history.json not generated yet")
    data = json.loads(history_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and len(data) > 0
    sample_id, points = next(iter(data.items()))
    assert re.fullmatch(r"[0-9a-f]{16}", sample_id)
    assert isinstance(points, list) and len(points) >= 1
    for pt in points:
        assert isinstance(pt, list) and len(pt) == 2
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", pt[0])
        assert type(pt[1]) is int and pt[1] > 0
