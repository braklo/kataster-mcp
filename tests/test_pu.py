import json
from pathlib import Path

import httpx
import pytest

from kataster_mcp.envelope import KatasterError
from kataster_mcp.policy import PUBLIC_POLICY
from kataster_mcp.server import create_server
from kataster_mcp.sources import layers as L
from kataster_mcp.sources import pu

from test_core import FakeTime, _call, _mock_http

FIX = Path(__file__).parent / "fixtures"
FILES = {  # NKOD distribution id -> fixture (real rows of the MPRV lists, no personal data)
    "845d1039-801d-4237-b48e-a4ed5e85243a": "mprv_pu_kpu_ukoncene.csv",
    "161a6540-8e85-482b-8ede-1f42fe225482": "mprv_pu_kpu_rozprac.csv",
    "f98d894c-268a-4771-94c4-21d0bb0fde50": "mprv_pu_jpu_ukoncene.csv",
    "8867c7a1-869e-4994-81b7-043b80d8f5b9": "mprv_pu_jpu_rozprac.csv",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _raw(dist_id):
    return (FIX / FILES[dist_id]).read_bytes()


def test_parse_all_four_headers():
    for typ, stav, dist_id in pu.DATASETS:
        rows = pu.parse_csv(_raw(dist_id), typ, stav)
        assert rows and all(r["typ"] == typ and r["stav"] == stav for r in rows)
        assert all(r["ku_kody"] and r["nazov"] for r in rows)  # empty rows are skipped
        assert all(isinstance(r["rok_nariadenia"], int) for r in rows)
        if stav == "ukoncene":
            assert all(isinstance(r["rok_schvalenia"], int) for r in rows)


def test_multi_code_cell_and_numbers():
    rows = pu.parse_csv(_raw("845d1039-801d-4237-b48e-a4ed5e85243a"), "komplexne", "ukoncene")
    multi = [r for r in rows if len(r["ku_kody"]) > 1]
    assert multi and multi[0]["ku_kody"] == ["848531", "848522"]
    assert any(isinstance(r["vymera_obvodu_ha"], int) and r["vymera_obvodu_ha"] > 999 for r in rows)  # "1 023"
    hit = pu.for_ku(rows, "848522")
    assert hit[0]["ine_ku"] == ["848531"]


def test_year_and_number_edge_cases():
    assert pu._year("2023") == 2023 and pu._year("") is None and pu._year("20234") == "20234"
    assert pu._number("1 023") == 1023 and pu._number("0,5") == 0.5 and pu._number("") is None


def test_header_drift_is_reported():
    with pytest.raises(ValueError):
        pu.parse_csv("a;b;c\r\n1;2;3".encode("cp1250"), "komplexne", "ukoncene")


@pytest.mark.anyio
async def test_needs_parcel():
    http = _mock_http(PUBLIC_POLICY, lambda r: httpx.Response(500), FakeTime())
    with pytest.raises(KatasterError):
        await pu.pozemkove_upravy(http, L.Target(lat=49.18, lon=19.32))


@pytest.mark.anyio
async def test_vrstvy_pozemkove_upravy_e503_and_dataset_cache(tmp_path):
    e503 = json.loads((FIX / "wfs_e503.json").read_text(encoding="utf-8"))
    downloads = []

    def handler(request: httpx.Request):
        if request.url.host == "inspirews.skgeodesy.sk":
            ok = "cp_uo" in request.url.path and "_503.E" in request.url.params.get("CQL_FILTER", "")
            return httpx.Response(200, json=e503 if ok else {"type": "FeatureCollection", "features": []})
        if request.url.host == "data.slovensko.sk":
            dist_id = request.url.params["id"]
            downloads.append(dist_id)
            return httpx.Response(200, content=_raw(dist_id), headers={"content-type": "text/csv"})
        return httpx.Response(503)

    s = create_server(PUBLIC_POLICY, data_dir=tmp_path, http=_mock_http(PUBLIC_POLICY, handler, FakeTime()))
    out = await _call(s, "kataster_vrstvy", {"ku": "871541", "cislo": "E 503", "vrstvy": ["pozemkove_upravy"]})
    d = out["data"]["vrstvy"]["pozemkove_upravy"]
    assert d["ku_kod"] == "871541" and d["prebiehaju"] is True
    assert [(p["typ"], p["stav"], p["rok_nariadenia"]) for p in d["projekty"]] == [("jednoduche", "rozpracovane", 2023)]
    assert {z["id"]: z["oficialny"] for z in out["zdroje"]}["mprv_pu"] is True
    assert any("whole cadastral unit" in w for w in out["varovania"])
    assert sorted(downloads) == sorted(FILES)



@pytest.mark.anyio
async def test_datasets_cached_across_cadastral_units(tmp_path):
    from kataster_mcp.cache import Cache
    downloads = []

    def handler(request: httpx.Request):
        downloads.append(request.url.params["id"])
        return httpx.Response(200, content=_raw(request.url.params["id"]), headers={"content-type": "text/csv"})

    http = _mock_http(PUBLIC_POLICY, handler, FakeTime())
    store = Cache(tmp_path / "cache.sqlite", private=False)
    a = await pu.pozemkove_upravy(http, L.Target(lat=0, lon=0, ku_kod="871541"), store=store)
    b = await pu.pozemkove_upravy(http, L.Target(lat=0, lon=0, ku_kod="848522"), store=store)
    assert a["prebiehaju"] is True and b["projekty"][0]["stav"] == "ukoncene"
    assert len(downloads) == 4  # second k.ú. served from the dataset cache
    none = await pu.pozemkove_upravy(http, L.Target(lat=0, lon=0, ku_kod="999999"), store=store)
    assert none["projekty"] == [] and none["prebiehaju"] is False
