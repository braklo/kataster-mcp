"""Cadastral map WMS of ÚGKK SR (kn_wms_norm): built-up area position and drawn encumbrances.

Verified 2026-10-07 (probe `scripts/probe_kn_wms_gfi.py`, k.ú. 871541):
- GetFeatureInfo honours each layer's MaxScaleDenominator: outside it the answer is empty,
  which does NOT mean "nothing here". Encumbrances (layer 4) show only at <= 1:1890.
- Layer 5 (parcel C) answers with public attributes, among them the position inside or outside
  the built-up area of the municipality (code 1 = inside, 2 = outside; both seen).
- Layers 4 (encumbrances), 11 (land consolidation projects) and 12 (built-up area boundary) are
  lines with an internal id only: no kind, no beneficiary, no geometry. A point inside an area hits
  nothing. Encumbrances are therefore found by rendering layer 4 and clipping it by the parcel.
- application/geo+json is broken for polygons (decimal comma in coordinates); the ESRI raw XML is not.
"""

import io
import math
import xml.etree.ElementTree as ET

import numpy as np
import shapely
from PIL import Image, ImageDraw

from ..envelope import KatasterError
from . import eskn

URL = "https://kataster.skgeodesy.sk/eskn/services/NR/kn_wms_norm/MapServer/WmsServer"
SOURCE = "kn_wms"
INFO_FORMAT = "application/vnd.esri.wms_raw_xml"
_NS = {"w": "http://www.esri.com/wms"}

LAYER_PARCEL_C = "5"
LAYER_ENCUMBRANCES = "4"
ZU_NAME = {"1": "v zastavanom území obce", "2": "mimo zastavaného územia obce"}

# Encumbrances: 0.4 map units (EPSG:3857 m) per pixel ~ 1:1430, under the 1:1890 limit at any latitude.
RES = 0.4
MAX_SIDE = 2048
MAX_TILES = 4
SAMPLES = 4
ERODE_M = 1.0

NOTE_TARCHY = ("Unofficial estimate from the rendered cadastral map (layer Ťarchy): a drawn encumbrance line "
               "crosses the parcel. Kind, content and beneficiary are only in part C of the LV (kataster_lv).")
WARN_BOUNDARY = ("Encumbrances: a line running along the common boundary with a neighbour can raise a false alarm "
                 "(the parcel is shrunk by 1 m before the test, which lowers but does not remove it).")


def to_3857(geom):
    k = 20037508.34 / 180

    def f(xy):
        x = xy[:, 0] * k
        y = np.log(np.tan((90 + xy[:, 1]) * np.pi / 360)) * 6378137
        return np.column_stack([x, y])

    return shapely.transform(geom, f)


def gfi_params(bbox: tuple, width: int, height: int, i: int, j: int, layer: str) -> dict:
    minx, miny, maxx, maxy = bbox
    return {
        "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetFeatureInfo", "LAYERS": layer, "QUERY_LAYERS": layer,
        "STYLES": "", "CRS": "EPSG:3857", "BBOX": f"{minx},{miny},{maxx},{maxy}", "WIDTH": width, "HEIGHT": height,
        "I": i, "J": j, "INFO_FORMAT": INFO_FORMAT, "FEATURE_COUNT": 50,
    }


def map_params(bbox: tuple, width: int, height: int, layer: str) -> dict:
    minx, miny, maxx, maxy = bbox
    return {
        "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "LAYERS": layer, "STYLES": "",
        "CRS": "EPSG:3857", "BBOX": f"{minx},{miny},{maxx},{maxy}", "WIDTH": width, "HEIGHT": height,
        "FORMAT": "image/png", "TRANSPARENT": "TRUE",
    }


