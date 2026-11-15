"""Offline regressions for incomplete LV documents (parts of PR #8)."""

import re

import pytest

from kataster_mcp.lv import IMPORT, LV_VER
from kataster_mcp.parsers.lv import parse_rows
from kataster_mcp.parsers.nahlad import parse_nahlad
from test_core import FIX, _call
from test_osoba import _offline


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("tail", [[], [["ČASŤ C: ŤARCHY"]]])
def test_extract_missing_or_empty_c_is_unknown(tail):
    parsed = parse_rows([["VÝPIS Z LISTU VLASTNÍCTVA č. 1"], *tail])
    assert parsed["tarchy"] == "unknown"


def test_extract_explicit_negative_c_is_none():
    parsed = parse_rows([["VÝPIS Z LISTU VLASTNÍCTVA č. 1"], ["ČASŤ C: ŤARCHY"], ["Bez tiarch."]])
    assert parsed["tarchy"] == "none"
    assert parsed["uplnost"]["cast_a"] == "missing"
    assert parsed["uplnost"]["cast_c"] == "read"
    assert parsed["uplnost"]["rozsah"] == "unverified"


def test_partial_extract_by_heading():
    parsed = parse_rows([["ČIASTOČNÝ VÝPIS Z LISTU VLASTNÍCTVA č. 1"], ["ČASŤ A: MAJETKOVÁ PODSTATA"],
                         ["ČASŤ B: VLASTNÍCI"], ["ČASŤ C: ŤARCHY"], ["Bez tiarch."]])
    assert parsed["uplnost"]["rozsah"] == "partial"
    assert parsed["uplnost"]["cast_a"] == "unread"


def test_word_partial_outside_heading_does_not_count():
    parsed = parse_rows([["VÝPIS Z LISTU VLASTNÍCTVA č. 1"], ["ČASŤ C: ŤARCHY"],
                         ["Vecné bremeno podľa čiastočný výpis zo zmluvy"]])
    assert parsed["uplnost"]["rozsah"] == "unverified"


def test_preview_without_c_section_is_unknown():
    parsed = parse_nahlad("<p>Náhľad listu vlastníctva č. 1</p>")
    assert parsed["tarchy"] == "unknown"
    assert parsed["uplnost"]["cast_c"] == "missing"
    assert "tarchy_poznamka" not in parsed


def test_preview_empty_c_table_is_none_per_preview():
    parsed = parse_nahlad('<p>Náhľad listu vlastníctva č. 1</p><div class="cast-c"><table></table></div>')
    assert parsed["tarchy"] == "none"
    assert "preview" in parsed["tarchy_poznamka"]


def test_preview_c_rows_are_encumbrances():
    page = ('<p>Náhľad listu vlastníctva č. 1</p><div class="cast-c"><table>'
            "<tr><td>1</td><td>Vecné bremeno</td></tr></table></div>")
    assert parse_nahlad(page)["tarchy"] == "see cast_c"


def test_preview_fixture_keeps_none_with_note():
    parsed = parse_nahlad((FIX / "lv_nahlad_anon.html").read_text(encoding="utf-8"))
    assert parsed["tarchy"] == "none" and parsed["tarchy_poznamka"]
    assert parsed["uplnost"]["cast_c"] == "unread"


@pytest.mark.anyio
async def test_import_warns_on_missing_c(tmp_path):
    original = (FIX / "lv_vypis_anon.html").read_text(encoding="utf-8")
    out = await _call(_offline(tmp_path), "kataster_lv_import", {"html": original[: original.index("ČASŤ C:")]})
    assert out["data"]["tarchy"] == "unknown"
    assert any("part C" in w for w in out["varovania"])


KEY = "871541|1926"


def _vypis() -> str:
    return (FIX / "lv_vypis_anon.html").read_text(encoding="utf-8")


def _without_second_owner(page: str) -> str:
    head, body = page.split("ČASŤ B:", 1)
    body, count = re.subn(r"<tr>\s*<td[^>]*>2</td>.*?</tr>", "", body, flags=re.S)
    assert count == 1
    return head + "ČASŤ B:" + body


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["older", "undated", "partial", "missing_c", "fewer_parcels", "fewer_owners"])
async def test_reimport_keeps_better_stored_lv(tmp_path, change):
    server = _offline(tmp_path)
    original = _vypis()
    await _call(server, "kataster_lv_import", {"html": original})
    before = server.app_state.osoby.get(IMPORT, LV_VER, KEY, None)
    candidate = {
        "older": lambda t: t.replace("6.10.2026 18:00:00", "6.10.2025 18:00:00"),
        "undated": lambda t: t.replace("6.10.2026 18:00:00", ""),
        "partial": lambda t: t.replace("VÝPIS Z LISTU", "ČIASTOČNÝ VÝPIS Z LISTU"),
        "missing_c": lambda t: t[: t.index("ČASŤ C:")],
        "fewer_parcels": lambda t: t.replace(">503<", ">nečitateľné<"),
        "fewer_owners": _without_second_owner,
    }[change](original)
    assert candidate != original
    out = await _call(server, "kataster_lv_import", {"html": candidate})
    assert server.app_state.osoby.get(IMPORT, LV_VER, KEY, None) == before
    assert out["data"]["ulozenie"]["stav"] == "preserved_existing"
    assert any("not stored" in w for w in out["varovania"])


@pytest.mark.anyio
async def test_newer_lv_replaces_even_with_fewer_owners(tmp_path):
    server = _offline(tmp_path)
    original = _vypis()
    await _call(server, "kataster_lv_import", {"html": original})
    newer = _without_second_owner(original.replace("6.10.2026 18:00:00", "7.10.2026 18:00:00"))
    out = await _call(server, "kataster_lv_import", {"html": newer})
    stored = server.app_state.osoby.get(IMPORT, LV_VER, KEY, None)[0]["lv"]
    assert stored["hlavicka"]["Údaje platné k"] == "7.10.2026 18:00:00"
    assert out["data"]["ulozenie"]["stav"] == "stored"
    assert any("1 vlastnici" in w for w in out["varovania"])
    assert out["zdroje"][0]["cas"]


@pytest.mark.anyio
async def test_older_file_after_newer_in_one_batch(tmp_path):
    server = _offline(tmp_path)
    original = _vypis()
    older, newer = tmp_path / "a_older.html", tmp_path / "b_newer.html"
    older.write_text(original.replace("6.10.2026 18:00:00", "6.10.2025 18:00:00"), encoding="utf-8")
    newer.write_text(original, encoding="utf-8")
    await _call(server, "kataster_lv_import", {"paths": [str(newer), str(older)]})
    stored = server.app_state.osoby.get(IMPORT, LV_VER, KEY, None)[0]
    assert stored["lv"]["hlavicka"]["Údaje platné k"] == "6.10.2026 18:00:00"
    assert stored["zdroj"] == str(newer)


@pytest.mark.anyio
async def test_batch_source_points_to_selected_document(tmp_path):
    server = _offline(tmp_path)
    preview, extract = str(FIX / "lv_nahlad_anon.html"), str(FIX / "lv_vypis_anon.html")
    await _call(server, "kataster_lv_import", {"paths": [preview, extract]})
    stored = server.app_state.osoby.get(IMPORT, LV_VER, KEY, None)[0]
    assert stored["lv"]["zdroj_typ"] == "vypis_lv_html"
    assert stored["zdroj"] == extract
