"""HTML map with sharp crops over the overview image (issue #7), offline."""

import copy
import io
import json
import re

import httpx
import pytest
from PIL import Image
from shapely.affinity import translate
from shapely.geometry import shape

from kataster_mcp import render
from kataster_mcp.policy import PUBLIC_POLICY
from kataster_mcp.server import create_server

from test_core import FIX, FakeTime, _call, _mock_http

E503 = json.loads((FIX / "wfs_e503.json").read_text(encoding="utf-8"))
# fake parcels: E 998 about 40 m east of E 503, E 999 about 2.2 km east
SHIFT = {"503": 0.0, "998": 0.0006, "999": 0.03}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _shifted(number: str) -> dict:
    fc = copy.deepcopy(E503)
    ft = fc["features"][0]
    ft["geometry"] = json.loads(json.dumps(translate(shape(ft["geometry"]), xoff=SHIFT[number]).__geo_interface__))
    for key in ("label", "nationalCadastralReference"):
        ft["properties"][key] = str(ft["properties"][key]).replace("503", number)
    ft["id"] = f"EP.{number}"
    return fc


def _server(tmp_path, calls):
    def handler(req: httpx.Request):
        calls.append(req)
        cql = req.url.params.get("CQL_FILTER", "")
        if "cp_uo" in req.url.path:
            m = re.search(r"871541_(\d+)\.E", cql)
            return httpx.Response(200, json=_shifted(m.group(1)) if m and m.group(1) in SHIFT
                                  else {"type": "FeatureCollection", "features": []})
        if req.url.params.get("REQUEST") == "GetMap":
            w, h = int(req.url.params["WIDTH"]), int(req.url.params["HEIGHT"])
            buf = io.BytesIO()
            Image.new("RGB", (w, h), (90, 140, 60)).save(buf, "JPEG")
            return httpx.Response(200, content=buf.getvalue(), headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    return create_server(PUBLIC_POLICY, data_dir=tmp_path / "data", limits_dir=tmp_path / "limity", http=_mock_http(PUBLIC_POLICY, handler, FakeTime()))


def _shapes(*numbers):
    return [shape(_shifted(n)["features"][0]["geometry"]) for n in numbers]


def test_clusters_group_near_parcels_only():
    assert render.clusters(_shapes("503", "999", "998")) == [[0, 2], [1]]
    assert render.clusters(_shapes("503")) == [[0]]


def test_detail_frames_only_where_sharper():
    far = _shapes("503", "998", "999")
    overview = render.Frame((min(s.bounds[0] for s in far), min(s.bounds[1] for s in far),
                             max(s.bounds[2] for s in far), max(s.bounds[3] for s in far)))
    crops = render.detail_frames(far, overview)
    assert [g for g, _ in crops] == [[0, 1], [2]]
    assert overview.metres_per_px > 1.0 and all(f.metres_per_px <= 0.25 for _, f in crops)
    assert all(f.metres_per_px >= render.DETAIL_FINEST_M_PX * 0.99 for _, f in crops)
    one = _shapes("503")
    assert render.detail_frames(one, render.Frame(one[0].bounds)) == []  # already sharp: no crop


@pytest.mark.anyio
async def test_html_map_has_crops_and_counts_requests(tmp_path):
    calls = []
    s = _server(tmp_path, calls)
    out = await _call(s, "kataster_mapa", {"parcely": [{"ku": "871541", "cislo": f"E {n}"} for n in ("503", "999", "998")],
                                           "format": ["html"], "podklad": "orto+kn", "out_dir": str(tmp_path / "m")})
    data = out["data"]
    getmaps = [c for c in calls if c.url.params.get("REQUEST") == "GetMap"]
    assert [v["parcely"] for v in data["vyrezy"]] == [["E 503", "E 998"], ["E 999"]]
    assert data["podklad_dotazy"] == {"wms": len(getmaps), "osm_dlazdice": 0} and len(getmaps) == 2 * 3
    assert data["mierka_m_na_px"] > 1.0 and all(v["mierka_m_na_px"] < 0.3 for v in data["vyrezy"])
    page = open(data["subory"]["html"], encoding="utf-8").read()
    assert page.count("L.imageOverlay(") == 3  # overview + 2 crops, all embedded
    assert page.index("L.imageOverlay(") < page.index("L.geoJSON(")
    assert "skgeodesy" not in page.split("<script>", 1)[1]  # the browser never asks the WMS itself
    assert "location.protocol.startsWith('http')" in page and "setMaxBounds" in page  # OSM only over http(s)


@pytest.mark.anyio
async def test_png_only_makes_no_crops(tmp_path):
    calls = []
    s = _server(tmp_path, calls)
    out = (await _call(s, "kataster_mapa", {"parcely": [{"ku": "871541", "cislo": "E 503"}, {"ku": "871541", "cislo": "E 999"}],
                                            "format": ["png"], "podklad": "orto", "out_dir": str(tmp_path / "m")}))["data"]
    assert out["vyrezy"] == [] and out["podklad_dotazy"]["wms"] == 1


@pytest.mark.anyio
async def test_osm_placeholder_tile_is_an_error():
    class Http:
        async def get(self, url, params=None):
            return httpx.Response(200, content=b"", headers={"content-type": "image/png",
                                                             "x-blocked": "No referer sent. Access denied."})

    class Env:
        def source(self, *a, **k):
            pass

    f = render.Frame(_shapes("503")[0].bounds)
    with pytest.raises(render.KatasterError) as e:
        await render.background(Http(), f, "osm", Env())
    assert e.value.kind == "unavailable" and "referer" in e.value.message
