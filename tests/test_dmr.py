import json
import math
from pathlib import Path

import httpx
import numpy as np
import pytest
from shapely.geometry import box, shape

from kataster_mcp.envelope import KatasterError
from kataster_mcp.policy import PUBLIC_POLICY
from kataster_mcp.server import create_server
from kataster_mcp.sources import dmr
from kataster_mcp.sources import layers as L

from test_core import FakeTime, _call, _mock_http

FIX = Path(__file__).parent / "fixtures"
TIF = (FIX / "dmr_wcs_e503_half.tif").read_bytes()  # real WCS answer over E 503 with SCALEFACTOR 0.5 (~2 m)

LAT = 49.18
DLAT = -1 / 110540  # 1 m cells, rows run south
DLON = 1 / (111320 * math.cos(math.radians(LAT)))


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _plane(rise_east_pct, rise_north_pct, n=60):
    rows, cols = np.mgrid[0:n, 0:n].astype(float)
    north = -rows  # metres, row 0 is the northern edge
    east = cols
    a = 500 + east * rise_east_pct / 100 + north * rise_north_pct / 100
    grid = (19.0, DLON, LAT, DLAT)
    mask = np.zeros_like(a, dtype=bool)
    mask[6:-6, 6:-6] = True  # like the real request: the parcel sits inside a padded bbox
    return a, grid, mask


@pytest.mark.parametrize("east, north, smer", [(0, 10, "J"), (0, -10, "S"), (10, 0, "Z"), (-7.07, -7.07, "SV")])
def test_plane_slope_and_exposure(east, north, smer):
    a, grid, mask = _plane(east, north)
    o = dmr.slope_stats(a, grid, mask, None)
    assert o["sklon_celkovy_pct"] == pytest.approx(math.hypot(east, north), abs=0.2)
    assert o["sklon_priemer_pct"] == pytest.approx(math.hypot(east, north), abs=0.2)
    assert o["expozicia"]["smer"] == smer and o["smer_klesania"]["smer"] == smer
    assert o["prevysenie_m"] > 0 and "vyskovy_system" in o  # no alignment offset given


def test_flat_and_offset_and_few_points():
    a, grid, mask = _plane(0, 0)
    small = np.zeros_like(mask)
    small[:5, :5] = True
    o = dmr.slope_stats(a, grid, small, 42.5)
    assert o["expozicia"]["smer"] == "rovina" and o["smer_klesania"] == "rovina"
    assert o["vyska_min_m"] == pytest.approx(457.5) and o["posun_wcs_m"] == 42.5
    assert "varovanie" in o and o["body"] == 25


def test_box_mean_ignores_nan():
    a = np.arange(25, dtype=float).reshape(5, 5)
    a[0, 0] = np.nan
    b = dmr.box_mean(a, 3)
    assert np.isnan(b[0, 0]) and b[2, 2] == pytest.approx(12.0)
    assert b[0, 1] == pytest.approx(np.nanmean([a[0, 0], a[0, 1], a[0, 2], a[1, 0], a[1, 1], a[1, 2]]))


def test_read_real_geotiff():
    a, (lon0, dlon, lat0, dlat) = dmr.read_geotiff(TIF)
    assert a.shape == (105, 59) and np.isfinite(a).mean() > 0.9
    assert 19.31 < lon0 < 19.32 and 49.18 < lat0 < 49.181 and dlat < 0 < dlon
    assert 570 < np.nanmin(a) < np.nanmax(a) < 620  # WCS heights (~42.6 m above the WMS values)
    with pytest.raises(ValueError):
        dmr.read_geotiff(b"<ows:ExceptionReport/>")


def test_scale_for_large_parcels():
    assert dmr.scale_for((19.0, 49.0, 19.001, 49.001)) is None
    s = dmr.scale_for((19.0, 49.0, 19.03, 49.03))  # ~2.2 x 3.3 km
    assert 0 < s < 1


@pytest.mark.anyio
async def test_needs_parcel():
    http = _mock_http(PUBLIC_POLICY, lambda r: httpx.Response(500), FakeTime())
    with pytest.raises(KatasterError):
        await dmr.sklon(http, L.Target(lat=49.18, lon=19.32))


@pytest.mark.anyio
async def test_vrstvy_sklon_e503(tmp_path):
    e503 = json.loads((FIX / "wfs_e503.json").read_text(encoding="utf-8"))
    calls = []

    def handler(request: httpx.Request):
        host = request.url.host
        if host == "inspirews.skgeodesy.sk" and "/el/" in request.url.path:
            calls.append("wcs")
            return httpx.Response(200, content=TIF, headers={"content-type": "image/tiff"})
        if host == "inspirews.skgeodesy.sk":
            ok = "cp_uo" in request.url.path and "_503.E" in request.url.params.get("CQL_FILTER", "")
            return httpx.Response(200, json=e503 if ok else {"type": "FeatureCollection", "features": []})
        if host == "zbgisws.skgeodesy.sk":
            calls.append("gfi")
            return httpx.Response(200, text="@DMR 5.0 Stretch.Pixel Value; 543.13; ")
        return httpx.Response(503)

    s = create_server(PUBLIC_POLICY, data_dir=tmp_path, http=_mock_http(PUBLIC_POLICY, handler, FakeTime()))
    out = await _call(s, "kataster_vrstvy", {"ku": "871541", "cislo": "E 503", "vrstvy": ["sklon"]})
    d = out["data"]["vrstvy"]["sklon"]
    assert calls == ["wcs", "gfi"]
    assert d["smer_klesania"]["smer"] in ("SV", "V") and 5 < d["sklon_celkovy_pct"] < 20
    assert 530 < d["vyska_min_m"] < d["vyska_max_m"] < 570 and 40 < d["posun_wcs_m"] < 45
    assert {z["id"]: z["oficialny"] for z in out["zdroje"]}["ugkk_inspire_dtm"] is True
    point = await _call(s, "kataster_vrstvy", {"lat": 49.1839, "lon": 19.3184, "vrstvy": ["vyska"]})
    assert "sklon" not in point["data"]["vrstvy"]
    assert "sklon" in L.LAYERS and L.LAYERS["sklon"].parcel_only
    assert shape(e503["features"][0]["geometry"]).intersects(box(19.315, 49.178, 19.317, 49.181))
