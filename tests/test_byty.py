"""Units (flats, non-residential premises) of a block of flats in the LV preview, offline."""
from pathlib import Path

import pytest

from kataster_mcp.parsers.nahlad import parse_nahlad

FIX = Path(__file__).parent / "fixtures"
FLATS = (FIX / "lv_nahlad_byty_anon.html").read_text(encoding="utf8")


def test_units_and_their_owners():
    d = parse_nahlad(FLATS)
    assert d["jednotky_pocet"] == {"byty": 3, "nebytove_priestory": 2, "byty_podla_hlavicky": 3}
    assert [(u["druh"], u["vchod"], u["cislo"], u["vlastnici_pocet"]) for u in d["jednotky"]] == [
        ("byt", "1", "1", 1), ("byt", "1", "2", 3), ("byt", "1", "3", 2),
        ("nebytovy_priestor", "8", "15", 2), ("nebytovy_priestor", "8", "16", 1)]
    first = d["jednotky"][0]
    assert (first["poschodie"], first["podiel_spolocne"]) == ("prízemie", first["podiel_spolocne"])
    assert "/" in first["podiel_spolocne"] and first["supisne_cislo"].isdigit()
    owners = d["cast_b"]["vlastnici"]
    assert len(owners) == 9 and all("jednotka" in o for o in owners)
    assert owners[0]["jednotka"] == {"druh": "byt", "vchod": "1", "cislo": "1"}
    assert {o["meno"] for o in owners} <= {"FIKTÍVNY Vlastník", "VYMYSLENÁ Osoba r. Skúšobná"}
    assert d["uplnost"]["rozsah"] == "unverified" and "jednotky_poznamka" not in d
    assert d["tarchy"] == "see cast_c"


def test_paged_or_short_preview_is_partial_never_silent():
    paged = parse_nahlad(FLATS.replace('class="cast-c"', '<span>Zobraziť viac</span><div class="cast-c"', 1))
    assert paged["uplnost"]["rozsah"] == "partial" and "jednotky_poznamka" in paged
    short = parse_nahlad(FLATS.replace("Počet bytov:", "Počet bytov: 6<!--", 1).replace("Počet vlastníkov", "--> Počet vlastníkov", 1))
    assert short["jednotky_pocet"]["byty_podla_hlavicky"] == 6 and short["uplnost"]["rozsah"] == "partial"


def test_unit_table_not_read_is_drift():
    with pytest.raises(ValueError, match="unit tables"):
        parse_nahlad(FLATS.replace("Číslo bytu", "Číslo jednotky", 1))


def test_lv_without_units_unchanged():
    d = parse_nahlad((FIX / "lv_nahlad_anon.html").read_text(encoding="utf8"))
    assert "jednotky" not in d and all("jednotka" not in o for o in d["cast_b"]["vlastnici"])


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_import_of_a_block_of_flats(tmp_path):
    from test_core import _call
    from test_osoba import _offline
    s = _offline(tmp_path)
    out = await _call(s, "kataster_lv_import", {"path": str(FIX / "lv_nahlad_byty_anon.html")})
    assert len(out["data"]["jednotky"]) == 5 and out["data"]["cast_b"]["vlastnici"][0]["jednotka"]["cislo"] == "1"
    many = await _call(s, "kataster_lv_import", {"paths": [str(FIX / "lv_nahlad_byty_anon.html"),
                                                           str(FIX / "lv_nahlad_anon.html")]})
    flats = next(x for x in many["data"]["lv"] if x.get("jednotky"))
    assert flats["jednotky"]["byty"] == 3


EXTRACT = (FIX / "lv_vypis_byty_anon.html").read_text(encoding="utf8")


def _extract(page=EXTRACT):
    from kataster_mcp.lv import parse_page
    return parse_page(page.encode("utf-8"))


