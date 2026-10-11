"""Search by person done by the user: guides, folder import, overview from imports; portal block and guides
on kataster_vlastnici (offline, fake people from the anonymized fixtures)."""

import copy
import json
import shutil

import httpx
import pytest

from kataster_mcp import navod
from kataster_mcp.lv import IMPORT, LV_VER
from kataster_mcp.policy import PUBLIC_POLICY
from kataster_mcp.server import create_server

from test_core import FIX, FakeTime, _call, _mock_http

OTHER = "VYMYSLENÁ"  # the co-owner on LV 1926: never listed by name in an overview of someone else


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _offline(tmp_path):
    def handler(req):
        raise AssertionError(f"no network expected: {req.url}")
    return create_server(PUBLIC_POLICY, data_dir=tmp_path / "data", http=_mock_http(PUBLIC_POLICY, handler, FakeTime()))


async def _err(s, name, args):
    with pytest.raises(Exception) as e:
        await _call(s, name, args)
    text = str(e.value)
    return json.loads(text[text.index("{"):])["chyba"]


@pytest.mark.anyio
async def test_navod_natural_and_legal_person(tmp_path):
    s = _offline(tmp_path)
    fo = (await _call(s, "kataster_osoba_navod", {"ku": "Vyšný Kubín", "priezvisko": "Vlastník", "meno": "Fiktívny"}))["data"]
    text = " ".join(fo["navod"])
    assert navod.SEARCH_URL in text and "Fyzické osoby" in text and "871541" in text and "Fiktívny" in text
    assert "kataster_lv_import" in text and "Webová stránka, kompletná" in text
    po = (await _call(s, "kataster_osoba_navod", {"ku": "871541", "typ": "pravnicka", "ico": "12345678"}))["data"]
    assert "Právnické osoby" in " ".join(po["navod"]) and "IČO 12345678" in " ".join(po["navod"])


@pytest.mark.anyio
async def test_import_folder_merges_and_reports(tmp_path):
    d = tmp_path / "lv"
    d.mkdir()
    shutil.copy(FIX / "lv_nahlad_anon.html", d / "nahlad.html")
    shutil.copy(FIX / "lv_vypis_anon.html", d / "vypis.html")
    (d / "ine.html").write_text("<html><body>nič</body></html>", encoding="utf-8")
    (d / "nahlad_files").mkdir()  # what 'web page, complete' adds: not descended into
    (d / "nahlad_files" / "x.html").write_text("<html></html>", encoding="utf-8")
    s = _offline(tmp_path)
    out = await _call(s, "kataster_lv_import", {"path": str(d)})
    data = out["data"]
    assert data["pocet_lv"] == 1 and data["pocet_suborov"] == 3
    lv = data["lv"][0]
    assert lv["ku_kod"] == "871541" and lv["lv"] == "1926" and len(lv["subory"]) == 2
    assert lv["zdroj_typ"] == "vypis_lv_html"  # the same data date: the extract wins over the preview
    assert any("ine.html" in w for w in out["varovania"])
    rows = s.app_state.osoby.db.execute("SELECT kluc FROM cache WHERE zdroj=?", (IMPORT,)).fetchall()
    assert rows == [("871541|1926",)]  # the preview header has only the name; it is keyed by code too
    err = await _err(s, "kataster_lv_import", {"paths": [str(d / "ine.html"), str(d / "nahlad_files" / "x.html")]})
    assert err["typ"] == "invalid_input"


@pytest.mark.anyio
async def test_single_file_import_unchanged(tmp_path):
    s = _offline(tmp_path)
    out = (await _call(s, "kataster_lv_import", {"path": str(FIX / "lv_nahlad_anon.html")}))["data"]
    assert out["zdroj_typ"] == "nahlad_lv" and len(out["cast_a"]) == 26


def _put_lv(s, ku_lv, data):
    s.app_state.osoby.put(IMPORT, LV_VER, ku_lv, {"zdroj": "test", "subory": ["test"], "lv": data})


