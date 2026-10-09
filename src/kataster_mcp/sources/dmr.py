"""Terrain slope of a parcel from DMR 5.0 (digital terrain model, 1 m) of ÚGKK SR.

Source: INSPIRE Download Service - Elevation (DTM), WCS 2.0.1 on inspirews.skgeodesy.sk, coverage
`el__EL.GridCoverage`, CC BY 4.0 (verified 2026-10-08). One GetCoverage per parcel: the bbox is given in
WGS84 (`subsettingCrs`), the answer is a float32 GeoTIFF in EPSG:4326 (nearest neighbour, ~1 m cells),
georeferenced by ModelTransformationTag 34264, nodata 3.4e38. SCALEFACTOR < 1 shrinks a large request.

Heights from this WCS are ~42.6 m higher than the DMR WMS GetFeatureInfo used by the `vyska` layer
(constant within a parcel: 42.42-42.71 m at three points of E 503); the vertical system is not stated in the
service metadata. Slope does not depend on it; reported heights are aligned to the WMS value at the
reference point (one more request).
"""

import io
import math

import numpy as np
from PIL import Image, ImageDraw

from ..envelope import KatasterError

URL = "https://inspirews.skgeodesy.sk/geoserver/el/ows"
SOURCE = "ugkk_inspire_dtm"
COVERAGE = "el__EL.GridCoverage"
NODATA_ABOVE = 1e30
PAD_DEG = 0.0001  # ~7-11 m around the parcel: the 5 m smoothing window and the gradient at its edge need neighbours
MAX_PX = 1_000_000
FLAT_PCT = 2.0
FEW_POINTS = 100
SMOOTH_M = 5  # slope is read at ~5 m: on the raw 1 m grid banks and furrows dominate (E 503: max 143 % raw, 56 % at 5 m)
SECTORS = ["S", "SV", "V", "JV", "J", "JZ", "Z", "SZ"]  # sever, severovýchod, ... (Slovak compass)

NOTE = ("Slope from DMR 5.0 (ÚGKK, 1 m grid, terrain without vegetation and buildings), smoothed to ~5 m. "
        "sklon_celkovy = plane fitted through the parcel (overall tilt); priemer/max/p95 = local slope inside it. "
        "Exposure is the direction the slope faces (downhill). Heights are aligned to the `vyska` layer at the "
        "reference point.")


def coverage_params(bounds: tuple, scale: float | None = None) -> list[tuple[str, str]]:
    minx, miny, maxx, maxy = bounds
    p = [("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"), ("coverageId", COVERAGE),
         ("subsettingCrs", "http://www.opengis.net/def/crs/EPSG/0/4326"),
         ("subset", f"Lat({miny - PAD_DEG},{maxy + PAD_DEG})"),
         ("subset", f"Long({minx - PAD_DEG},{maxx + PAD_DEG})"), ("format", "image/tiff")]
    if scale is not None:
        p.append(("SCALEFACTOR", f"{scale:.3f}"))
    return p


def scale_for(bounds: tuple) -> float | None:
    minx, miny, maxx, maxy = bounds
    lat0 = (miny + maxy) / 2
    w = (maxx - minx + 2 * PAD_DEG) * 111320 * math.cos(math.radians(lat0))
    h = (maxy - miny + 2 * PAD_DEG) * 110540
    px = w * h  # ~1 m cells
    return None if px <= MAX_PX else math.floor(math.sqrt(MAX_PX / px) * 1000) / 1000


