import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw
from shapely.geometry import box

from kataster_mcp.envelope import KatasterError
from kataster_mcp.policy import PUBLIC_POLICY
from kataster_mcp.server import create_server
from kataster_mcp.sources import kn_wms
from kataster_mcp.sources import layers as L

from test_core import FakeTime, _call, _mock_http

FIX = Path(__file__).parent / "fixtures"
GFI_E503 = (FIX / "kn_gfi_5_13_e503.xml").read_text(encoding="utf-8")
GFI_V_ZU = (FIX / "kn_gfi_5_v_zu.xml").read_text(encoding="utf-8")
GFI_TARCHY = (FIX / "kn_gfi_4_tarchy.xml").read_text(encoding="utf-8")
XML = "application/vnd.esri.wms_raw_xml"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_parse_gfi_fields_without_geometry():
    rows = kn_wms.parse_gfi(GFI_E503)
    assert [r["vrstva"] for r in rows] == ["Parcela registra C", "Katastrálne územia"]
    assert "Shape" in rows[0] and rows[0]["Číslo listu vlastníctva"] is None  # 'Null' -> None
    assert [r["identifikator"] for r in kn_wms.parse_gfi(GFI_TARCHY)] == ["111222107", "111222108"]
    with pytest.raises(ValueError):
        kn_wms.parse_gfi("<html>error</html>")


def test_zastavane_uzemie_c_both_codes():
    out = kn_wms.parse_zu_c(kn_wms.parse_gfi(GFI_E503), "807")
    assert (out["v_zastavanom_uzemi"], out["kod"], out["parcela_c"]) == (False, "2", "807")
    out = kn_wms.parse_zu_c(kn_wms.parse_gfi(GFI_V_ZU), None)
    assert (out["v_zastavanom_uzemi"], out["kod"]) == (True, "1")
    assert "v zastavanom" in out["nazov"]
    with pytest.raises(KatasterError) as e:
        kn_wms.parse_zu_c(kn_wms.parse_gfi(GFI_E503), "808")
    assert e.value.kind == "not_found"


def test_tiles_cover_bbox_and_limit():
    tiles = kn_wms._tiles((0, 0, 1000, 300))  # 2500 x 750 px -> 2 tiles
    assert len(tiles) == 2 and all(w <= kn_wms.MAX_SIDE and h <= kn_wms.MAX_SIDE for _, w, h, _, _ in tiles)
    assert tiles[0][0][0] == 0 and tiles[-1][0][2] >= 1000
    with pytest.raises(KatasterError):
        kn_wms._tiles((0, 0, 5000, 5000))


def test_spread_sample_picks_far_points():
    import numpy as np
    pts = np.array([[0, 0], [0, 1], [0, 100], [0, 50]], dtype=float)
    assert kn_wms.spread_sample(pts, 2) == [0, 2]
    assert kn_wms.spread_sample(np.empty((0, 2)), 4) == []


# Parcel ~37 x 33 m in Vyšný Kubín.
PARCEL = box(19.3240, 49.1840, 19.3245, 49.1843)


def _png(width, height, line=None):
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if line:
        ImageDraw.Draw(img).line(line(width, height), fill=(0, 0, 255, 255), width=2)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _wms_handler(line, calls):
    def handler(request: httpx.Request):
        p = request.url.params
        calls.append(p["REQUEST"])
        if p["REQUEST"] == "GetMap":
            return httpx.Response(200, content=_png(int(p["WIDTH"]), int(p["HEIGHT"]), line),
                                  headers={"content-type": "image/png"})
        return httpx.Response(200, text=GFI_TARCHY, headers={"content-type": XML})
    return handler


def _north_edge_px():
    """Row of the parcel's north edge in the requested map (bbox = parcel + 2 m pad, k = 1/cos(lat))."""
    import math
    return 2 / math.cos(math.radians(49.18415)) / kn_wms.RES