@pytest.mark.anyio
async def test_overview_shares_namesakes_and_privacy(tmp_path):
    s = _offline(tmp_path)
    await _call(s, "kataster_lv_import", {"path": str(FIX / "lv_vypis_anon.html")})
    out = await _call(s, "kataster_osoba_prehlad", {"priezvisko": "Fiktívny", "meno": "vlastník"})
    data = out["data"]
    assert len(data["osoby"]) == 1
    p = data["osoby"][0]
    e = p["lv"][0]
    assert p["datum_narodenia"] == "01.01.1900" and e["lv"] == "1926" and e["podiel"] == "1/2"
    assert len(e["parcely"]) == 26 and e["ostatni_spoluvlastnici"] == 1
    assert e["vymera_podielom_m2"] == pytest.approx(e["vymera_lv_m2"] / 2, abs=0.1)
    assert OTHER not in json.dumps(data, ensure_ascii=False)
    assert any("súpis" in w for w in out["varovania"]) and data["importy"]["platne_do"]

    # a namesake on another LV (fake), then one without a date of birth
    base = s.app_state.osoby.get(IMPORT, LV_VER, "871541|1926", None)[0]["lv"]
    twin = copy.deepcopy(base)
    twin["lv"] = "2000"
    twin["cast_b"]["vlastnici"] = [{**base["cast_b"]["vlastnici"][0], "datum_narodenia": "05.05.1950", "podiel": "1/1"}]
    _put_lv(s, "871541|2000", twin)
    nodate = copy.deepcopy(twin)
    nodate["lv"] = "2001"
    nodate["cast_b"]["vlastnici"][0]["datum_narodenia"] = None
    _put_lv(s, "871541|2001", nodate)

    out = await _call(s, "kataster_osoba_prehlad", {"priezvisko": "Fiktívny", "meno": "Vlastník"})
    assert len(out["data"]["osoby"]) == 3 and any("namesakes" in w for w in out["varovania"])
    out = (await _call(s, "kataster_osoba_prehlad", {"priezvisko": "Fiktívny", "meno": "Vlastník",
                                                     "datum_narodenia": "5.5.1950"}))["data"]
    assert [x["lv"] for x in out["osoby"][0]["lv"]] == ["2000"] and len(out["osoby"]) == 1
    assert [x["lv"] for x in out["nejednoznacne"]] == ["2001"]
    other_ku = (await _call(s, "kataster_osoba_prehlad", {"priezvisko": "Fiktívny", "meno": "Vlastník", "ku": "812315"}))["data"]
    assert other_ku["osoby"] == [] and other_ku["importy"]["pocet_lv"] == 0


@pytest.mark.anyio
async def test_overview_legal_person_by_ico(tmp_path):
    s = _offline(tmp_path)
    await _call(s, "kataster_lv_import", {"path": str(FIX / "lv_vypis_anon.html")})
    base = s.app_state.osoby.get(IMPORT, LV_VER, "871541|1926", None)[0]["lv"]
    firm = copy.deepcopy(base)
    firm["lv"] = "3000"
    firm["cast_b"]["vlastnici"] = [{"poradie": 1, "meno": "Skúšobná firma s.r.o.", "ico": "12 345 678",
                                    "datum_narodenia": None, "adresa": "Nikde", "podiel": "1/1"}]
    _put_lv(s, "871541|3000", firm)
    out = (await _call(s, "kataster_osoba_prehlad", {"ico": "12345678"}))["data"]
    assert len(out["osoby"]) == 1 and out["osoby"][0]["lv"][0]["lv"] == "3000"


@pytest.mark.anyio
async def test_overview_needs_a_name(tmp_path):
    s = _offline(tmp_path)
    assert (await _err(s, "kataster_osoba_prehlad", {"priezvisko": "Vlastník"}))["typ"] == "invalid_input"


@pytest.mark.anyio
async def test_lv_has_guide(tmp_path):
    s = _offline(tmp_path)
    out = (await _call(s, "kataster_lv", {"ku": "871541", "lv": "1926"}))["data"]
    text = " ".join(out["navod"])
    assert out["url"] in text and "slovensko.sk" in text and "Náhľad LV" in text and "Nie som robot" in text


def _portal(tmp_path, detail_page, calls):
    e503 = json.loads((FIX / "wfs_e503.json").read_text(encoding="utf-8"))
    ident = json.loads((FIX / "eskn_identify_e503.json").read_text(encoding="utf-8"))

    def handler(req):
        calls.append(str(req.url.path))
        if req.url.host == "inspirews.skgeodesy.sk":
            ok = "cp_uo" in req.url.path and "_503.E" in req.url.params.get("CQL_FILTER", "")
            return httpx.Response(200, json=e503 if ok else {"type": "FeatureCollection", "features": []})
        if req.url.path.endswith("/identify"):
            return httpx.Response(200, json=ident)
        return httpx.Response(200, text=detail_page, headers={"content-type": "text/html"})

    return create_server(PUBLIC_POLICY, data_dir=tmp_path / "data", http=_mock_http(PUBLIC_POLICY, handler, FakeTime()))


