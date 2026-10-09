"""Sonda GetFeatureInfo nad KN WMS kn_wms_norm (fáza 2, bod 1).

Ľudským tempom: pauza >= 3 s medzi dotazmi na kataster.skgeodesy.sk. Surové odpovede ukladá do
adresára z argumentu --out; na výstup vypíše len mená polí a TVAR hodnôt (písmená -> a, číslice -> 9),
nikdy hodnoty samotné, kým nie je jasné, že vrstvy nenesú osobné údaje.

Použitie:
  uv run python scripts/probe_kn_wms_gfi.py --out DIR gfi LAT LON LAYERS [--fmt FMT] [--half M]
  uv run python scripts/probe_kn_wms_gfi.py --out DIR map LAT LON LAYERS --half M [--px N]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

URL = "https://kataster.skgeodesy.sk/eskn/services/NR/kn_wms_norm/MapServer/WmsServer"
UA = "kataster-mcp/0.1 (sonda GFI)"
PAUSE_S = 3.5
_STAMP = Path.home() / ".cache" / "kataster-mcp-probe-last"


def _throttle() -> None:
    try:
        last = float(_STAMP.read_text())
    except (OSError, ValueError):
        last = 0.0
    wait = PAUSE_S - (time.time() - last)
    if wait > 0:
        time.sleep(wait)
    _STAMP.parent.mkdir(parents=True, exist_ok=True)
    _STAMP.write_text(str(time.time()))


def _get(params: dict) -> tuple[int, str, bytes]:
    _throttle()
    url = URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def _merc(lat: float, lon: float) -> tuple[float, float]:
    x = lon * 20037508.342789244 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) * 6378137.0
    return x, y


def _bbox(lat: float, lon: float, half: float) -> str:
    x, y = _merc(lat, lon)
    return f"{x - half},{y - half},{x + half},{y + half}"


def shape(v: object) -> str:
    s = str(v)
    s = re.sub(r"[^\W\d_]", "a", s)
    return re.sub(r"\d", "9", s)


def gfi(out: Path, lat: float, lon: float, layers: str, fmt: str, half: float, px: int = 101) -> None:
    params = {
        "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetFeatureInfo",
        "LAYERS": layers, "QUERY_LAYERS": layers, "STYLES": "",
        "CRS": "EPSG:3857", "BBOX": _bbox(lat, lon, half),
        "WIDTH": px, "HEIGHT": px, "I": px // 2, "J": px // 2,
        "INFO_FORMAT": fmt, "FEATURE_COUNT": 50,
    }
    status, ctype, body = _get(params)
    tag = f"gfi_{lat:.6f}_{lon:.6f}_{layers.replace(',', '-')}_{fmt.replace('/', '_')}_{half:g}"
    (out / tag).write_bytes(body)
    print(f"HTTP {status} {ctype} {len(body)} B -> {tag}")
    if "json" in fmt:
        try:
            data = json.loads(body)
        except ValueError:
            print("  nie je JSON:", shape(body[:200].decode("utf-8", "replace")))
            return
        feats = data.get("features", [])
        print(f"  features: {len(feats)}")
        for f in feats:
            props = f.get("properties", {})
            print("  layer?", f.get("layerName") or f.get("id"), "geom:", (f.get("geometry") or {}).get("type"))
            for k, v in props.items():
                print(f"    {k} = {shape(v)}")
    else:
        txt = body.decode("utf-8", "replace")
        print("  " + shape(re.sub(r"\s+", " ", txt))[:1500])


def wms_map(out: Path, lat: float, lon: float, layers: str, half: float, px: int) -> None:
    params = {
        "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
        "LAYERS": layers, "STYLES": "", "CRS": "EPSG:3857", "BBOX": _bbox(lat, lon, half),
        "WIDTH": px, "HEIGHT": px, "FORMAT": "image/png", "TRANSPARENT": "TRUE",
    }
    status, ctype, body = _get(params)
    tag = f"map_{lat:.6f}_{lon:.6f}_{layers.replace(',', '-')}_{half:g}_{px}.png"
    (out / tag).write_bytes(body)
    print(f"HTTP {status} {ctype} {len(body)} B -> {tag}")
    if not ctype.startswith("image/png"):
        return
    try:
        from PIL import Image
    except ImportError:
        return
    import io

    im = Image.open(io.BytesIO(body)).convert("RGBA")
    w, h = im.size
    x0, y0 = _merc(lat, lon)
    hits = [(i, j) for j in range(h) for i in range(w) if im.getpixel((i, j))[3] > 0]
    print(f"  nepriehľadných pixelov: {len(hits)} z {w * h}")
    for i, j in hits[:: max(1, len(hits) // 8)][:8]:
        x = x0 - half + (i + 0.5) * 2 * half / w
        y = y0 + half - (j + 0.5) * 2 * half / h
        la = math.degrees(2 * math.atan(math.exp(y / 6378137.0)) - math.pi / 2)
        lo = x * 180 / 20037508.342789244
        print(f"  pixel ({i},{j}) -> lat {la:.7f} lon {lo:.7f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("cmd", choices=["gfi", "map"])
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("layers")
    ap.add_argument("--fmt", default="application/geo+json")
    ap.add_argument("--half", type=float, default=10.0)
    ap.add_argument("--px", type=int, default=256)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if a.cmd == "gfi":
        gfi(a.out, a.lat, a.lon, a.layers, a.fmt, a.half, a.px)
    else:
        wms_map(a.out, a.lat, a.lon, a.layers, a.half, a.px)


if __name__ == "__main__":
    main()
