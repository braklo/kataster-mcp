"""Building detail (detail_st) behind kataster_vlastnici(detail_stavieb=true), offline."""
import json
import re
from pathlib import Path

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_core import FakeTime, _call
from test_owners import E503, EMPTY, IDENT

from kataster_mcp.http import Http
from kataster_mcp.policy import PUBLIC_POLICY
from kataster_mcp.server import create_server
from kataster_mcp.sources import eskn_building

FIX = Path(__file__).parent / "fixtures"
DETAIL_C = (FIX / "eskn_detail_c_anon.html").read_text(encoding="utf8")
BUILDING = (FIX / "eskn_detail_st_anon.html").read_text(encoding="utf8")
FORM = ('<form id="bodyForm" name="bodyForm" method="post" action="/eskn-portal/search/parcels/detail_pc?p1=x">'
        '<input type="hidden" name="bodyForm" value="bodyForm" />{}'
        '<input type="hidden" name="javax.faces.ViewState" id="j_id1:javax.faces.ViewState:0" value="vs-1" /></form>')
PARCEL_PAGE = FORM.format(DETAIL_C)
ST_URL = "https://kataster.skgeodesy.sk/eskn-portal/search/parcels/detail_st?p1=y"

# a block of flats: owner rows without shares (markup of a real page 2026-11, names fictitious)
UNIT_ROW = ('<li><span class="inline-block vertical-center" style="max-width: 100%"> <span class="inline-block m-bottom-xs">'
            '{n}. <a id="j_idt173:{n}:j_idt186" href="#" class="ui-commandlink ui-widget f-bold inline-block ucastnik-link" '
            'onclick="return false;">Fiktívny Vlastník, 01.01.1900, Testovacia 1/1, Nikde, 000 00</a> <strong> </strong>'
            '</span></span></li>')


