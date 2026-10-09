"""Map output: PNG rendered on the server, self-contained-ish HTML (Leaflet), GeoJSON, KML.

Only geometry and public parcel attributes go into maps. Never owners.
"""

import base64
import io
import json
import math
from html import escape
from xml.sax.saxutils import escape as xml_escape

from PIL import Image, ImageDraw, ImageFont

from .envelope import KatasterError

ORTO_URL = "https://zbgisws.skgeodesy.sk/zbgis_ortofoto_wms/service.svc/get"
KN_URL = "https://kataster.skgeodesy.sk/eskn/services/NR/kn_wms_norm/MapServer/WmsServer"
# 5 parcels C, 6 other boundary, 7 number arrows, 8 land-type symbols, 10 parcel numbers C,
# 12 built-up area boundary (ZÚOB), 13 cadastral units. 4 = encumbrances (phase 2).
KN_LAYERS = "5,6,7,8,10,12,13"
OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
COLORS = [(230, 25, 75), (0, 130, 200), (255, 225, 25), (60, 180, 75), (245, 130, 48),
          (145, 30, 180), (70, 240, 240), (240, 50, 230)]
MAX_SIDE = 1400
ATTRIBUTION = {
    "orto": "Ortofoto © ÚGKK SR (ZBGIS)",
    "kn": "Katastrálna mapa © ÚGKK SR",
    "osm": "© OpenStreetMap contributors",
}


class Frame:
    """A lon/lat box mapped to pixels with true proportions at its latitude."""

    def __init__(self, bounds, pad=0.15):
        minx, miny, maxx, maxy = bounds
        lat0 = (miny + maxy) / 2
        self.k = math.cos(math.radians(lat0))
        w, h = (maxx - minx) * self.k, maxy - miny
        side = max(w, h, 0.0003)
        w, h = max(w, side * 0.5), max(h, side * 0.5)
        cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
        w, h = w * (1 + 2 * pad), h * (1 + 2 * pad)
        self.minx, self.maxx = cx - w / 2 / self.k, cx + w / 2 / self.k
        self.miny, self.maxy = cy - h / 2, cy + h / 2
        scale = MAX_SIDE / max(w, h)
        self.width, self.height = round(w * scale), round(h * scale)

    def px(self, lon, lat):
        x = (lon - self.minx) / (self.maxx - self.minx) * self.width
        y = (self.maxy - lat) / (self.maxy - self.miny) * self.height
        return x, y

    @property
    def metres_per_px(self) -> float:
        return (self.maxx - self.minx) * self.k * 111320.0 / self.width

    def wms_params(self, layers: str, fmt: str, transparent=False) -> dict:
        p = {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "LAYERS": layers, "STYLES": "",
             "CRS": "EPSG:4326", "BBOX": f"{self.miny},{self.minx},{self.maxy},{self.maxx}",
             "WIDTH": self.width, "HEIGHT": self.height, "FORMAT": fmt}
        if transparent:
            p["TRANSPARENT"] = "TRUE"
        return p


def _image(resp, what: str) -> Image.Image:
    if not resp.headers.get("content-type", "").startswith("image/"):
        raise KatasterError("unavailable", f"{what}: expected an image, got {resp.headers.get('content-type')}")
    return Image.open(io.BytesIO(resp.content)).convert("RGBA")