def test_units_in_the_extract_match_the_preview():
    d, preview = _extract(), parse_nahlad(FLATS)
    assert d["zdroj_typ"] == "vypis_lv_html" and d["jednotky_pocet"] == preview["jednotky_pocet"]
    keys = ("druh", "vchod", "cislo", "poschodie", "supisne_cislo", "podiel_spolocne", "vlastnici_pocet")
    assert [{k: u.get(k) for k in keys} for u in d["jednotky"]] == [{k: u.get(k) for k in keys} for u in preview["jednotky"]]
    assert [u.get("druh_nebytoveho_priestoru") for u in d["jednotky"]] == [None, None, None, "Garáž", "Garáž"]
    owners = d["cast_b"]["vlastnici"]
    assert all("jednotka" in o for o in owners) and all("jednotka" in o for o in d["cast_b"]["ine_opravnene_osoby"])
    pairs = [o for o in owners if o.get("bsm")]
    assert len(pairs) % 2 == 0 and pairs and pairs[0]["poradie"] == pairs[1]["poradie"]
    assert {pairs[0]["meno"], pairs[1]["meno"]} == {"FIKTÍVNY Vlastník", "VYMYSLENÁ Osoba r. Skúšobná"}
    assert (pairs[0]["datum_narodenia"], pairs[1]["datum_narodenia"]) == ("01.01.1900", "02.02.1900")


def test_extract_unit_not_read_is_drift_and_short_is_partial():
    from kataster_mcp.envelope import KatasterError
    with pytest.raises(KatasterError, match="unit headers"):
        _extract(EXTRACT.replace("Číslo bytu", "Číslo jednotky", 1))
    short = _extract(EXTRACT.replace("Počet bytov: 3", "Počet bytov: 4", 1))
    assert short["uplnost"]["rozsah"] == "partial" and "jednotky_poznamka" in short


def test_split_spouses():
    from kataster_mcp.parsers.lv import split_spouses
    pair = split_spouses("NOVÁ Eva r. Stará a NOVÝ Adam, Ulica 1, Mesto, PSČ 000 00, SR, "
                         "Dátum narodenia: 01.01.1900, Dátum narodenia: 02.02.1900")
    assert [(p["meno"], p["datum_narodenia"], p["adresa"]) for p in pair] == [
        ("NOVÁ Eva r. Stará", "01.01.1900", "Ulica 1, Mesto, PSČ 000 00, SR"),
        ("NOVÝ Adam", "02.02.1900", "Ulica 1, Mesto, PSČ 000 00, SR")]
    assert split_spouses("JEDEN Človek, Ulica 1, Dátum narodenia: 01.01.1900") is None


RUNS = (FIX / "lv_pdf_runs_byty_anon.json").read_text(encoding="utf8")


def _pdf(runs_json=RUNS):
    import json

    from kataster_mcp.parsers import lv_pdf
    return lv_pdf.parse_runs(json.loads(runs_json))


def test_units_in_the_pdf_extract_match_the_html_extract():
    d, html = _pdf(), _extract()
    assert d["zdroj_typ"] == "vypis_lv_pdf" and d["jednotky_pocet"] == html["jednotky_pocet"]
    keys = ("druh", "vchod", "cislo", "poschodie", "supisne_cislo", "podiel_spolocne", "vlastnici_pocet",
            "druh_nebytoveho_priestoru")
    assert [{k: u.get(k) for k in keys} for u in d["jednotky"]] == [{k: u.get(k) for k in keys} for u in html["jednotky"]]
    person = ("poradie", "meno", "datum_narodenia", "podiel", "bsm", "jednotka")
    assert [{k: o.get(k) for k in person} for o in d["cast_b"]["vlastnici"]] == \
        [{k: o.get(k) for k in person} for o in html["cast_b"]["vlastnici"]]
    assert "poznamka_pdf" not in d  # every order number found
    assert d["stavby"] == html["stavby"] and d["stavby"][0]["druh_stavby"] == "Bytový dom"  # kind and description apart


def test_pdf_unit_not_read_is_drift():
    from kataster_mcp.envelope import KatasterError
    with pytest.raises((ValueError, KatasterError), match="unit headers"):
        _pdf(RUNS.replace('"Číslo bytu"', '"Číslo jednotky"', 1))


def test_lone_share_like_text_is_a_wrapped_line_not_an_owner():
    from kataster_mcp.parsers import lv_pdf
    assert not lv_pdf._starts_new([{"x": 40.0, "t": "123/45"}])
    assert lv_pdf._starts_new([{"x": 87.5, "t": "NIEKTO, Ulica 1"}, {"x": 509.8, "t": "1/2"}])
