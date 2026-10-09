"""Small geometry helpers (WGS84 in, shares of parcel area out)."""

import math

import numpy as np
import shapely
from shapely.geometry import LinearRing, MultiPolygon, Polygon, shape
from shapely.ops import unary_union

MAX_QUERY_VERTICES = 120


def parcel_shape(geojson: dict):
    return shape(geojson)


def esri_rings_to_shape(rings: list) -> MultiPolygon | Polygon:
    """ESRI polygon rings: clockwise = outer, counter-clockwise = hole."""
    outers, holes = [], []
    for r in rings:
        if len(r) < 4:
            continue
        (holes if LinearRing(r).is_ccw else outers).append(r)
    polys = [shapely.make_valid(Polygon(o)) for o in outers]
    out = unary_union(polys) if polys else Polygon()
    for h in holes:
        out = out.difference(shapely.make_valid(Polygon(h)))
    return _polygonal(shapely.make_valid(out))


def _polygonal(g):
    """Keep only polygon parts (make_valid may add lines or points)."""
    if g.geom_type in ("Polygon", "MultiPolygon"):
        return g
    parts = [p for p in getattr(g, "geoms", []) if p.geom_type in ("Polygon", "MultiPolygon")]
    return unary_union(parts) if parts else Polygon()


def _local_metric(geom, lat0: float):
    k = math.cos(math.radians(lat0))
    scale = np.array([111320.0 * k, 110540.0])
    return shapely.transform(geom, lambda xy: xy * scale)


def share(part, whole) -> float:
    """Fraction of `whole` covered by `part` (both WGS84), using a local metric approximation."""
    if whole.is_empty:
        return 0.0
    lat0 = whole.centroid.y
    w = _local_metric(whole, lat0)
    inter = _local_metric(part.intersection(whole), lat0)
    return min(1.0, inter.area / w.area) if w.area else 0.0


def query_polygon(geom):
    """Simplified polygon for URL-based queries (keeps the shape within ~0.5 m)."""
    g = geom
    tol = 0.000005
    while _vertex_count(g) > MAX_QUERY_VERTICES and tol < 0.001:
        g = geom.simplify(tol, preserve_topology=True)
        tol *= 2
    return g


def _vertex_count(g) -> int:
    polys = g.geoms if hasattr(g, "geoms") else [g]
    return sum(len(p.exterior.coords) + sum(len(i.coords) for i in p.interiors) for p in polys)


def esri_rings(geom) -> list:
    """Shapely polygon -> ESRI rings (outer clockwise, holes counter-clockwise)."""
    polys = geom.geoms if hasattr(geom, "geoms") else [geom]
    rings = []
    for p in polys:
        ext = list(p.exterior.coords)
        rings.append(ext if not LinearRing(ext).is_ccw else ext[::-1])
        for i in p.interiors:
            c = list(i.coords)
            rings.append(c if LinearRing(c).is_ccw else c[::-1])
    return rings


def ewkt(geom) -> str:
    """Shapely polygon -> EWKT in lon/lat for GeoServer CQL."""
    return "SRID=4326;" + geom.wkt


def to_metric(geom, lat0: float):
    return _local_metric(geom, lat0)