async def _osm(http, f: Frame) -> Image.Image:
    def tile_xy(lon, lat, z):
        n = 2 ** z
        x = (lon + 180) / 360 * n
        y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
        return x, y

    z = 18
    while z > 10:
        x0, y0 = tile_xy(f.minx, f.maxy, z)
        x1, y1 = tile_xy(f.maxx, f.miny, z)
        if (int(x1) - int(x0) + 1) * (int(y1) - int(y0) + 1) <= 16:
            break
        z -= 1
    canvas = Image.new("RGBA", ((int(x1) - int(x0) + 1) * 256, (int(y1) - int(y0) + 1) * 256))
    for tx in range(int(x0), int(x1) + 1):
        for ty in range(int(y0), int(y1) + 1):
            resp = await http.get(OSM_TILES.format(z=z, x=tx, y=ty))
            canvas.paste(_image(resp, "OSM tile"), ((tx - int(x0)) * 256, (ty - int(y0)) * 256))
    box = ((x0 - int(x0)) * 256, (y0 - int(y0)) * 256, (x1 - int(x0)) * 256, (y1 - int(y0)) * 256)
    return canvas.crop(tuple(round(v) for v in box)).resize((f.width, f.height), Image.LANCZOS)


async def background(http, f: Frame, podklad: str, env) -> Image.Image:
    base = Image.new("RGBA", (f.width, f.height), (255, 255, 255, 255))
    if podklad in ("orto", "orto+kn"):
        resp = await http.get(ORTO_URL, f.wms_params("1", "image/jpeg"))
        base = _image(resp, "ZBGIS orthophoto")
        env.source("zbgis_ortofoto", ORTO_URL, oficialny=True, z_cache=False)
    if podklad == "osm":
        base = await _osm(http, f)
        env.source("osm_tiles", OSM_TILES, oficialny=False, z_cache=False)
    if podklad in ("kn", "orto+kn"):
        resp = await http.get(KN_URL, f.wms_params(KN_LAYERS, "image/png", transparent=True))
        base = Image.alpha_composite(base, _image(resp, "KN WMS"))
        env.source("ugkk_kn_wms", KN_URL, oficialny=True, z_cache=False)
    return base


def _font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _scale_bar(draw, f: Frame, font):
    target = f.metres_per_px * f.width / 5
    step = 10 ** math.floor(math.log10(target))
    length_m = max(s * step for s in (1, 2, 5) if s * step <= target)
    px = length_m / f.metres_per_px
    x0, y0 = 20, f.height - 30
    draw.rectangle((x0 - 6, y0 - 26, x0 + px + 60, y0 + 12), fill=(255, 255, 255, 200))
    draw.line((x0, y0, x0 + px, y0), fill=(0, 0, 0, 255), width=4)
    for x in (x0, x0 + px):
        draw.line((x, y0 - 8, x, y0 + 4), fill=(0, 0, 0, 255), width=2)
    draw.text((x0, y0 - 24), f"{int(length_m)} m", fill=(0, 0, 0, 255), font=font)


