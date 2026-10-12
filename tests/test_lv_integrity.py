"""Offline regressions for incomplete documents and destructive reimports."""

import pytest

from kataster_mcp.lv import IMPORT, LV_VER
from kataster_mcp.parsers.lv import parse_rows
from kataster_mcp.parsers.nahlad import parse_nahlad
from test_core import FIX, _call
from test_osoba import _err, _offline


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("tail", [[], [["ČASŤ C: ŤARCHY"]]])
def test_missing_or_empty_c_is_unknown(tail):
    parsed = parse_rows([["VÝPIS Z LISTU VLASTNÍCTVA č. 1"], *tail])
    assert parsed["tarchy"] == "unknown"


def test_explicit_negative_c_is_preserved():
    parsed = parse_rows(
        [["VÝPIS Z LISTU VLASTNÍCTVA č. 1"], ["ČASŤ C: ŤARCHY"], ["Bez tiarch."]]
    )
    assert parsed["tarchy"] == "none"
    assert parsed["uplnost"]["cast_a"] == "missing"
    assert parsed["uplnost"]["cast_c"] == "read"


def test_partial_extract_is_labelled_even_with_all_sections():
    parsed = parse_rows(
        [
            ["ČIASTOČNÝ VÝPIS Z LISTU VLASTNÍCTVA č. 1"],
            ["ČASŤ A: MAJETKOVÁ PODSTATA"],
            ["ČASŤ B: VLASTNÍCI"],
            ["ČASŤ C: ŤARCHY"],
            ["Bez tiarch."],
        ]
    )
    assert parsed["uplnost"]["rozsah"] == "partial"
    assert parsed["uplnost"]["cast_a"] == "unread"


@pytest.mark.parametrize("content", ["", '<div class="cast-c"><table></table></div>'])
def test_preview_empty_c_is_unknown(content):
    parsed = parse_nahlad(f"<p>Náhľad listu vlastníctva č. 1</p>{content}")
    assert parsed["tarchy"] == "unknown"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "ico", ["abc", "", "   ", "123", "12345678x", "１２３４５６７８"]
)
async def test_invalid_ico_is_rejected_even_with_empty_cache(tmp_path, ico):
    error = await _err(
        _offline(tmp_path), "kataster_osoba_prehlad", {"ico": ico, "nazov": "Test"}
    )
    assert error["typ"] == "invalid_input"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change", ["older", "undated", "partial", "missing_c", "fewer_parcels"]
)
async def test_reimport_does_not_destroy_better_cached_lv(tmp_path, change):
    server = _offline(tmp_path)
    original = (FIX / "lv_vypis_anon.html").read_text(encoding="utf-8")
    await _call(server, "kataster_lv_import", {"html": original})
    before = server.app_state.osoby.get(IMPORT, LV_VER, "871541|1926", None)
    candidate = (
        original.replace("6.10.2026 18:00:00", "6.10.2025 18:00:00")
        if change == "older"
        else original
    )
    if change == "undated":
        candidate = candidate.replace("6.10.2026 18:00:00", "")
    elif change == "partial":
        candidate = candidate.replace("VÝPIS Z LISTU", "ČIASTOČNÝ VÝPIS Z LISTU")
    elif change == "missing_c":
        candidate = candidate[: candidate.index("ČASŤ C:")]
    elif change == "fewer_parcels":
        # Change one real parcel number into an unreadable cell without touching the owner table.
        candidate = candidate.replace(">503<", ">nečitateľné<")
        assert candidate != original
    out = await _call(server, "kataster_lv_import", {"html": candidate})
    after = server.app_state.osoby.get(IMPORT, LV_VER, "871541|1926", None)
    assert after == before
    assert out["data"]["ulozenie"]["stav"] == "preserved_existing"
    assert out["varovania"]


@pytest.mark.anyio
async def test_newer_equivalent_scope_replaces_old_lv(tmp_path):
    server = _offline(tmp_path)
    original = (FIX / "lv_vypis_anon.html").read_text(encoding="utf-8")
    await _call(server, "kataster_lv_import", {"html": original})
    newer = original.replace("6.10.2026 18:00:00", "7.10.2026 18:00:00")
    await _call(server, "kataster_lv_import", {"html": newer})
    stored = server.app_state.osoby.get(IMPORT, LV_VER, "871541|1926", None)[0]["lv"]
    assert stored["hlavicka"]["Údaje platné k"] == "7.10.2026 18:00:00"


@pytest.mark.anyio
async def test_reduced_owner_table_does_not_replace_existing(tmp_path):
    import re

    server = _offline(tmp_path)
    original = (FIX / "lv_vypis_anon.html").read_text(encoding="utf-8")
    await _call(server, "kataster_lv_import", {"html": original})
    before = server.app_state.osoby.get(IMPORT, LV_VER, "871541|1926", None)
    # Remove the second synthetic owner row only.
    head, body = original.split("ČASŤ B:", 1)
    body, count = re.subn(r"<tr>\s*<td[^>]*>2</td>.*?</tr>", "", body, flags=re.S)
    candidate = head + "ČASŤ B:" + body
    assert count == 1
    out = await _call(server, "kataster_lv_import", {"html": candidate})
    assert server.app_state.osoby.get(IMPORT, LV_VER, "871541|1926", None) == before
    assert out["data"]["ulozenie"]["stav"] == "preserved_existing"


@pytest.mark.anyio
async def test_inline_import_respects_size_limit(tmp_path, monkeypatch):
    from kataster_mcp import lv

    monkeypatch.setattr(lv, "MAX_BYTES", 20)
    error = await _err(
        _offline(tmp_path),
        "kataster_lv_import",
        {"html": "<p>VÝPIS Z LISTU VLASTNÍCTVA č. 1</p>"},
    )
    assert error["typ"] == "invalid_input"


@pytest.mark.anyio
async def test_batch_source_points_to_selected_document(tmp_path):
    server = _offline(tmp_path)
    preview = str(FIX / "lv_nahlad_anon.html")
    extract = str(FIX / "lv_vypis_anon.html")
    await _call(server, "kataster_lv_import", {"paths": [preview, extract]})
    stored = server.app_state.osoby.get(IMPORT, LV_VER, "871541|1926", None)[0]
    assert stored["lv"]["zdroj_typ"] == "vypis_lv_html"
    assert stored["zdroj"] == extract
