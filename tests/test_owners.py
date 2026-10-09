import json
import stat
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from kataster_mcp.cache import POSIX
from mcp.server.fastmcp.exceptions import ToolError

from kataster_mcp.policy import PUBLIC_POLICY
from kataster_mcp.server import create_server
from kataster_mcp.sources import eskn

from test_core import FakeTime, _call, _mock_http

FIX = Path(__file__).parent / "fixtures"
E503 = json.loads((FIX / "wfs_e503.json").read_text(encoding="utf-8"))
IDENT = json.loads((FIX / "eskn_identify_e503.json").read_text(encoding="utf-8"))
DETAIL = (FIX / "eskn_detail_e_anon.html").read_text(encoding="utf8")
EMPTY = {"type": "FeatureCollection", "features": []}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def handler_factory(calls, detail=DETAIL):
    def handler(request: httpx.Request):
        calls.append(request.url.host + request.url.path)
        if request.url.host == "inspirews.skgeodesy.sk":
            cql = request.url.params.get("CQL_FILTER", "")
            return httpx.Response(200, json=E503 if ("cp_uo" in request.url.path and "_503.E" in cql) else EMPTY)
        if request.url.path.endswith("/identify"):
            return httpx.Response(200, json=IDENT)
        if "detail_p" in request.url.path:
            return httpx.Response(200, text=detail, headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(404)
    return handler


def server(tmp_path, calls, policy=PUBLIC_POLICY, detail=DETAIL):
    return create_server(policy, data_dir=tmp_path,
                         http=_mock_http(policy, handler_factory(calls, detail), FakeTime()))


def eskn_calls(calls):
    return [c for c in calls if c.startswith("kataster.skgeodesy.sk")]


def test_parse_detail_fixture():
    d = eskn.parse_detail(DETAIL)
    assert d["lv"] == "1926" and d["vymera_m2"] == 1286
    assert d["druh_pozemku"] == "Trvalý trávny porast"
    assert [o["podiel"] for o in d["vlastnici"]] == ["1/2", "1/2"]
    o = d["vlastnici"][0]
    assert (o["meno"], o["datum_narodenia"]) == ("Fiktívny Vlastník", "01.01.1900")
    assert o["adresa"].startswith("Testovacia 1/1")
    assert d["stav_k"] == "06.10.2026"


def test_parse_identify_fixture():
    i = eskn.parse_identify(IDENT, "E", "503")
    assert i["id"] == "185206301"
    assert i["uzemie"] == {"kraj": "Žilinský kraj", "okres": "Dolný Kubín", "obec": "Vyšný Kubín",
                           "obec_kod": "510181", "ku_kod": "871541", "ku_nazov": "Vyšný Kubín"}


@pytest.mark.anyio
async def test_owners_counts_once_and_cache_does_not_count(tmp_path):
    calls = []
    s = server(tmp_path, calls)
    out = await _call(s, "kataster_vlastnici", {"ku": "871541", "cislo": "E 503"})
    d = out["data"]
    assert d["lv"] == "1926" and len(d["vlastnici"]) == 2
    assert d["uzemie"]["okres"] == "Dolný Kubín"
    assert d["limit"]["pouzite"] == 1 and d["limit"]["zostava"] == 49
    assert len(eskn_calls(calls)) == 2  # identify + detail
    assert any("unofficial" in w for w in out["varovania"])
    out2 = await _call(s, "kataster_vlastnici", {"ku": "871541", "cislo": "E 503"})
    assert len(eskn_calls(calls)) == 2 and out2["data"]["limit"]["pouzite"] == 1
    assert out2["zdroje"][-1]["z_cache"] is True
    # forced refresh queries the portal again but the parcel is still counted once today
    await _call(s, "kataster_vlastnici", {"ku": "871541", "cislo": "E 503", "obnovit": True})
    assert s.app_state.quota.used() == 1
    assert len(eskn_calls(calls)) == 3  # identify cached, detail again


@pytest.mark.anyio
async def test_owners_db_is_private(tmp_path):
    s = server(tmp_path, [])
    await _call(s, "kataster_vlastnici", {"ku": "871541", "cislo": "E 503"})
    assert not POSIX or stat.S_IMODE((tmp_path / "osoby.sqlite").stat().st_mode) == 0o600
    # owners never land in the public cache
    marker = "Fiktívny".encode()
    assert marker in (tmp_path / "osoby.sqlite").read_bytes()  # positive control
    assert marker not in (tmp_path / "cache.sqlite").read_bytes()


@pytest.mark.anyio
async def test_hard_limit_blocks_before_portal(tmp_path):
    calls = []
    s = server(tmp_path, calls)
    for i in range(50):
        s.app_state.quota.record(f"x|{i}")
    with pytest.raises(ToolError, match="limit_reached"):
        await s.call_tool("kataster_vlastnici", {"ku": "871541", "cislo": "E 503"})
    assert eskn_calls(calls) == []


@pytest.mark.anyio
async def test_soft_limit_needs_confirmation(tmp_path):
    calls = []
    policy = replace(PUBLIC_POLICY, owners_daily_soft=2, owners_daily_hard=5)
    s = server(tmp_path, calls, policy)
    s.app_state.quota.record("a")
    s.app_state.quota.record("b")
    with pytest.raises(ToolError, match="potvrdenie_potrebne"):
        await s.call_tool("kataster_vlastnici", {"ku": "871541", "cislo": "E 503"})
    assert eskn_calls(calls) == []
    out = await _call(s, "kataster_vlastnici", {"ku": "871541", "cislo": "E 503", "potvrdene": True})
    assert out["data"]["limit"]["pouzite"] == 3


@pytest.mark.anyio
async def test_failed_attempt_counts_and_snapshot_is_private(tmp_path):
    calls = []
    s = server(tmp_path, calls, detail="<html><body>Nieco ine</body></html>")
    with pytest.raises(ToolError, match="parser_drift"):
        await s.call_tool("kataster_vlastnici", {"ku": "871541", "cislo": "E 503"})
    assert s.app_state.quota.used() == 1
    snaps = list((tmp_path / "snapshots").iterdir())
    assert len(snaps) == 1 and (not POSIX or stat.S_IMODE(snaps[0].stat().st_mode) == 0o600)
