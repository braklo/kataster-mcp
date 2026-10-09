import json
from pathlib import Path

import httpx
import pytest
from shapely.geometry import box, shape

from kataster_mcp import geo
from kataster_mcp.policy import PUBLIC_POLICY
from kataster_mcp.server import create_server
from kataster_mcp.sources import layers as L

from test_core import FakeTime, _call, _mock_http

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_share_half_overlap():
    parcel = box(19.0, 49.0, 19.001, 49.001)
    other = box(19.0005, 48.9, 19.1, 49.1)
    assert geo.share(other, parcel) == pytest.approx(0.5, abs=1e-6)


def test_esri_rings_roundtrip_with_hole():
    outer = box(0, 0, 10, 10)
    poly = outer.difference(box(4, 4, 6, 6))
    back = geo.esri_rings_to_shape(geo.esri_rings(poly))
    assert back.area == pytest.approx(96)


def test_query_polygon_is_simplified():
    g = shape(json.loads((FIX / "wfs_e503.json").read_text(encoding="utf-8"))["features"][0]["geometry"]).buffer(0.0001, quad_segs=64)
    q = geo.query_polygon(g)
    assert geo._vertex_count(q) <= geo.MAX_QUERY_VERTICES
    assert geo.share(q, g) > 0.99


def _ring(b):
    return [list(c) for c in b.exterior.coords][::-1]  # clockwise = outer for ESRI


def test_parse_bpej_shares_and_protection():
    parcel = box(19.0, 49.0, 19.001, 49.001)
    t = L.Target(lat=49.0005, lon=19.0005, geom=parcel, vymera_m2=1000)
    a1 = {"BPEJ": "0964443", "Skupina kvality": "8", "Bodová hodnota produkčného potenciálu": "37",
          "Trvalý záber [€/m²]": "0,7", "Dočasný záber [€/²]": "0,01"}
    data = {"results": [
        {"layerId": 0, "attributes": a1, "geometry": {"rings": [_ring(box(18.9, 48.9, 19.00075, 49.1))]}},
        {"layerId": 0, "attributes": {**a1, "BPEJ": "0964543", "Skupina kvality": "7"},
         "geometry": {"rings": [_ring(box(19.00075, 48.9, 19.1, 49.1))]}},
        {"layerId": 1, "attributes": {"BPEJ": "0964543"}},
    ]}
    rows = L.parse_bpej(data, t)
    assert [r["bpej"] for r in rows] == ["0964443", "0964543"]
    assert rows[0]["podiel"] == pytest.approx(0.75, abs=1e-3) and rows[0]["vymera_m2"] == 750
    assert rows[0]["odvod_trvaly_zaber_eur_m2"] == 0.7 and rows[0]["odvod_docasny_zaber_eur_m2"] == 0.01
    assert (rows[0]["chranena_poda"], rows[1]["chranena_poda"]) == (False, True)


def test_parse_floods_and_dmr_and_sop():
    f = L.parse_floods({"results": [{"layerId": 10, "attributes": {}}, {"layerId": 17, "attributes": {}}]},
                       {"results": [{"layerId": 31, "attributes": {"Raster.hlbka": "do 0,5 m"}},
                                    {"layerId": 24, "attributes": {"UniqueValue.Pixel Value": "NoData"}}]})
    assert f["zaplavove_uzemie"] == ["Q100", "Q1000"] and f["hlbka_v_referencnom_bode"] == {"Q100": "do 0,5 m"}
    assert L.parse_dmr("@DMR 5.0 Stretch.Pixel Value; 543.130005; ") == 543.1
    with pytest.raises(ValueError):
        L.parse_dmr("<html>error</html>")
    s = L.parse_sop([("chranene_uzemia_chvu", "CHVÚ", {"features": [{"properties": {
        "NATIONALSITECODE": "SKCHVU050", "SITETITLE_SK": "Chočské vrchy", "categoryName": "CHVU", "AREA_HA": 1}}]}),
        ("sz_protection_degree", "deg", {"features": []})])
    assert s["chranene_uzemia"][0]["kod"] == "SKCHVU050" and isinstance(s["stupen_ochrany"], str)


@pytest.mark.anyio
async def test_vrstvy_failing_layer_does_not_fail_others(tmp_path):
    e503 = json.loads((FIX / "wfs_e503.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request):
        host = request.url.host
        if host == "inspirews.skgeodesy.sk":
            cql = request.url.params.get("CQL_FILTER", "")
            ok = "cp_uo" in request.url.path and "_503.E" in cql
            return httpx.Response(200, json=e503 if ok else {"type": "FeatureCollection", "features": []})
        if host == "zbgisws.skgeodesy.sk":
            return httpx.Response(200, text="@DMR 5.0 Stretch.Pixel Value; 543.13; ")
        return httpx.Response(503)

    s = create_server(PUBLIC_POLICY, data_dir=tmp_path, http=_mock_http(PUBLIC_POLICY, handler, FakeTime()))
    out = await _call(s, "kataster_vrstvy", {"ku": "871541", "cislo": "E 503", "vrstvy": ["vyska", "bpej"]})
    v = out["data"]["vrstvy"]
    assert v["vyska"]["nadmorska_vyska_m"] == 543.1
    assert v["bpej"]["chyba"]["typ"] == "unavailable"
    assert any("bpej" in w for w in out["varovania"])


def test_shared_boundary_between_adjacent_boxes():
    from kataster_mcp.skupina import shared_boundary_m
    a = box(19.0, 49.0, 19.001, 49.001)
    b = box(19.001, 49.0, 19.002, 49.0005)  # shares half of a's east edge
    edge_m = 0.0005 * 110540
    assert shared_boundary_m(a, b, 49.0) == pytest.approx(edge_m, abs=1.0)
    far = box(19.01, 49.0, 19.02, 49.001)
    assert shared_boundary_m(a, far, 49.0) == 0
