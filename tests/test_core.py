import json
import stat
from pathlib import Path

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from kataster_mcp import ku
from kataster_mcp.cache import POSIX, Cache, Quota
from kataster_mcp.envelope import KatasterError
from kataster_mcp.http import Http, check_response
from kataster_mcp.normalize import fold, parcel_number
from kataster_mcp.policy import PUBLIC_POLICY, Policy
from kataster_mcp.server import create_server

FIX = Path(__file__).parent / "fixtures"


# --- normalisation -------------------------------------------------------

@pytest.mark.parametrize("text,reg,expected", [
    ("503", "auto", ("auto", "503")),
    ("503 / 1", "auto", ("auto", "503/1")),
    ("E 503", "auto", ("E", "503")),
    ("C-KN 660/66", "auto", ("C", "660/66")),
    ("parc. č. E 599/3", "auto", ("E", "599/3")),
    ("660/66", "C", ("C", "660/66")),
])
def test_parcel_number(text, reg, expected):
    assert parcel_number(text, reg) == expected


@pytest.mark.parametrize("text,reg", [("abc", "auto"), ("E 503", "C"), ("503-1", "auto")])
def test_parcel_number_invalid(text, reg):
    with pytest.raises(KatasterError) as e:
        parcel_number(text, reg)
    assert e.value.kind == "invalid_input"


def test_fold():
    assert fold("Vyšný  Kubín") == "vysny kubin"


# --- k.ú. table ----------------------------------------------------------

def test_ku_table_complete():
    t = ku.table()
    assert t["pocet"] == len(t["ku"]) > 3500


def test_ku_search_diacritics_and_code():
    assert [r["kod"] for r in ku.search("vysny kubin")] == ["871541"]
    assert ku.search("871541")[0]["nazov"] == "Vyšný Kubín"


def test_ku_rows_have_municipality_and_district():
    r = ku.search("871541")[0]
    assert (r["obec"], r["obec_kod"], r["okres"]) == ("Vyšný Kubín", "510181", "Dolný Kubín")
    assert all(x["okres"] for x in ku.table()["_rows"])


def test_ku_resolve_ambiguous():
    with pytest.raises(KatasterError) as e:
        ku.resolve("Kubín")
    assert e.value.kind == "ambiguous"
    assert {r["kod"] for r in e.value.details} >= {"812315", "871541"}


# --- pacing ----------------------------------------------------------------

