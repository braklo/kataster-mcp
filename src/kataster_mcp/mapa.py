"""kataster_mapa: PNG / HTML / GeoJSON / KML of parcels. Geometry only, never owners."""

import os
import re
import time
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field
from shapely.ops import unary_union

from . import geo, render
from .cache import restrict
from .envelope import Envelope, KatasterError
from .skupina import MAX_PARCELS, ParcelRef

Format = Literal["png", "html", "geojson", "kml"]


def _slug(text: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "-", text).strip("-")
    return s[:60] or "mapa"


def register(mcp, app) -> None:
    from .server import _with_ku, find_parcel

    @mcp.tool()
    async def kataster_mapa(
        parcely: Annotated[list[ParcelRef], Field(description=f"Parcels to draw (max {MAX_PARCELS}).")],
        format: Annotated[list[Format], Field(description="Output files: png (rendered map), html (interactive Leaflet map), geojson, kml.")] = ["png"],
        podklad: Annotated[Literal["orto", "kn", "orto+kn", "osm"], Field(description="Background: orto (ZBGIS orthophoto), kn (cadastral map), orto+kn, osm (OpenStreetMap).")] = "orto",
        popisky: Annotated[bool, Field(description="Label parcels with register and number.")] = True,
        out_dir: Annotated[str | None, Field(description="Directory for the files; default <data dir>/mapy.")] = None,
        nazov: Annotated[str | None, Field(description="Base file name; default derived from the parcels.")] = None,
    ) -> dict:
        """Draw parcels on a map and save files. Returns the file paths.

        PNG is rendered on the server (background from official WMS, outlines, labels, scale bar,
        attribution). HTML is an interactive Leaflet map with the same background embedded.
        GeoJSON/KML contain only geometry and public attributes (no owners).
        """
        env = Envelope()
        try:
            if not parcely or len(parcely) > MAX_PARCELS:
                raise KatasterError("invalid_input", f"Give 1 to {MAX_PARCELS} parcels.")
            found = [_with_ku(await find_parcel(app, env, r.ku, r.cislo, r.register)) for r in parcely]
            shapes = [geo.parcel_shape(p["geometria"]) for p in found]
            frame = render.Frame(unary_union(shapes).bounds)
            directory = Path(out_dir).expanduser() if out_dir else app.data_dir / "mapy"
            directory.mkdir(parents=True, exist_ok=True)
            base = nazov or (found[0]["ku_kod"] + "_" + "_".join(f"{p['register']}{p['cislo']}" for p in found[:5]))
            base = f"{_slug(base)}_{time.strftime('%Y%m%d-%H%M%S')}"
            fmts = list(dict.fromkeys(format))
            files = {}
            bg = None
            if "png" in fmts or "html" in fmts:
                bg = await render.background(app.http, frame, podklad, env)
            title = ", ".join(f"{p['register']} {p['cislo']}" for p in found[:5]) + f" — {found[0].get('ku_nazov') or found[0]['ku_kod']}"
            for fmt in fmts:
                path = directory / f"{base}.{fmt}"
                if fmt == "png":
                    path.write_bytes(render.render_png(bg, frame, found, shapes, podklad, popisky))
                elif fmt == "html":
                    path.write_text(render.html(found, frame, render.jpeg(bg), title, podklad), encoding="utf-8")
                elif fmt == "geojson":
                    path.write_text(render.geojson(found), encoding="utf-8")
                elif fmt == "kml":
                    path.write_text(render.kml(found, shapes), encoding="utf-8")
                restrict(path, 0o644)
                files[fmt] = str(path)
            return env.ok({
                "subory": files,
                "parcely": [f"{p['register']} {p['cislo']}" for p in found],
                "rozmer_px": [frame.width, frame.height],
                "mierka_m_na_px": round(frame.metres_per_px, 3),
            })
        except KatasterError as e:
            raise env.fail(e) from e