def flats_page() -> str:
    start = BUILDING.index("<li>", BUILDING.index("Vlastník"))
    end = BUILDING.index("</ul>", start)
    rows = "".join(UNIT_ROW.format(n=n) for n in range(1, 11))
    return (BUILDING[:start] + rows + BUILDING[end:] + '<span class="ui-button-text ui-c">Zobraziť viac</span>') \
        .replace("Rodinný dom", "Bytový dom")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def server(tmp_path, calls, parcel_page=PARCEL_PAGE, building=BUILDING, redirect_to=ST_URL):
    posts = []

    def handler(request: httpx.Request):
        calls.append(request.method + " " + request.url.path)
        if request.url.host == "inspirews.skgeodesy.sk":
            cql = request.url.params.get("CQL_FILTER", "")
            return httpx.Response(200, json=E503 if ("cp_uo" in request.url.path and "_503.E" in cql) else EMPTY)
        if request.url.path.endswith("/identify"):
            return httpx.Response(200, json=IDENT)
        if "detail_p" in request.url.path and request.method == "GET":
            return httpx.Response(200, text=parcel_page, headers={"content-type": "text/html; charset=utf-8"})
        if request.method == "POST":
            posts.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(302, headers={"location": redirect_to})
        if "detail_st" in request.url.path:
            return httpx.Response(200, text=building, headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(200, text="<html>Chyba</html>")

    ft = FakeTime()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    s = create_server(PUBLIC_POLICY, data_dir=tmp_path, limits_dir=tmp_path / "limity", http=Http(PUBLIC_POLICY, client=client, sleep=ft.sleep, clock=ft.clock))
    s.posts = posts
    return s


def test_parse_building_fixture():
    d = eskn_building.parse_building(BUILDING)
    assert (d["lv"], d["druh_stavby"], d["parcely_c"], d["stav_k"]) == ("1683", "Rodinný dom", ["660/73"], "13.11.2026")
    assert [(o["poradie"], o["meno"], o["podiel"], o.get("bsm")) for o in d["vlastnici"]] == [
        (1, "Fiktívny Vlastník", "1/1", True), (1, "Vymyslená Osoba r. Skúšobná", "1/1", True)]
    assert d["jednotky"] is False


def test_parse_building_block_of_flats_returns_no_unit_owners():
    d = eskn_building.parse_building(flats_page())
    assert d["vlastnici"] is None and d["jednotky"] is True
    assert d["riadky_vlastnikov_jednotiek"] == 10 and d["dalsie_riadky"] is True
    assert "Fiktívny" not in json.dumps(d, ensure_ascii=False)  # unit owners never leave the parser


def test_parse_building_drift():
    with pytest.raises(ValueError, match="not a building"):
        eskn_building.parse_building(DETAIL_C)
    start = BUILDING.index("Vlastník")
    extra = BUILDING[:start] + BUILDING[start:].replace("</li>", "</li>" + UNIT_ROW.format(n=2), 1)
    with pytest.raises(ValueError, match="2 owner rows, 1 shares"):  # a row without a share next to one with it
        eskn_building.parse_building(extra)


def test_postback_form_from_parcel_detail():
    link = eskn_building.building_link(PARCEL_PAGE, 0, "1")
    action, fields = eskn_building.postback(PARCEL_PAGE, link)
    assert action.startswith("/eskn-portal/search/parcels/detail_pc")
    assert fields[link] == link and fields["javax.faces.ViewState"] == "vs-1"
    with pytest.raises(ValueError, match="house number"):
        eskn_building.building_link(PARCEL_PAGE, 0, "7")
    with pytest.raises(ValueError, match="not found"):
        eskn_building.building_link(PARCEL_PAGE, 1, None)
    with pytest.raises(ValueError, match="bodyForm"):
        eskn_building.postback(DETAIL_C, link)


@pytest.mark.anyio
async def test_off_by_default_no_building_query(tmp_path):
    calls = []
    s = server(tmp_path, calls)
    out = await _call(s, "kataster_vlastnici", {"ku": "871541", "cislo": "E 503"})
    assert "stavby_detail" not in out["data"] and not any(c.startswith("POST") for c in calls)
    assert s.app_state.quota.used() == 1


@pytest.mark.anyio
async def test_building_detail_counts_one_query_and_is_cached(tmp_path):
    calls = []
    s = server(tmp_path, calls)
    out = await _call(s, "kataster_vlastnici", {"ku": "871541", "cislo": "E 503", "detail_stavieb": True})
    d = out["data"]
    assert d["stavby_dotazy"] == 1 and d["limit"]["pouzite"] == 2  # parcel + building
    b = d["stavby_detail"][0]
    assert (b["supisne_cislo"], b["lv"], b["parcely_c"]) == ("1", "1683", ["660/73"]) and len(b["vlastnici"]) == 2
    assert any("LV 1683" in w for w in out["varovania"])
    assert [c.split(" ")[0] for c in calls if "detail" in c] == ["GET", "POST", "GET"]  # parcel page reused
    assert s.posts[0]["j_idt212:0:j_idt215"] == "j_idt212:0:j_idt215"
    assert out["zdroje"][-1]["id"] == eskn_building.SOURCE

    again = await _call(s, "kataster_vlastnici", {"ku": "871541", "cislo": "E 503", "detail_stavieb": True})
    assert again["data"]["stavby_dotazy"] == 0 and again["data"]["stavby_detail"] == d["stavby_detail"]
    assert len([c for c in calls if "detail" in c]) == 3 and s.app_state.quota.used() == 2


@pytest.mark.anyio
async def test_building_from_cached_parcel_fetches_parcel_page_again(tmp_path):
    calls = []
    s = server(tmp_path, calls)
    await _call(s, "kataster_vlastnici", {"ku": "871541", "cislo": "E 503"})
    out = await _call(s, "kataster_vlastnici", {"ku": "871541", "cislo": "E 503", "detail_stavieb": True})
    assert out["data"]["stavby_dotazy"] == 1 and s.app_state.quota.used() == 2
    assert [c.split(" ")[0] for c in calls if "detail" in c] == ["GET", "GET", "POST", "GET"]


@pytest.mark.anyio
async def test_block_of_flats_warns_and_keeps_no_unit_owners(tmp_path):
    calls = []
    s = server(tmp_path, calls, building=flats_page())
    out = await _call(s, "kataster_vlastnici", {"ku": "871541", "cislo": "E 503", "detail_stavieb": True})
    b = out["data"]["stavby_detail"][0]
    assert b["vlastnici"] is None and b["jednotky"] is True
    assert any("divided into units" in w for w in out["varovania"])
    steps = out["data"]["navod"]
    assert any("Súpisné číslo" in x or "súpisné číslo 1" in x for x in steps)
    assert any("LV stavby (1683)" in x for x in steps) and "kataster_lv_import" in steps[-1]


@pytest.mark.anyio
async def test_family_house_has_no_flats_guide(tmp_path):
    out = await _call(server(tmp_path, []), "kataster_vlastnici", {"ku": "871541", "cislo": "E 503", "detail_stavieb": True})
    assert "navod" not in out["data"]


@pytest.mark.anyio
async def test_two_buildings_warn_and_count_two(tmp_path):
    calls = []
    two = DETAIL_C.replace("Súpisné číslo: 1</a></li>",
                           'Súpisné číslo: 1</a></li><li><a id="j_idt212:1:j_idt215" href="#" '
                           'class="ui-commandlink ui-widget inline">Súpisné číslo: 2</a></li>', 1)
    s = server(tmp_path, calls, parcel_page=FORM.format(two))
    out = await _call(s, "kataster_vlastnici", {"ku": "871541", "cislo": "E 503", "detail_stavieb": True})
    assert out["data"]["stavby_dotazy"] == 2 and s.app_state.quota.used() == 3
    assert any("2 buildings" in w for w in out["varovania"])
    assert [p_ for p in s.posts for p_ in p if p_.startswith("j_idt212")] == ["j_idt212:0:j_idt215", "j_idt212:1:j_idt215"]


@pytest.mark.anyio
async def test_building_page_change_is_drift_with_snapshot(tmp_path):
    calls = []
    s = server(tmp_path, calls, building="<html>iná stránka</html>")
    with pytest.raises(ToolError, match="parser_drift"):
        await s.call_tool("kataster_vlastnici", {"ku": "871541", "cislo": "E 503", "detail_stavieb": True})
    assert any("detail_st" in p.name for p in (tmp_path / "snapshots").iterdir())

    s2 = server(tmp_path / "b", calls, redirect_to="https://kataster.skgeodesy.sk/eskn-portal/error")
    with pytest.raises(ToolError, match="did not open the building"):
        await s2.call_tool("kataster_vlastnici", {"ku": "871541", "cislo": "E 503", "detail_stavieb": True})


@pytest.mark.anyio
async def test_building_captcha_gives_navod(tmp_path):
    calls = []
    s = server(tmp_path, calls, building='<html><script src="https://www.google.com/recaptcha/api.js"></script></html>')
    with pytest.raises(ToolError) as e:
        await s.call_tool("kataster_vlastnici", {"ku": "871541", "cislo": "E 503", "detail_stavieb": True})
    assert "captcha_required" in str(e.value) and re.search(r"navod", str(e.value))
