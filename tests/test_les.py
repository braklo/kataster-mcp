import json
from pathlib import Path

import httpx
import pytest

from kataster_mcp import geo
from kataster_mcp.policy import PUBLIC_POLICY
from kataster_mcp.server import create_server
from kataster_mcp.sources import layers as L

from test_core import FakeTime, _call, _mock_http

FIX = Path(__file__).parent / "fixtures"
JPRL = json.loads((FIX / "nlc_jprl_c751.json").read_text(encoding="utf-8"))  # real identify answers for C 751 (871541)
TYPES = json.loads((FIX / "nlc_lesne_typy_c751.json").read_text(encoding="utf-8"))
C751 = geo.parcel_shape(json.loads((FIX / "wfs_c751_geom.json").read_text(encoding="utf-8")))


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _t():
    return L.Target(lat=49.175638, lon=19.328919, geom=C751, vymera_m2=26293)


def test_parse_forest_c751():
    o = L.parse_forest(JPRL, TYPES, _t(), 2026)
    assert o["lesny_pozemok"] is True and o["podiel_lesa"] == pytest.approx(1.0, abs=0.01)
    assert [s["jprl"] for s in o["jprl"]] == ["532b3", "532b4", "532b1", "532b2"]  # by share
    s = o["jprl"][0]
    assert s["kategoria_lesa"] == {"kod": "H", "nazov": "hospodársky"} and s["lesny_celok"] == "LESNÝ CELOK DOLNÝ KUBÍN"
    assert s["lhp_platnost"] == {"od": 2016, "do": 2025} and s["druh_pozemku"] is None  # 'Null' -> None
    ft = o["lesne_typy"][0]
    assert ft["kod_lesneho_typu"] == "5209" and ft["vegetacny_stupen"] == "jedlovoBukovy" and "  " not in ft["hslt"]
    assert "2025" in o["varovanie"]


def test_parse_forest_no_people_fields():
    text = json.dumps(L.parse_forest(JPRL, TYPES, _t(), 2026), ensure_ascii=False)
    for field in ("POZN", "GUID", "id_Plocha", "obhospodarovatel", "vlastnik"):
        assert field not in text


def test_parse_forest_current_plan_and_empty():
    o = L.parse_forest(JPRL, TYPES, _t(), 2025)
    assert "varovanie" not in o
    empty = L.parse_forest({"results": []}, {"results": []}, _t(), 2026)
    assert empty == {**empty, "lesny_pozemok": False, "jprl": [], "lesne_typy": []} and "podiel_lesa" not in empty


@pytest.mark.anyio
async def test_vrstvy_les_two_requests(tmp_path):
    e503 = json.loads((FIX / "wfs_e503.json").read_text(encoding="utf-8"))
    calls = []

    def handler(request: httpx.Request):
        if request.url.host == "inspirews.skgeodesy.sk":
            ok = "cp_uo" in request.url.path and "_503.E" in request.url.params.get("CQL_FILTER", "")
            return httpx.Response(200, json=e503 if ok else {"type": "FeatureCollection", "features": []})
        if request.url.host == "gis.nlcsk.org":
            calls.append(request.url.path)
            return httpx.Response(200, json={"results": []})
        return httpx.Response(503)

    s = create_server(PUBLIC_POLICY, data_dir=tmp_path, http=_mock_http(PUBLIC_POLICY, handler, FakeTime()))
    out = await _call(s, "kataster_vrstvy", {"ku": "871541", "cislo": "E 503", "vrstvy": ["les"]})
    assert out["data"]["vrstvy"]["les"]["lesny_pozemok"] is False
    assert len(calls) == 2 and {z["id"]: z["oficialny"] for z in out["zdroje"]}["nlc_les"] is True
