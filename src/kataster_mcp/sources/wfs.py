"""Parcel geometry from the official INSPIRE WFS of ÚGKK SR (inspirews.skgeodesy.sk).

- register C: cp:CP.CadastralParcel, register E: cp_uo:CP.CadastralParcelUO
- nationalCadastralReference = '<k.ú. code>_<number>.<C|E>'
- GeoJSON coordinates are [lon, lat]; CQL POINT() takes (lat lon).
"""

from ..envelope import KatasterError

BASE = "https://inspirews.skgeodesy.sk/geoserver"
LAYERS = {"C": ("cp", "cp:CP.CadastralParcel"), "E": ("cp_uo", "cp_uo:CP.CadastralParcelUO")}
SOURCE_ID = "ugkk_inspire_wfs"


def url_for(register: str) -> str:
    return f"{BASE}/{LAYERS[register][0]}/ows"


def _params(register: str, cql: str) -> dict:
    return {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeNames": LAYERS[register][1], "outputFormat": "application/json", "CQL_FILTER": cql,
    }


def _bbox(geom: dict) -> list[float]:
    lons, lats = [], []

    def walk(c):
        if isinstance(c[0], (int, float)):
            lons.append(c[0])
            lats.append(c[1])
        else:
            for x in c:
                walk(x)

    walk(geom["coordinates"])
    return [min(lats), min(lons), max(lats), max(lons)]


def parse_parcel(feature: dict, register: str) -> dict:
    """Turn one WFS feature into the tool's parcel record."""
    p = feature["properties"]
    ref = p["nationalCadastralReference"]
    ku_kod, rest = ref.split("_", 1)
    number, reg = rest.rsplit(".", 1)
    if reg != register or number != p["label"]:
        raise ValueError(f"unexpected reference {ref} for label {p['label']}")
    area = p["areaValue"]
    if area.get("@uom") != "m2":
        raise ValueError(f"unexpected area unit {area.get('@uom')}")
    rp = p["referencePoint"]["coordinates"]
    geom = feature["geometry"]
    return {
        "register": register,
        "cislo": number,
        "ku_kod": ku_kod,
        "oznacenie": ref,
        "vymera_m2": area["value"],
        "referencny_bod": {"lat": rp[1], "lon": rp[0]},
        "bbox": _bbox(geom),
        "platne_od": p.get("validFrom"),
        "verzia_zdroja": (p.get("inspireId") or {}).get("versionId"),
        "geometria": geom,
    }


def _parse_all(data: dict, register: str) -> list[dict]:
    try:
        return [parse_parcel(f, register) for f in data["features"]]
    except (KeyError, ValueError, TypeError, IndexError) as e:
        raise KatasterError("parser_drift", f"INSPIRE WFS response changed: {e}") from e


async def by_number(http, ku_kod: str, number: str, register: str) -> list[dict]:
    ref = f"{ku_kod}_{number}.{register}"
    data = await http.get_json(url_for(register), _params(register, f"nationalCadastralReference = '{ref}'"))
    return _parse_all(data, register)


async def at_point(http, lat: float, lon: float, register: str) -> list[dict]:
    data = await http.get_json(url_for(register), _params(register, f"INTERSECTS(geometry,POINT({lat} {lon}))"))
    return _parse_all(data, register)


async def in_bbox(http, bounds: tuple[float, float, float, float], register: str, limit: int = 300) -> list[dict]:
    """Parcels whose geometry intersects a lon/lat box (minlon, minlat, maxlon, maxlat).

    BBOX takes lon lat, unlike INTERSECTS(POINT(lat lon)). A long polygon WKT in the URL was
    rejected by the WAF (2026-10-07), so neighbours are searched by bbox and filtered locally.
    """
    minx, miny, maxx, maxy = (round(v, 7) for v in bounds)
    params = _params(register, f"BBOX(geometry,{minx},{miny},{maxx},{maxy})")
    params["count"] = limit
    data = await http.get_json(url_for(register), params)
    return _parse_all(data, register)


PARSERS = (parse_parcel, _bbox, _parse_all)