@pytest.mark.anyio
async def test_block_gives_guide_and_24h_pause(tmp_path):
    calls = []
    s = _portal(tmp_path, "<html>Request Rejected</html>", calls)
    err = await _err(s, "kataster_vlastnici", {"ku": "871541", "cislo": "E 503"})
    assert err["typ"] == "blocked" and navod.SEARCH_URL in " ".join(err["navod"]) and "registra E" in " ".join(err["navod"])
    n, used = len(calls), s.app_state.quota.used()
    err = await _err(s, "kataster_vlastnici", {"ku": "871541", "cislo": "E 503"})
    assert err["typ"] == "blocked" and "skusi_znova" in err["detail"] and err["navod"]
    assert all("skgeodesy.sk/eskn" not in c for c in calls[n:]) and s.app_state.quota.used() == used
    # the block survives a restart (kept in cache.sqlite), other hosts are not blocked
    s2 = _portal(tmp_path, "<html>ok</html>", calls)
    assert s2.app_state.http.blocked_until() is not None
    assert s2.app_state.http.blocked_until("inspirews.skgeodesy.sk") is None


@pytest.mark.anyio
async def test_quota_gives_guide_and_reset_time(tmp_path):
    calls = []
    s = _portal(tmp_path, (FIX / "eskn_detail_e_anon.html").read_text(encoding="utf-8"), calls)
    for i in range(PUBLIC_POLICY.owners_daily_hard):
        s.app_state.quota.record(f"x{i}")
    err = await _err(s, "kataster_vlastnici", {"ku": "871541", "cislo": "E 503"})
    assert err["typ"] == "limit_reached" and err["navod"] and "T00:00:00" in err["detail"]["kvota_obnovena"]


@pytest.mark.anyio
async def test_buildings_from_preview_import_and_overview(tmp_path):
    d = tmp_path / "lv"
    d.mkdir()
    shutil.copy(FIX / "lv_nahlad_anon.html", d / "parcely.html")
    shutil.copy(FIX / "lv_nahlad_stavba_anon.html", d / "dom.html")
    s = _offline(tmp_path)
    one = (await _call(s, "kataster_lv_import", {"path": str(d / "dom.html")}))["data"]
    assert one["cast_a"] == [] and len(one["stavby"]) == 1
    b = one["stavby"][0]
    assert b["supisne_cislo"].isdigit() and b["druh_stavby"] == "Rodinný dom"  # code 10 translated by the legend
    assert b["umiestnenie_stavby"].startswith("Stavba postavená") and b["lv_pozemku"].isdigit()
    assert b["parcela"] in b["pravny_vztah_k_pozemku"]
    summary = (await _call(s, "kataster_lv_import", {"path": str(d)}))["data"]["lv"]
    assert sorted((x["parcely"], x["stavby"]) for x in summary) == [(0, 1), (26, 0)]
    p = (await _call(s, "kataster_osoba_prehlad", {"priezvisko": "Fiktívny", "meno": "Vlastník"}))["data"]["osoby"][0]
    assert p["pocet_lv"] == 2 and p["pocet_stavieb"] == 1 and p["pocet_parciel"] == 26
    house = next(e for e in p["lv"] if e["stavby"])
    assert house["parcely"] == [] and house["vymera_lv_m2"] is None and house["vymera_podielom_m2"] is None
    assert house["stavby"][0]["lv_pozemku"] == b["lv_pozemku"]
    assert house["bsm"] and house["podiel"] == "1/1" and house["ostatni_spoluvlastnici"] == 1  # spouses, both listed
    assert OTHER not in json.dumps(p, ensure_ascii=False)


def test_buildings_in_extract_rows():
    """The buildings table of an extract (format not seen on a real extract yet): shape as in the preview."""
    from kataster_mcp.parsers.lv import parse_rows

    base = [["VÝPIS Z LISTU VLASTNÍCTVA č. 7"], ["Katastrálne územie", ":", "871541 Vyšný Kubín"], ["ČASŤ A"]]
    rows = base + [
        ["Parcely registra \"C\""], ["Parcelné číslo", "Výmera v m²", "Druh pozemku"], ["12/3", "100", "16"],
        ["Stavby"], ["Súpisné číslo", "Na pozemku parcelné číslo", "Druh stavby", "Popis stavby"],
        ["45", "12/3", "10", "Rodinný dom"],
        ["", "Právny vzťah k pozemku parcelné číslo 12/3 pod stavbou s.č. 45 je evidovaný na liste vlastníctva č. 1 234."],
        ["Legenda"], ["Druh stavby"], ["10", "Rodinný dom"], ["ČASŤ B"], ["ČASŤ C"],
    ]
    out = parse_rows(rows)
    assert [p["cislo"] for p in out["cast_a"]] == ["12/3"]  # the building row is not taken for a parcel
    assert out["stavby"] == [{"supisne_cislo": "45", "parcela": "12/3", "druh_stavby": "Rodinný dom",
                              "popis_stavby": "Rodinný dom", "lv_pozemku": "1234",
                              "pravny_vztah_k_pozemku": rows[-6][1]}]
    odd = parse_rows(base + [["Stavby"], ["Súpisné č.", "na parc. č."], ["45 12/3"], ["ČASŤ B"]])
    assert odd["stavby"] == [] and odd["stavby_neprecitane"]
