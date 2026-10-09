import json
import xml.dom.minidom
from pathlib import Path

from PIL import Image
from shapely.geometry import shape

from kataster_mcp import render
from kataster_mcp.sources import wfs

FIX = Path(__file__).parent / "fixtures"
PARCEL = {**wfs.parse_parcel(json.loads((FIX / "wfs_e503.json").read_text(encoding="utf-8"))["features"][0], "E"), "ku_nazov": "Vyšný Kubín"}
SHAPE = shape(PARCEL["geometria"])


def test_frame_keeps_true_proportions():
    f = render.Frame(SHAPE.bounds)
    w_m = (f.maxx - f.minx) * f.k * 111320
    h_m = (f.maxy - f.miny) * 110540
    assert abs((w_m / h_m) - (f.width / f.height)) < 0.01
    assert max(f.width, f.height) == render.MAX_SIDE


def test_png_renders_on_blank_background():
    f = render.Frame(SHAPE.bounds)
    bg = Image.new("RGBA", (f.width, f.height), (255, 255, 255, 255))
    png = render.render_png(bg, f, [PARCEL], [SHAPE], "orto", True)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_exports_carry_no_owner_fields():
    owner_like = {**PARCEL, "vlastnici": [{"meno": "Fiktívny Vlastník"}], "lv": "1926"}
    gj = render.geojson([owner_like])
    km = render.kml([owner_like], [SHAPE])
    for text in (gj, km):
        assert "Fiktívny" not in text and "1926" not in text
    assert set(json.loads(gj)["features"][0]["properties"]) == {"oznacenie", "register", "cislo", "ku_kod", "ku_nazov", "vymera_m2"}
    xml.dom.minidom.parseString(km)  # well-formed


def test_html_contains_parcel_and_no_owner():
    f = render.Frame(SHAPE.bounds)
    h = render.html([{**PARCEL, "vlastnici": ["Fiktívny"]}], f, None, "E 503", "orto")
    assert "871541_503.E" in h and "Fiktívny" not in h and "leaflet" in h


def test_lv_url_matches_verified_form():
    from kataster_mcp.lv import lv_url
    # Form verified 2026-10-07 from the portal redirect (kataster-sk.md).
    assert lv_url("871541", "1926", "html", [("E", "503")]) == (
        "https://kataster.skgeodesy.sk/Portal45/api/Bo/GeneratePrfPublic"
        "?prfNumber=1926&cadastralUnitCode=871541&outputType=html&filter=parcelsE:503;")
    assert "filter" not in lv_url("871541", "1926", "pdf", [])


def test_parse_lv_fixture():
    from kataster_mcp.parsers.lv import parse_lv
    d = parse_lv((FIX / "lv_vypis_anon.html").read_text(encoding="utf8"))
    assert d["lv"] == "1926"
    assert d["hlavicka"]["Katastrálne územie"] == "871541 Vyšný Kubín"
    assert len(d["cast_a"]) == 26 and all(p["register"] == "E" for p in d["cast_a"])
    first = d["cast_a"][0]
    assert (first["cislo"], first["vymera_m2"], first["druh_pozemku"]) == ("503", 1286, "Trvalý trávny porast")
    assert first["umiestnenie"].startswith("Pozemok je umiestnený mimo")  # legend applied
    owners = d["cast_b"]["vlastnici"]
    assert [o["podiel"] for o in owners] == ["1/2", "1/2"]
    assert owners[0]["meno"] == "FIKTÍVNY Vlastník" and owners[0]["datum_narodenia"] == "01.01.1900"
    assert owners[0]["titul_nadobudnutia"].startswith("Zmluva č. TEST-1/2000")
    assert owners[0]["poznamka"] == "Bez zápisu" and owners[0]["ine_udaje"] == "Bez zápisu"
    assert {o["typ"] for o in d["cast_b"]["ine_opravnene_osoby"]} == {"Správca", "Nájomca", "Iná oprávnená osoba"}
    assert d["tarchy"] == "none"


def test_parse_lv_rejects_other_pages():
    import pytest
    from kataster_mcp.parsers.lv import parse_lv
    with pytest.raises(ValueError):
        parse_lv("<html><body>Nie som robot</body></html>")


def test_parse_nahlad_fixture_matches_extract():
    from kataster_mcp.parsers.lv import parse_lv
    from kataster_mcp.parsers.nahlad import parse_nahlad
    n = parse_nahlad((FIX / "lv_nahlad_anon.html").read_text(encoding="utf8"))
    v = parse_lv((FIX / "lv_vypis_anon.html").read_text(encoding="utf8"))
    assert n["lv"] == v["lv"] == "1926" and n["zdroj_typ"] == "nahlad_lv"
    assert n["hlavicka"]["Údaje platné k"].startswith("6.10.2026")
    # the two pages describe the same LV: same parcels, areas and shares
    key = lambda rows: [(p["register"], p["cislo"], p["vymera_m2"], p["druh_pozemku"]) for p in rows]
    assert key(n["cast_a"]) == key(v["cast_a"])
    assert n["cast_a"][0]["umiestnenie"].startswith("Pozemok je umiestnený mimo")
    o = n["cast_b"]["vlastnici"]
    assert [x["podiel"] for x in o] == ["1/2", "1/2"]
    assert (o[0]["meno"], o[0]["datum_narodenia"]) == ("FIKTÍVNY Vlastník", "01.01.1900")
    assert o[0]["adresa"].startswith("Testovacia") and o[0]["titul_nadobudnutia"].startswith("Zmluva č. TEST")
    assert n["tarchy"] == "none"