@pytest.mark.anyio
async def test_tarchy_line_across_parcel():
    calls = []
    http = _mock_http(PUBLIC_POLICY, _wms_handler(lambda w, h: [(0, h // 2), (w, h // 2)], calls), FakeTime())
    t = L.Target(lat=49.18415, lon=19.32425, geom=PARCEL, register="C", cislo="1")
    out = await kn_wms.tarchy(http, t)
    assert out["zakreslena_tarcha"] is True and out["pixely"] > 0
    assert out["identifikatory"] == ["111222107", "111222108"] and out["pocet_najmenej"] == 2
    assert calls.count("GetMap") == 1 and 1 <= calls.count("GetFeatureInfo") <= kn_wms.SAMPLES


@pytest.mark.anyio
@pytest.mark.parametrize("inside_m, hit", [(0.4, False), (3.0, True)])
async def test_tarchy_line_near_boundary(inside_m, hit):
    """1 m shrink: a line 0.4 m inside the edge is ignored; 3 m inside it is found (positive control)."""
    import math
    row = round(_north_edge_px() + inside_m / math.cos(math.radians(49.18415)) / kn_wms.RES)
    calls = []
    http = _mock_http(PUBLIC_POLICY, _wms_handler(lambda w, h: [(0, row), (w, row)], calls), FakeTime())
    out = await kn_wms.tarchy(http, L.Target(lat=49.18415, lon=19.32425, geom=PARCEL, register="C", cislo="1"))
    assert out["zakreslena_tarcha"] is hit
    assert ("GetFeatureInfo" in calls) is hit


@pytest.mark.anyio
async def test_tarchy_needs_parcel():
    http = _mock_http(PUBLIC_POLICY, _wms_handler(None, []), FakeTime())
    with pytest.raises(KatasterError):
        await kn_wms.tarchy(http, L.Target(lat=49.18, lon=19.32))


@pytest.mark.anyio
async def test_vrstvy_new_layers_e503(tmp_path):
    e503 = json.loads((FIX / "wfs_e503.json").read_text(encoding="utf-8"))
    ident = json.loads((FIX / "eskn_identify_e503.json").read_text(encoding="utf-8"))
    calls = []

    def handler(request: httpx.Request):
        if request.url.host == "inspirews.skgeodesy.sk":
            ok = "cp_uo" in request.url.path and "_503.E" in request.url.params.get("CQL_FILTER", "")
            return httpx.Response(200, json=e503 if ok else {"type": "FeatureCollection", "features": []})
        if request.url.path.endswith("/identify"):
            calls.append("identify")
            return httpx.Response(200, json=ident)
        return _wms_handler(lambda w, h: [(w // 2, 0), (w // 2, h)], calls)(request)

    s = create_server(PUBLIC_POLICY, data_dir=tmp_path, http=_mock_http(PUBLIC_POLICY, handler, FakeTime()))
    args = {"ku": "871541", "cislo": "E 503", "vrstvy": ["zastavane_uzemie", "tarchy"]}
    out = await _call(s, "kataster_vrstvy", args)
    v = out["data"]["vrstvy"]
    assert (v["zastavane_uzemie"]["v_zastavanom_uzemi"], v["zastavane_uzemie"]["kod"]) == (False, "2")
    assert v["tarchy"]["zakreslena_tarcha"] is True
    src = {z["id"]: z["oficialny"] for z in out["zdroje"]}
    assert src["eskn_identify"] is False and src["kn_wms_tarchy"] is False
    assert any("false alarm" in w for w in out["varovania"])

    n = len(calls)
    again = await _call(s, "kataster_vrstvy", args)
    assert len(calls) == n and all(z["z_cache"] for z in again["zdroje"] if z["id"] != "ugkk_inspire_wfs")
    assert any("false alarm" in w for w in again["varovania"])


@pytest.mark.anyio
async def test_vrstvy_point_default_skips_tarchy(tmp_path):
    def handler(request: httpx.Request):
        if request.url.host == "kataster.skgeodesy.sk":
            return httpx.Response(200, text=GFI_V_ZU, headers={"content-type": XML})
        return httpx.Response(503)

    s = create_server(PUBLIC_POLICY, data_dir=tmp_path, http=_mock_http(PUBLIC_POLICY, handler, FakeTime()))
    out = await _call(s, "kataster_vrstvy", {"lat": 49.1839, "lon": 19.3184})
    v = out["data"]["vrstvy"]
    assert "tarchy" not in v and v["zastavane_uzemie"]["v_zastavanom_uzemi"] is True
    assert {z["id"]: z["oficialny"] for z in out["zdroje"]}["kn_wms"] is True
