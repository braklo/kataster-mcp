"""Roads, railways and power lines at a parcel from ZBGIS (GKÚ Bratislava / ÚGKK SR), CC BY 4.0.

Service `zbgis_antropogenne_prvky_wms_featureinfo` (ArcGIS WMS, verified 2026-10-08):
- line layers 25 forest/field road, 26 street, 27 railway siding, 28 main railway, 29 road class 1-3,
  30 motorway, 33 power line (shown only up to 1:9449; with EPSG:4326 the scale is reckoned in degrees,
  so the pixel is kept at 1.2e-5 deg ~ 1:4800);
- GetFeatureInfo hits only a drawn pixel (a point next to the line returns nothing), but then returns the
  WHOLE feature with its polyline (lon lat, EPSG:4326) and attributes: `Typ cesty`, `Typ povrchu cesty`,
  `Prenášané napätie (kV)`, `DIGEST kód` (AP030 road, AN010 railway, AT030 power line), `ZBGIS_ID`.
  The same feature is repeated once per queried layer, so the layer name is not its type.

So: one GetMap of all line layers around the parcel, then GetFeatureInfo on drawn pixels, nearest first,
until every drawn pixel lies on a feature already known or the budget is spent. Protection zones are NOT
computed (they depend on law and voltage); the answer only warns.
"""

import io
import math
import re
import xml.etree.ElementTree as ET

import numpy as np
import shapely
from PIL import Image
from shapely.geometry import LineString, MultiLineString

from .. import geo
from ..envelope import KatasterError

URL = "https://zbgisws.skgeodesy.sk/zbgis_antropogenne_prvky_wms_featureinfo/service.svc/get"
SOURCE = "zbgis_liniove_objekty"
LINE_LAYERS = "25,26,27,28,29,30,33"
PX_DEG = 1.2e-5
MAX_PX_DEG = 2.3e-5  # 1:9449 for power lines
MAX_SIDE = 2048
NEAR_M = 50.0
ADJACENT_M = 6.0  # ZBGIS roads are centre lines: half a road width plus a margin (C 660/66: 3.8 m from a street)
GFI_BUDGET = 4
COVER_PX = 4
_NS = {"w": "http://www.esri.com/wms"}

KIND = {"AP030": "cesta", "AN010": "zeleznica", "AT030": "elektricke_vedenie"}
WARN_ZONES = ("siete: a protection zone (ochranné pásmo) of a nearby road, railway or power line may reach the "
              "parcel; it is not computed here (it depends on the law and the voltage), check with the operator.")
WARN_INCOMPLETE = "siete: more line objects are drawn near the parcel than were identified (query budget)."
NOTE = ("Line objects of ZBGIS (GKÚ, CC BY 4.0) on the parcel or within 50 m, with exact distance and length "
        "on the parcel. Access = nearest road of ZBGIS; neighbouring cadastral road parcels are not checked.")


def frame(bounds: tuple, lat0: float) -> tuple[tuple, int, int, float]:
    """Parcel bounds + 50 m -> (bbox lon/lat, width, height, deg per px)."""
    minx, miny, maxx, maxy = bounds
    dlon = NEAR_M / (111320 * math.cos(math.radians(lat0)))
    dlat = NEAR_M / 110540
    box = (minx - dlon, miny - dlat, maxx + dlon, maxy + dlat)
    px = max(PX_DEG, (box[2] - box[0]) / MAX_SIDE, (box[3] - box[1]) / MAX_SIDE)
    if px > MAX_PX_DEG:
        raise KatasterError("invalid_input", "Parcel too large for the line-object check (power lines are drawn "
                            "only at large scale).")
    return box, math.ceil((box[2] - box[0]) / px), math.ceil((box[3] - box[1]) / px), px


def _wms(box: tuple, w: int, h: int) -> dict:
    minx, miny, maxx, maxy = box
    return {"SERVICE": "WMS", "VERSION": "1.3.0", "LAYERS": LINE_LAYERS, "STYLES": "", "CRS": "EPSG:4326",
            "BBOX": f"{miny},{minx},{maxy},{maxx}", "WIDTH": w, "HEIGHT": h}


def map_params(box, w, h) -> dict:
    return {**_wms(box, w, h), "REQUEST": "GetMap", "FORMAT": "image/png", "TRANSPARENT": "TRUE"}


def gfi_params(box, w, h, i, j) -> dict:
    return {**_wms(box, w, h), "REQUEST": "GetFeatureInfo", "QUERY_LAYERS": LINE_LAYERS, "I": i, "J": j,
            "INFO_FORMAT": "application/vnd.esri.wms_raw_xml", "FEATURE_COUNT": 20}


def _num(v):
    try:
        return float(v.replace(",", ".").replace(" ", ""))
    except (AttributeError, ValueError):
        return None