def render_png(bg: Image.Image, f: Frame, parcels: list[dict], shapes: list, podklad: str, popisky: bool) -> bytes:
    overlay = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _font(max(14, f.width // 60))
    for i, (p, s) in enumerate(zip(parcels, shapes)):
        color = COLORS[i % len(COLORS)]
        polys = s.geoms if hasattr(s, "geoms") else [s]
        for poly in polys:
            pts = [f.px(x, y) for x, y in poly.exterior.coords]
            draw.polygon(pts, fill=color + (60,))
            draw.line(pts, fill=color + (255,), width=4, joint="curve")
    if popisky:
        for i, (p, s) in enumerate(zip(parcels, shapes)):
            c = s.representative_point()
            x, y = f.px(c.x, c.y)
            text = f"{p['register']} {p['cislo']}"
            bb = draw.textbbox((x, y), text, font=font, anchor="mm")
            draw.rectangle((bb[0] - 4, bb[1] - 2, bb[2] + 4, bb[3] + 2), fill=(255, 255, 255, 210))
            draw.text((x, y), text, fill=(0, 0, 0, 255), font=font, anchor="mm")
    _scale_bar(draw, f, _font(max(12, f.width // 80)))
    attrib = " · ".join(ATTRIBUTION[k] for k in ("orto", "kn", "osm") if k in podklad)
    small = _font(max(11, f.width // 110))
    bb = draw.textbbox((f.width - 8, f.height - 8), attrib, font=small, anchor="rd")
    draw.rectangle((bb[0] - 4, bb[1] - 2, bb[2] + 4, bb[3] + 2), fill=(255, 255, 255, 200))
    draw.text((f.width - 8, f.height - 8), attrib, fill=(0, 0, 0, 255), font=small, anchor="rd")
    out = Image.alpha_composite(bg, overlay).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=85)
    return buf.getvalue()


def feature(p: dict) -> dict:
    props = {k: p.get(k) for k in ("oznacenie", "register", "cislo", "ku_kod", "ku_nazov", "vymera_m2")}
    return {"type": "Feature", "properties": props, "geometry": p["geometria"]}


def geojson(parcels: list[dict]) -> str:
    return json.dumps({"type": "FeatureCollection", "features": [feature(p) for p in parcels]}, ensure_ascii=False)


def kml(parcels: list[dict], shapes: list) -> str:
    marks = []
    for i, (p, s) in enumerate(zip(parcels, shapes)):
        r, g, b = COLORS[i % len(COLORS)]
        polys = s.geoms if hasattr(s, "geoms") else [s]
        geoms = "".join(
            "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
            + " ".join(f"{x:.8f},{y:.8f}" for x, y in poly.exterior.coords)
            + "</coordinates></LinearRing></outerBoundaryIs></Polygon>" for poly in polys)
        name = xml_escape(f"{p['register']} {p['cislo']} ({p.get('ku_nazov') or p['ku_kod']})")
        marks.append(
            f"<Placemark><name>{name}</name><description>{p['vymera_m2']} m²</description>"
            f"<Style><LineStyle><color>ff{b:02x}{g:02x}{r:02x}</color><width>3</width></LineStyle>"
            f"<PolyStyle><color>40{b:02x}{g:02x}{r:02x}</color></PolyStyle></Style>"
            f"<MultiGeometry>{geoms}</MultiGeometry></Placemark>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            + "".join(marks) + "</Document></kml>\n")


def html(parcels: list[dict], f: Frame, bg_jpeg: bytes | None, title: str, podklad: str) -> str:
    data = json.loads(geojson(parcels))
    for i, feat in enumerate(data["features"]):
        r, g, b = COLORS[i % len(COLORS)]
        feat["properties"]["_color"] = f"#{r:02x}{g:02x}{b:02x}"
    overlay = ""
    if bg_jpeg is not None:
        uri = "data:image/jpeg;base64," + base64.b64encode(bg_jpeg).decode()
        overlay = (f"L.imageOverlay('{uri}', [[{f.miny},{f.minx}],[{f.maxy},{f.maxx}]]).addTo(map);")
    attrib = " · ".join(ATTRIBUTION[k] for k in ("orto", "kn", "osm") if k in podklad)
    return f"""<!doctype html>
<html lang="sk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#map{{height:100%;margin:0}} .lbl{{background:#fff;border:0;font:600 13px sans-serif}}</style>
</head><body><div id="map"></div><script>
const map = L.map('map');
L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom: 20, maxNativeZoom: 19,
  attribution: '© OpenStreetMap contributors'}}).addTo(map);
{overlay}
const data = {json.dumps(data, ensure_ascii=False)};
const layer = L.geoJSON(data, {{
  style: ft => ({{color: ft.properties._color, weight: 3, fillOpacity: 0.2}}),
  onEachFeature: (ft, l) => {{
    const p = ft.properties;
    l.bindTooltip(p.register + ' ' + p.cislo, {{permanent: true, direction: 'center', className: 'lbl'}});
    l.bindPopup('<b>' + p.register + ' ' + p.cislo + '</b><br>' + (p.ku_nazov || p.ku_kod) + '<br>' + p.vymera_m2 + ' m²');
  }}
}}).addTo(map);
map.fitBounds(layer.getBounds(), {{padding: [30, 30]}});
map.attributionControl.addAttribution({json.dumps(attrib, ensure_ascii=False)});
</script></body></html>
"""
