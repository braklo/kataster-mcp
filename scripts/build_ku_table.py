"""Build the bundled list of cadastral units (k.ú.) from the official INSPIRE WFS.

The WFS cannot return CadastralZoning without polygons (propertyName is rejected in
2.0.0 and ignored in 1.1.0), so the full list is ~90 MB. We download it once, page by
page and slowly, and keep only code, name, a point inside the unit, and the municipality
and district it overlaps most (boundaries from ZBGIS via the SVP map service, layers 77/79;
the INSPIRE WFS has no administrative units).

    uv run python scripts/build_ku_table.py
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from shapely.geometry import shape
from shapely.strtree import STRtree

from kataster_mcp.geo import esri_rings_to_shape

ADMIN = "https://mpt.svp.sk/gisserver/rest/services/OP_MPOMPR_II/MPO/MapServer"

URL = "https://inspirews.skgeodesy.sk/geoserver/cp/ows"
PAGE = 100
OUT = Path(__file__).resolve().parents[1] / "src" / "kataster_mcp" / "data" / "ku.json"


def admin_units(c: httpx.Client, layer: int, code_field: str, name_field: str) -> list[tuple]:
    out, offset = [], 0
    while True:
        r = c.get(f"{ADMIN}/{layer}/query", params={
            "f": "json", "where": "1=1", "outFields": f"{code_field},{name_field}", "returnGeometry": "true",
            "outSR": 4326, "maxAllowableOffset": 0.0003, "resultRecordCount": 500, "resultOffset": offset,
            "orderByFields": "OBJECTID"})
        r.raise_for_status()
        d = r.json()
        for f in d["features"]:
            a = f["attributes"]
            out.append((str(a[code_field]), a[name_field], esri_rings_to_shape(f["geometry"]["rings"])))
        print(f"layer {layer}: {len(out)}", file=sys.stderr, flush=True)
        if not d.get("exceededTransferLimit"):
            return out
        offset += len(d["features"])
        time.sleep(1.5)


def best(geom, units, tree):
    cands = [units[i] for i in tree.query(geom)]
    if not cands:
        return None
    def overlap(u):
        try:
            return u[2].intersection(geom).area
        except Exception:  # GEOS topology error on a generalised boundary
            return u[2].buffer(0).intersection(geom.buffer(0)).area
    return max(cands, key=overlap)


def main() -> int:
    rows: dict[str, list] = {}
    total = None
    start = 0
    with httpx.Client(timeout=180, headers={"User-Agent": "kataster-mcp/build-ku"}) as c:
        districts = admin_units(c, 77, "IDN3", "NM3")
        munis = admin_units(c, 79, "IDN4", "NM4")
        d_tree = STRtree([u[2] for u in districts])
        m_tree = STRtree([u[2] for u in munis])
        while total is None or start < total:
            params = {
                "service": "WFS", "version": "2.0.0", "request": "GetFeature",
                "typeNames": "cp:CP.CadastralZoning", "outputFormat": "application/json",
                "count": PAGE, "startIndex": start, "sortBy": "nationalCadastalZoningReference",
            }
            r = c.get(URL, params=params)
            r.raise_for_status()
            d = r.json()
            total = d["numberMatched"]
            for f in d["features"]:
                p = f["properties"]
                g = shape(f["geometry"]).buffer(0)
                pt = g.representative_point()
                muni = best(g, munis, m_tree)
                dist = best(muni[2] if muni else g, districts, d_tree)
                rows[p["nationalCadastalZoningReference"]] = [
                    p["nationalCadastalZoningReference"], p["label"],
                    muni[1] if muni else None, muni[0] if muni else None, dist[1] if dist else None,
                    round(pt.y, 5), round(pt.x, 5)]
            print(f"{start + len(d['features'])}/{total}", file=sys.stderr, flush=True)
            if not d["features"]:
                break
            start += PAGE
            time.sleep(1.5)
    if len(rows) != total:
        print(f"ERROR: got {len(rows)} unique k.ú., expected {total}", file=sys.stderr)
        return 1
    out = {
        "zdroj": URL + " (cp:CP.CadastralZoning)",
        "stiahnute": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pocet": len(rows),
        "zdroj_obce_okresy": ADMIN + " (layers 77, 79; ZBGIS)",
        "stlpce": ["kod", "nazov", "obec", "obec_kod", "okres", "lat", "lon"],
        "ku": sorted(rows.values()),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(rows)} k.ú.)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
