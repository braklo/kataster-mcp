import json
from pathlib import Path

import pytest

from kataster_mcp.parsers import lv_pdf
from kataster_mcp.parsers.lv import parse_lv
from kataster_mcp.policy import PUBLIC_POLICY
from kataster_mcp.server import create_server

from test_core import FakeTime, _call, _mock_http

FIX = Path(__file__).parent / "fixtures"
HTML = (FIX / "lv_vypis_anon.html").read_text(encoding="utf8")
# Printed from lv_vypis_anon.html (fictitious owners) by headless Chrome; never a real extract.
PDF = (FIX / "lv_vypis_anon_generated.pdf").read_bytes()
# Output of scripts/anonymize_lv_pdf.py on a PDF with other fictitious owners.
RUNS = json.loads((FIX / "lv_pdf_runs_anon.json").read_text(encoding="utf8"))


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_pdf_equals_html_field_by_field():
    h, p = parse_lv(HTML), lv_pdf.parse_lv_pdf(PDF)
    assert h["zdroj_typ"] == "vypis_lv_html" and p["zdroj_typ"] == "vypis_lv_pdf"
    assert {k: v for k, v in p.items() if k != "zdroj_typ"} == {k: v for k, v in h.items() if k != "zdroj_typ"}
    assert len(p["cast_a"]) == 26 and [o["poradie"] for o in p["cast_b"]["vlastnici"]] == [1, 2]
    assert "poznamka_pdf" not in p  # order numbers were found, not guessed


def test_runs_fixture_parses_with_fake_owners_only():
    d = lv_pdf.parse_runs(RUNS)
    assert d["lv"] == "1926" and len(d["cast_a"]) == 26 and d["tarchy"] == "none"
    owners = d["cast_b"]["vlastnici"]
    assert [o["meno"] for o in owners] == ["FIKTÍVNY Vlastník", "VYMYSLENÁ Osoba r. Skúšobná"]
    assert all(o["titul_nadobudnutia"] == "Zmluva č. TEST-1/2000 - 1/00" for o in owners)
    assert d["cast_a"][0] == {"register": "E", "cislo": "503", "vymera_m2": 1286, "druh_pozemku": "Trvalý trávny porast",
                             "povodne_ku": "", "spolocna_nehnutelnost": "Pozemok nie je spoločnou nehnuteľnosťou",
                             "umiestnenie": "Pozemok je umiestnený mimo zastavaného územia obce", "ine_udaje": "Bez zápisu"}


def test_missing_order_number_is_flagged():
    runs = [r for r in RUNS if not (r["t"] == "2" and r["x"] < 100 and r["p"] == 1)]
    d = lv_pdf.parse_runs(runs)
    assert [o["poradie"] for o in d["cast_b"]["vlastnici"]] == [1, 2] and "poznamka_pdf" in d


def test_rejects_non_pdf_and_textless():
    with pytest.raises(ValueError):
        lv_pdf.parse_lv_pdf(b"<html></html>")
    with pytest.raises(ValueError):
        lv_pdf.parse_lv_pdf(b"%PDF-1.4\n%%EOF")


@pytest.mark.anyio
async def test_import_tool_accepts_pdf(tmp_path):
    f = tmp_path / "vypis.pdf"
    f.write_bytes(PDF)
    s = create_server(PUBLIC_POLICY, data_dir=tmp_path, http=_mock_http(PUBLIC_POLICY, lambda r: None, FakeTime()))
    out = await _call(s, "kataster_lv_import", {"path": str(f)})
    assert out["data"]["zdroj_typ"] == "vypis_lv_pdf" and out["data"]["lv"] == "1926"
    assert out["zdroje"][0]["id"] == "lv_import"


def test_portal_pdf_layout_lv1926():
    """Runs of the portal's own PDF (LV 1926, 2026-10-09), anonymized by scripts/anonymize_lv_pdf.py.
    Layout differs from a browser print: date of birth wraps to the next line, page furniture off the page."""
    runs = json.loads((FIX / "lv_pdf_runs_lv1926_anon.json").read_text(encoding="utf8"))
    d = lv_pdf.parse_runs(runs)
    assert d["lv"] == "1926" and len(d["cast_a"]) == 26 and d["cast_c"] == ["Bez tiarch."]
    assert set(d["hlavicka"]) == {"Okres", "Obec", "Katastrálne územie", "Dátum vyhotovenia", "Čas vyhotovenia",
                                  "Údaje platné k"}
    assert all(r["register"] == "E" and r["umiestnenie"].startswith("Pozemok") for r in d["cast_a"])
    owners = d["cast_b"]["vlastnici"]
    assert [o["poradie"] for o in owners] == [1, 2] and "poznamka_pdf" not in d
    assert all(o["meno"] in ("FIKTÍVNY Vlastník", "VYMYSLENÁ Osoba r. Skúšobná") and o["podiel"] == "1/2" for o in owners)
    assert [x["typ"] for x in d["cast_b"]["ine_opravnene_osoby"]] == ["Správca", "Nájomca", "Iná oprávnená osoba"]


def test_page_furniture_filter():
    assert lv_pdf._decoration({"x": 1075.0, "y": 72.0, "t": "1"}, 595.0)
    assert lv_pdf._decoration({"x": 530.0, "y": 36.0, "t": "1 z"}, 595.0)
    assert not lv_pdf._decoration({"x": 88.0, "y": 30.0, "t": "1676/2"}, 595.0)  # a parcel low on the page
    assert not lv_pdf._decoration({"x": 510.0, "y": 40.0, "t": "1/2"}, 595.0)  # a share