class FakeTime:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def clock(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(s)
        self.t += s


def _mock_http(policy, handler, ft):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Http(policy, client=client, sleep=ft.sleep, clock=ft.clock)


@pytest.mark.anyio
async def test_eskn_pause_at_least_3s():
    ft = FakeTime()
    http = _mock_http(PUBLIC_POLICY, lambda r: httpx.Response(200, json={}), ft)
    for _ in range(3):
        await http.get("https://kataster.skgeodesy.sk/x")
    assert len(ft.slept) == 2
    assert all(3.0 <= s <= 4.0 for s in ft.slept)


@pytest.mark.anyio
async def test_other_host_pause():
    ft = FakeTime()
    http = _mock_http(PUBLIC_POLICY, lambda r: httpx.Response(200, json={}), ft)
    await http.get("https://inspirews.skgeodesy.sk/a")
    await http.get("https://inspirews.skgeodesy.sk/b")
    assert ft.slept == [1.0]


def test_public_policy_values():
    assert PUBLIC_POLICY.eskn_pause_s >= 3.0
    assert PUBLIC_POLICY.owners_daily_hard == 50
    assert PUBLIC_POLICY.owners_daily_soft is None


@pytest.mark.parametrize("body,status,kind", [
    ("<html>Request Rejected</html>", 200, "blocked"),
    ('<div class="g-recaptcha"></div>', 200, "captcha_required"),
    ("x", 403, "blocked"),
    ("x", 503, "unavailable"),
])
def test_check_response(body, status, kind):
    resp = httpx.Response(status, text=body, headers={"content-type": "text/html"},
                          request=httpx.Request("GET", "https://kataster.skgeodesy.sk/"))
    with pytest.raises(KatasterError) as e:
        check_response(resp)
    assert e.value.kind == kind


# --- cache and quota -------------------------------------------------------

def test_private_cache_permissions(tmp_path):
    c = Cache(tmp_path / "d" / "osoby.sqlite", private=True)
    assert not POSIX or stat.S_IMODE((tmp_path / "d" / "osoby.sqlite").stat().st_mode) == 0o600
    assert not POSIX or stat.S_IMODE((tmp_path / "d").stat().st_mode) == 0o700
    c.put("s", "v", "k", {"a": 1})
    assert c.get("s", "v", "k", None)[0] == {"a": 1}
    assert c.get("s", "other-version", "k", None) is None


def test_quota_counts_parcel_once(tmp_path):
    q = Quota(Cache(tmp_path / "o.sqlite", private=True).db)
    q.record("871541|E|503", "2026-10-07")
    q.record("871541|E|503", "2026-10-07")
    q.record("871541|E|599/3", "2026-10-07")
    assert q.used("2026-10-07") == 2
    assert q.used("2026-10-08") == 0


# --- tools -----------------------------------------------------------------

def _server(tmp_path, handler):
    ft = FakeTime()
    return create_server(PUBLIC_POLICY, data_dir=tmp_path, http=_mock_http(PUBLIC_POLICY, handler, ft))


def _wfs_handler(calls):
    e503 = json.loads((FIX / "wfs_e503.json").read_text(encoding="utf-8"))
    empty = {"type": "FeatureCollection", "features": []}

    def handler(request: httpx.Request):
        calls.append(request)
        cql = request.url.params.get("CQL_FILTER", "")
        if "cp_uo" in request.url.path and "871541_503.E" in cql:
            return httpx.Response(200, json=e503)
        return httpx.Response(200, json=empty)
    return handler


async def _call(server, name, args):
    res = await server.call_tool(name, args)
    # FastMCP returns (content, structured) for dict results
    structured = res[1] if isinstance(res, tuple) else None
    return structured.get("result", structured) if structured else json.loads(res[0].text)


@pytest.mark.anyio
async def test_parcela_auto_finds_e_and_caches(tmp_path):
    calls = []
    s = _server(tmp_path, _wfs_handler(calls))
    out = await _call(s, "kataster_parcela", {"ku": "Vyšný Kubín", "cislo": "503"})
    d = out["data"]
    assert (d["register"], d["cislo"], d["ku_kod"], d["vymera_m2"]) == ("E", "503", "871541", 1286)
    assert d["ku_nazov"] == "Vyšný Kubín" and "geometria" not in d
    assert 49 < d["referencny_bod"]["lat"] < 50 and 19 < d["referencny_bod"]["lon"] < 20
    assert len(calls) == 2  # C and E
    out2 = await _call(s, "kataster_parcela", {"ku": "871541", "cislo": "E 503", "geometria": True})
    assert len(calls) == 2  # served from cache
    assert out2["zdroje"][0]["z_cache"] is True and out2["data"]["geometria"]["type"] == "Polygon"


@pytest.mark.anyio
async def test_parcela_not_found_has_coverage(tmp_path):
    s = _server(tmp_path, _wfs_handler([]))
    with pytest.raises(ToolError) as e:
        await s.call_tool("kataster_parcela", {"ku": "871541", "cislo": "99999"})
    body = json.loads(str(e.value).split(": ", 1)[-1]) if not str(e.value).startswith("{") else json.loads(str(e.value))
    assert body["chyba"]["typ"] == "not_found" and "coverage" in body["chyba"]


@pytest.mark.anyio
async def test_parcela_rejects_swapped_point(tmp_path):
    s = _server(tmp_path, _wfs_handler([]))
    with pytest.raises(ToolError, match="outside Slovakia"):
        await s.call_tool("kataster_parcela", {"lat": 19.3, "lon": 49.17})


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_parcela_auto_ambiguous_when_in_c_and_e(tmp_path):
    # Real case: 871541 has both C 503 (28 m²) and E 503 (1 286 m²).
    e503 = json.loads((FIX / "wfs_e503.json").read_text(encoding="utf-8"))
    c503 = json.loads(json.dumps(e503))
    p = c503["features"][0]["properties"]
    p["nationalCadastralReference"] = "871541_503.C"
    p["areaValue"]["value"] = 28

    def handler(request):
        return httpx.Response(200, json=e503 if "cp_uo" in request.url.path else c503)

    s = _server(tmp_path, handler)
    with pytest.raises(ToolError, match='"ambiguous"'):
        await s.call_tool("kataster_parcela", {"ku": "871541", "cislo": "503"})
    out = await _call(s, "kataster_parcela", {"ku": "871541", "cislo": "503", "register": "C"})
    assert out["data"]["vymera_m2"] == 28
