import json
from pathlib import Path

import httpx
import pytest
from shapely.geometry import LineString, box

from kataster_mcp import geo
from kataster_mcp.envelope import KatasterError
from kataster_mcp.policy import PUBLIC_POLICY
from kataster_mcp.sources import layers as L
from kataster_mcp.sources import zbgis

from test_core import FakeTime, _mock_http

FIX = Path(__file__).parent / "fixtures"
GEOM = geo.parcel_shape(json.loads((FIX / "wfs_e599_3_geom.json").read_text(encoding="utf-8")))
MAP = (FIX / "zbgis_e599_3_map.png").read_bytes()  # real GetMap + 4 GetFeatureInfo answers for E 599/3 (871541)
GFI = json.loads((FIX / "zbgis_e599_3_gfi.json").read_text(encoding="utf-8"))


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _target():
    return L.Target(lat=49.17887429, lon=19.31914428, geom=GEOM, register="E", cislo="599/3")


def test_parse_features_power_line_and_dedup():
    feats = {}
    for x in GFI:
        feats.update(zbgis.parse_features(x))
    kinds = sorted({zbgis.describe(f["attrs"])["druh"] for f in feats.values()})
    assert "elektricke_vedenie" in kinds and "cesta" in kinds
    volts = {zbgis.describe(f["attrs"]).get("napatie_kv") for f in feats.values()}
    assert {110.0, 220.0} <= volts
    one = zbgis.parse_features(GFI[0])
    assert one and all(f["geom"].geom_type in ("LineString", "MultiLineString") and f["geom"].length > 0
                       for f in one.values())
    with pytest.raises(ValueError):
        zbgis.parse_features("<html/>")


def test_unknown_sentinels_and_free_text_dropped():
    d = zbgis.describe({"DIGEST kód": "AP030", "Typ cesty": "Ulica", "Celková využiteľná šírka cesty v m": None,
                        "Popis, poznámka": "anything"})
    assert d == {"druh": "cesta", "digest": "AP030", "typ": "Ulica"}


def test_summarize_distance_length_and_access():
    parcel = box(19.0, 49.0, 19.001, 49.001)  # ~73 x 111 m
    feats = {
        "a": {"attrs": {"DIGEST kód": "AP030", "Typ cesty": "Ulica"},
              "geom": LineString([(19.00104, 48.999), (19.00104, 49.002)])},  # ~3 m east of the parcel
        "b": {"attrs": {"DIGEST kód": "AT030", "Prenášané napätie (kV)": "22 kV"},
              "geom": LineString([(18.999, 49.0005), (19.002, 49.0005)])},  # crosses it
        "c": {"attrs": {"DIGEST kód": "AN010"}, "geom": LineString([(19.01, 49.0), (19.01, 49.001)])},  # ~660 m away
    }
    o = zbgis.summarize(feats, parcel, 49.0, True)
    rows = {r["druh"]: r for r in o["liniove_objekty"]}
    assert set(rows) == {"cesta", "elektricke_vedenie"}
    assert rows["elektricke_vedenie"]["napatie_kv"] == 22 and rows["elektricke_vedenie"]["dlzka_na_parcele_m"] == pytest.approx(73, abs=2)
    assert o["pristup"]["susedi_s_cestou"] is True and o["pristup"]["najblizsia_cesta"]["vzdialenost_m"] == pytest.approx(2.9, abs=0.5)
    assert zbgis.warnings(o) == zbgis.WARN_ZONES
    empty = zbgis.summarize({}, parcel, 49.0, True)
    assert empty["pristup"]["najblizsia_cesta"] is None and zbgis.warnings(empty) is None


def test_frame_scale_limit():
    b, w, h, px = zbgis.frame((19.0, 49.0, 19.001, 49.001), 49.0)
    assert px == zbgis.PX_DEG and w > 0 and h > 0
    with pytest.raises(KatasterError):
        zbgis.frame((19.0, 49.0, 19.06, 49.06), 49.0)


@pytest.mark.anyio
async def test_siete_replay_e599_3():
    answers = iter(GFI)
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url.params["REQUEST"])
        if request.url.params["REQUEST"] == "GetMap":
            return httpx.Response(200, content=MAP, headers={"content-type": "image/png"})
        return httpx.Response(200, text=next(answers), headers={"content-type": "application/vnd.esri.wms_raw_xml"})

    o = await zbgis.siete(_mock_http(PUBLIC_POLICY, handler, FakeTime()), _target())
    assert calls == ["GetMap"] + ["GetFeatureInfo"] * 4 and o["uplne"] is True
    crossing = [r for r in o["liniove_objekty"] if r["pretina"]]
    assert sorted(r["napatie_kv"] for r in crossing) == [110.0, 220.0]
    assert o["pristup"]["susedi_s_cestou"] is False and o["pristup"]["najblizsia_cesta"]["vzdialenost_m"] == pytest.approx(10.7, abs=0.2)


@pytest.mark.anyio
async def test_siete_needs_parcel():
    with pytest.raises(KatasterError):
        await zbgis.siete(_mock_http(PUBLIC_POLICY, lambda r: httpx.Response(500), FakeTime()), L.Target(lat=49.1, lon=19.3))