def read_geotiff(data: bytes) -> tuple[np.ndarray, tuple]:
    """float32 GeoTIFF -> (heights with NaN for nodata, (lon0, dlon, lat0, dlat) of the pixel corner grid)."""
    try:
        im = Image.open(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001 - PIL raises several kinds
        raise ValueError(f"not an image: {data[:120]!r}") from e
    t = im.tag_v2.get(34264)
    if im.mode != "F" or not t:
        raise ValueError(f"expected a float GeoTIFF with ModelTransformation, got mode {im.mode}")
    a = np.array(im, dtype=float)
    a[~np.isfinite(a) | (a > NODATA_ABOVE)] = np.nan
    return a, (t[3], t[0], t[7], t[5])


def parcel_mask(geom, shape: tuple, grid: tuple) -> np.ndarray:
    lon0, dlon, lat0, dlat = grid
    img = Image.new("L", (shape[1], shape[0]), 0)
    draw = ImageDraw.Draw(img)

    def px(coords):
        return [((x - lon0) / dlon, (y - lat0) / dlat) for x, y in coords]

    for p in getattr(geom, "geoms", [geom]):
        if p.is_empty:
            continue
        draw.polygon(px(p.exterior.coords), fill=1)
        for hole in p.interiors:
            draw.polygon(px(hole.coords), fill=0)
    return np.array(img, dtype=bool)


def value_at(a: np.ndarray, grid: tuple, lat: float, lon: float) -> float | None:
    lon0, dlon, lat0, dlat = grid
    r, c = int((lat - lat0) / dlat), int((lon - lon0) / dlon)
    if 0 <= r < a.shape[0] and 0 <= c < a.shape[1] and np.isfinite(a[r, c]):
        return float(a[r, c])
    return None


def box_mean(a: np.ndarray, k: int) -> np.ndarray:
    """k x k moving average ignoring NaN (summed-area table, O(n))."""
    if k <= 1:
        return a
    p = k // 2
    v = np.where(np.isfinite(a), a, 0.0)
    n = np.isfinite(a).astype(float)

    h, w = a.shape

    def window_sum(x):
        # c[i, j] = sum of padded x[:i, :j]; window of output (r, c) = padded rows r..r+k-1, cols c..c+k-1
        c = np.zeros((h + 2 * p + 1, w + 2 * p + 1))
        c[1:, 1:] = np.pad(x, p, mode="constant").cumsum(0).cumsum(1)
        return c[k:k + h, k:k + w] - c[:h, k:k + w] - c[k:k + h, :w] + c[:h, :w]

    s, cnt = window_sum(v), window_sum(n)
    out = np.full(a.shape, np.nan)
    ok = cnt > 0
    out[ok] = s[ok] / cnt[ok]
    out[~np.isfinite(a)] = np.nan
    return out


def _sector(az: float) -> str:
    return SECTORS[int((az + 22.5) // 45) % 8]


def plane_fit(z: np.ndarray, east: np.ndarray, north: np.ndarray) -> tuple[float, float]:
    """Least squares plane z = be*E + bn*N + c -> (slope %, downhill azimuth °)."""
    A = np.c_[east, north, np.ones(len(z))]
    (be, bn, _), *_ = np.linalg.lstsq(A, z, rcond=None)
    return float(math.hypot(be, bn) * 100), float((math.degrees(math.atan2(-be, -bn)) + 360) % 360)


def slope_stats(a: np.ndarray, grid: tuple, mask: np.ndarray, offset: float | None) -> dict:
    lon0, dlon, lat0, dlat = grid
    lat_c = lat0 + dlat * a.shape[0] / 2
    dx = abs(dlon) * 111320 * math.cos(math.radians(lat_c))
    dy = abs(dlat) * 110540
    k = max(1, round(SMOOTH_M / ((dx + dy) / 2)))
    k += 1 - k % 2  # odd window
    dz_row, dz_col = np.gradient(box_mean(a, k), dy, dx)  # rows run south (dlat < 0)
    dz_north = -dz_row if dlat < 0 else dz_row
    dz_east = dz_col if dlon > 0 else -dz_col
    pct = np.hypot(dz_east, dz_north) * 100
    aspect = (np.degrees(np.arctan2(-dz_east, -dz_north)) + 360) % 360

    sel = mask & np.isfinite(pct)
    n = int(sel.sum())
    if n == 0:
        raise KatasterError("not_found", "DMR has no heights inside the parcel.")
    s, asp, z = pct[sel], aspect[sel], a[sel]
    rows, cols = np.nonzero(sel)
    east = (cols - cols.mean()) * dx * (1 if dlon > 0 else -1)
    north = (rows - rows.mean()) * dy * (-1 if dlat < 0 else 1)
    plane_pct, plane_az = plane_fit(z, east, north) if n >= 3 else (0.0, 0.0)

    def deg(p):
        return round(math.degrees(math.atan(p / 100)), 1)

    sloped = s >= FLAT_PCT
    if sloped.mean() < 0.2:
        expo = {"smer": "rovina", "podiel": round(1 - float(sloped.mean()), 2)}
    else:
        idx = ((asp[sloped] + 22.5) // 45).astype(int) % 8
        counts = np.bincount(idx, minlength=8)
        top = int(counts.argmax())
        expo = {"smer": SECTORS[top], "podiel": round(float(counts[top] / sloped.sum()), 2)}

    shift = offset or 0.0
    out = {
        "sklon_celkovy_pct": round(plane_pct, 1), "sklon_celkovy_st": deg(plane_pct),
        "smer_klesania": {"azimut": round(plane_az), "smer": _sector(plane_az)} if plane_pct >= FLAT_PCT else "rovina",
        "sklon_priemer_pct": round(float(s.mean()), 1), "sklon_priemer_st": deg(float(s.mean())),
        "sklon_max_pct": round(float(s.max()), 1), "sklon_max_st": deg(float(s.max())),
        "sklon_p95_pct": round(float(np.percentile(s, 95)), 1),
        "expozicia": expo,
        "vyska_min_m": round(float(z.min()) - shift, 1), "vyska_max_m": round(float(z.max()) - shift, 1),
        "prevysenie_m": round(float(z.max() - z.min()), 1),
        "body": n, "rozlisenie_m": round((dx + dy) / 2, 2), "vyhladenie_m": round(k * (dx + dy) / 2),
        "poznamka": NOTE,
    }
    if offset is None:
        out["vyskovy_system"] = "not aligned (DMR WMS unavailable): heights as served by the WCS, ~42.6 m above `vyska`"
    else:
        out["posun_wcs_m"] = round(offset, 2)
    if n < FEW_POINTS:
        out["varovanie"] = f"sklon: only {n} grid points (1 m) inside the parcel; values are rough."
    return out


async def sklon(http, t) -> dict:
    if t.geom is None:
        raise KatasterError("invalid_input", "Slope needs a parcel (ku + cislo), not a point.")
    from . import layers  # late: layers imports this module

    bounds = t.geom.bounds
    scale = scale_for(bounds)
    resp = await http.get(URL, coverage_params(bounds, scale))
    try:
        a, grid = read_geotiff(resp.content)
    except ValueError as e:
        raise KatasterError("parser_drift", f"DMR WCS answer changed: {e}") from e
    mask = parcel_mask(t.geom, a.shape, grid)

    offset = None
    wcs_ref = value_at(a, grid, t.lat, t.lon)
    if wcs_ref is not None:
        try:
            wms_ref = (await layers.elevation(http, t))["nadmorska_vyska_m"]
        except KatasterError:
            wms_ref = None
        if wms_ref is not None:
            offset = wcs_ref - wms_ref
    out = slope_stats(a, grid, mask, offset)
    if scale is not None:
        out["zmensene"] = f"large parcel: grid shrunk by SCALEFACTOR {scale}"
    return out