def parse_features(text: str) -> dict:
    """ESRI raw XML -> {ZBGIS_ID: {'attrs': {...}, 'geom': (Multi)LineString}} (duplicates per layer merged)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"not a FeatureInfo XML: {e}") from e
    if not root.tag.endswith("FeatureInfoResponse"):
        raise ValueError(f"unexpected root {root.tag}")
    out = {}
    for coll in root.findall("w:FeatureInfoCollection", _NS):
        for fi in coll.findall("w:FeatureInfo", _NS):
            attrs, paths = {}, []
            for fld in fi.findall("w:Field", _NS):
                name = (fld.findtext("w:FieldName", "", _NS) or "").strip()
                g = fld.find("w:FieldGeometry", _NS)
                if g is not None:
                    for path in g.iter("{http://www.esri.com/wms}Path"):
                        pts = [tuple(map(float, p.text.split(","))) for p in path.findall("w:Point", _NS)]
                        if len(pts) >= 2:
                            paths.append(pts)
                    continue
                val = (fld.findtext("w:FieldValue", "", _NS) or "").strip()
                attrs[name] = None if val in ("", "Null", "N/A") or val.startswith("-3276") else val  # -32767/-32768 = unknown
            fid = attrs.get("ZBGIS_ID") or attrs.get("OBJECTID")
            if fid and paths and fid not in out:
                geom = LineString(paths[0]) if len(paths) == 1 else MultiLineString(paths)
                out[fid] = {"attrs": attrs, "geom": geom}
    return out


def describe(attrs: dict) -> dict:
    kind = KIND.get(attrs.get("DIGEST kód") or "", "ine")
    d = {"druh": kind, "digest": attrs.get("DIGEST kód")}
    if kind == "cesta":
        d["typ"] = attrs.get("Typ cesty")
        d["povrch"] = attrs.get("Typ povrchu cesty")
        d["cislo_cesty"] = attrs.get("Oficiálne číslo cesty")
        d["sirka_m"] = _num(attrs.get("Celková využiteľná šírka cesty v m"))
    elif kind == "elektricke_vedenie":
        v = attrs.get("Prenášané napätie (kV)")
        d["napatie_kv"] = _num(re.sub(r"[^\d,.]", "", v)) if v else None
    else:
        d.update({k: v for k, v in attrs.items() if k.startswith("Typ") and v})
    d["stav"] = attrs.get("Stav objektu")
    return {k: v for k, v in d.items() if v is not None}


def drawn(png: bytes, w: int, h: int) -> np.ndarray:
    a = np.array(Image.open(io.BytesIO(png)).convert("RGBA"))[:, :, 3]
    if a.shape != (h, w):
        raise KatasterError("parser_drift", f"ZBGIS map size {a.shape} differs from request {(h, w)}.")
    return np.argwhere(a > 0)


def pixel_points(hits: np.ndarray, box: tuple, px: float) -> np.ndarray:
    lon = box[0] + (hits[:, 1] + 0.5) * px
    lat = box[3] - (hits[:, 0] + 0.5) * px
    return shapely.points(lon, lat)


def summarize(features: dict, parcel, lat0: float, complete: bool) -> dict:
    pm = geo.to_metric(parcel, lat0)
    rows = []
    for fid, f in features.items():
        g = geo.to_metric(f["geom"], lat0)
        dist = float(pm.distance(g))
        if dist > NEAR_M:
            continue
        on = float(g.intersection(pm).length)
        rows.append({**describe(f["attrs"]), "pretina": on > 0, "dlzka_na_parcele_m": round(on, 1),
                     "vzdialenost_m": round(dist, 1)})
    rows.sort(key=lambda r: (r["vzdialenost_m"], r["druh"]))
    roads = [r for r in rows if r["druh"] == "cesta"]
    out = {
        "pristup": {
            "susedi_s_cestou": bool(roads) and roads[0]["vzdialenost_m"] <= ADJACENT_M,
            "kriterium": f"road centre line within {ADJACENT_M:g} m of the parcel boundary",
            "najblizsia_cesta": ({k: roads[0][k] for k in ("typ", "povrch", "vzdialenost_m") if k in roads[0]}
                                 if roads else None),
        },
        "liniove_objekty": rows,
        "uplne": complete,
        "poznamka": NOTE,
    }
    if not roads:
        out["pristup"]["poznamka"] = "no ZBGIS road within 50 m"
    return out


async def siete(http, t) -> dict:
    if t.geom is None:
        raise KatasterError("invalid_input", "Line objects need a parcel (ku + cislo), not a point.")
    box, w, h, px = frame(t.geom.bounds, t.lat)
    resp = await http.get(URL, map_params(box, w, h))
    if not resp.headers.get("content-type", "").startswith("image/"):
        raise KatasterError("unavailable", f"ZBGIS: expected an image, got {resp.headers.get('content-type')}")
    hits = drawn(resp.content, w, h)
    features: dict = {}
    if len(hits):
        pts = pixel_points(hits, box, px)
        dist = shapely.distance(geo.to_metric(pts, t.lat), geo.to_metric(t.geom, t.lat))
        order = np.argsort(dist)
        covered = dist > NEAR_M + 2  # bbox corners lie farther than 50 m: not asked for
        for _ in range(GFI_BUDGET):
            left = [i for i in order if not covered[i]]
            if not left:
                break
            r, c = hits[left[0]]
            g = await http.get(URL, gfi_params(box, w, h, int(c), int(r)))
            try:
                found = parse_features(g.text)
            except ValueError as e:
                raise KatasterError("parser_drift", f"ZBGIS FeatureInfo changed: {e}", details=g.text[:300]) from e
            covered[left[0]] = True  # a miss must not repeat the same pixel
            for fid, f in found.items():
                features.setdefault(fid, f)
                covered |= shapely.distance(pts, f["geom"]) <= COVER_PX * px
        complete = bool(covered.all())
    else:
        complete = True
    out = summarize(features, t.geom, t.lat, complete)
    if not complete:
        out["varovanie_neuplne"] = WARN_INCOMPLETE
    return out


def warnings(d: dict):
    msgs = []
    if d.get("liniove_objekty"):
        msgs.append(WARN_ZONES)
    if d.get("varovanie_neuplne"):
        msgs.append(d["varovanie_neuplne"])
    return " ".join(msgs) or None