def parse_gfi(text: str) -> list[dict]:
    """ESRI raw XML -> [{'vrstva': layer name, field: value, ...}]; geometry fields are skipped."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"not a FeatureInfo XML: {e}") from e
    if not root.tag.endswith("FeatureInfoResponse"):
        raise ValueError(f"unexpected root {root.tag}")
    out = []
    for coll in root.findall("w:FeatureInfoCollection", _NS):
        for fi in coll.findall("w:FeatureInfo", _NS):
            row = {"vrstva": coll.get("layername")}
            for fld in fi.findall("w:Field", _NS):
                if fld.find("w:FieldGeometry", _NS) is not None:
                    continue
                name = (fld.findtext("w:FieldName", "", _NS) or "").strip()
                val = (fld.findtext("w:FieldValue", "", _NS) or "").strip()
                row[name] = None if val in ("", "Null") else val
            out.append(row)
    return out


def _gfi_rows(text: str, what: str) -> list[dict]:
    try:
        return parse_gfi(text)
    except ValueError as e:
        raise KatasterError("parser_drift", f"{what}: {e}", details=text[:300]) from e


# --- Position in the built-up area (zastavané územie obce) ------------------------

def zu_result(kod: str | None, via: str) -> dict:
    return {
        "v_zastavanom_uzemi": {"1": True, "2": False}.get(kod or ""),
        "kod": kod,
        "nazov": ZU_NAME.get(kod or ""),
        "zdroj": via,
    }


def parse_zu_c(rows: list[dict], cislo: str | None) -> dict:
    parcels = [r for r in rows if r.get("Číslo parcely registra C")]
    if cislo is not None:
        parcels = [r for r in parcels if r["Číslo parcely registra C"] == cislo]
    if not parcels:
        raise KatasterError("not_found", "Cadastral map WMS shows no parcel C at the reference point.",
                            coverage="KN WMS layer 5 GetFeatureInfo at the WFS referencePoint")
    p = parcels[0]
    kod = p.get("Kód polohy v zastavanom území")
    out = zu_result(kod, "KN WMS, parcel C")
    out["parcela_c"] = p["Číslo parcely registra C"]
    if kod is not None and p.get("Názov polohy v zastavanom území"):
        out["nazov"] = p["Názov polohy v zastavanom území"]
    return out


async def zastavane_uzemie(http, t) -> dict:
    if t.register == "E":
        raw = await http.get_json(eskn.IDENTIFY_URL, eskn.identify_params(t.lat, t.lon))
        ident = eskn.parse_identify_safe(raw, "E", t.cislo)
        if ident["id"] is None:
            raise KatasterError("not_found", f"ESKN identify does not show E {t.cislo} at its reference point.",
                                coverage="ESKN identify at the WFS referencePoint")
        return zu_result(ident.get("poloha_zu_kod"), "ESKN identify, parcel E (unofficial)")
    x, y = eskn.to_3857(t.lat, t.lon)
    d = 10.0
    resp = await http.get(URL, gfi_params((x - d, y - d, x + d, y + d), 101, 101, 50, 50, LAYER_PARCEL_C))
    return parse_zu_c(_gfi_rows(resp.text, "KN WMS parcel C"), t.cislo)


# --- Drawn encumbrances (ťarchy) --------------------------------------------------

def _tiles(bounds: tuple) -> list[tuple]:
    """Split the bbox (EPSG:3857) into tiles of at most MAX_SIDE px at RES."""
    minx, miny, maxx, maxy = bounds
    w_px, h_px = math.ceil((maxx - minx) / RES), math.ceil((maxy - miny) / RES)
    nx, ny = math.ceil(w_px / MAX_SIDE), math.ceil(h_px / MAX_SIDE)
    if nx * ny > MAX_TILES:
        raise KatasterError("invalid_input", f"Parcel too large for the encumbrance check "
                            f"({(maxx - minx):.0f} x {(maxy - miny):.0f} map m; limit {MAX_TILES} tiles).")
    tw, th = math.ceil(w_px / nx), math.ceil(h_px / ny)
    tiles = []
    for ty in range(ny):
        for tx in range(nx):
            x0, y1 = minx + tx * tw * RES, maxy - ty * th * RES
            tiles.append(((x0, y1 - th * RES, x0 + tw * RES, y1), tw, th, tx * tw, ty * th))
    return tiles


def parcel_mask(geom3857, bbox: tuple, width: int, height: int) -> np.ndarray:
    minx, _, _, maxy = bbox
    img = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(img)

    def px(coords):
        return [((x - minx) / RES, (maxy - y) / RES) for x, y in coords]

    for p in getattr(geom3857, "geoms", [geom3857]):
        if p.is_empty:
            continue
        draw.polygon(px(p.exterior.coords), fill=1)
        for hole in p.interiors:
            draw.polygon(px(hole.coords), fill=0)
    return np.array(img, dtype=bool)


def drawn_pixels(png: bytes, mask: np.ndarray) -> np.ndarray:
    """(row, col) of non-transparent pixels inside the mask."""
    alpha = np.array(Image.open(io.BytesIO(png)).convert("RGBA"))[:, :, 3]
    if alpha.shape != mask.shape:
        raise KatasterError("parser_drift", f"KN WMS map size {alpha.shape} differs from request {mask.shape}.")
    return np.argwhere((alpha > 0) & mask)


def spread_sample(points: np.ndarray, k: int) -> list[int]:
    """Indices of up to k points far from each other (farthest-point sampling)."""
    if len(points) == 0:
        return []
    chosen = [0]
    dist = np.linalg.norm(points - points[0], axis=1)
    while len(chosen) < min(k, len(points)):
        i = int(dist.argmax())
        if dist[i] == 0:
            break
        chosen.append(i)
        dist = np.minimum(dist, np.linalg.norm(points - points[i], axis=1))
    return chosen


async def tarchy(http, t) -> dict:
    if t.geom is None:
        raise KatasterError("invalid_input", "Encumbrances need a parcel (ku + cislo), not a point.")
    poly = to_3857(t.geom)
    k = 1 / math.cos(math.radians(t.lat))
    test = poly.buffer(-ERODE_M * k)
    narrow = test.is_empty
    if narrow:
        test = poly
    minx, miny, maxx, maxy = poly.bounds
    pad = 2 * k
    tiles = _tiles((minx - pad, miny - pad, maxx + pad, maxy + pad))

    hits = []  # (tile index, row, col)
    for n, (bbox, w, h, _, _) in enumerate(tiles):
        resp = await http.get(URL, map_params(bbox, w, h, LAYER_ENCUMBRANCES))
        if not resp.headers.get("content-type", "").startswith("image/"):
            raise KatasterError("unavailable", f"KN WMS: expected an image, got {resp.headers.get('content-type')}")
        for r, c in drawn_pixels(resp.content, parcel_mask(test, bbox, w, h)):
            hits.append((n, int(r), int(c)))

    out = {"zakreslena_tarcha": bool(hits), "pixely": len(hits),
           "rozlisenie_m_px": round(RES / k, 2), "poznamka": NOTE_TARCHY}
    if narrow:
        out["uzka_parcela"] = "parcel narrower than 2 m: tested without shrinking"
    if not hits:
        return out
    glob = np.array([(tiles[n][4] + r, tiles[n][3] + c) for n, r, c in hits], dtype=float)
    ids = set()
    for idx in spread_sample(glob, SAMPLES):
        n, r, c = hits[idx]
        bbox, w, h, _, _ = tiles[n]
        resp = await http.get(URL, gfi_params(bbox, w, h, c, r, LAYER_ENCUMBRANCES))
        ids.update(row["identifikator"] for row in _gfi_rows(resp.text, "KN WMS encumbrances")
                   if row.get("identifikator"))
    out["identifikatory"] = sorted(ids)
    out["pocet_najmenej"] = len(ids)
    return out
