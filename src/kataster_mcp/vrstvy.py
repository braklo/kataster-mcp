"""kataster_vrstvy: soil, land value, LPIS, land user, floods, nature protection, elevation."""

from typing import Annotated, Literal

from pydantic import Field

from . import geo
from .cache import DAY, parser_version, ts_iso
from .envelope import Envelope, KatasterError
from .sources import layers as L

LayerName = Literal["bpej", "hodnota", "lpis", "uzivatel", "zaplavy", "ochrana", "vyska", "zastavane_uzemie", "tarchy",
                    "pozemkove_upravy", "sklon", "les", "siete"]
ALL = list(L.LAYERS)
VERSIONS = {name: parser_version(*spec.parsers) for name, spec in L.LAYERS.items()}


def register(mcp, app) -> None:
    from .server import SK_LAT, SK_LON, _with_ku, find_parcel

    @mcp.tool()
    async def kataster_vrstvy(
        ku: Annotated[str | None, Field(description="Cadastral unit (k.ú.): name or 6-digit code.")] = None,
        cislo: Annotated[str | None, Field(description="Parcel number, e.g. '503', 'E 503'.")] = None,
        register: Annotated[Literal["auto", "C", "E"], Field(description="Register C or E; 'auto' fails if the number exists in both.")] = "auto",
        lat: Annotated[float | None, Field(description="Latitude WGS84, instead of a parcel.")] = None,
        lon: Annotated[float | None, Field(description="Longitude WGS84, instead of a parcel.")] = None,
        vrstvy: Annotated[list[LayerName] | None, Field(description="Layers to return; default all.")] = None,
    ) -> dict:
        """Thematic layers for a parcel (whole polygon, with area shares) or a point.

        - bpej: soil-ecological units (BPEJ) with quality group (skupina kvality), points, protected soil,
          levies for taking farmland (odvod za záber) — VÚPOP
        - hodnota: official land value €/m² for land consolidation (vyhl. 38/2005), not a market price — VÚPOP
        - lpis: farm blocks (diel pôdneho bloku, LPIS) and land use — VÚPOP
        - uzivatel: who farms the land per subsidy applications (name, IČO, year) — NLC
        - zaplavy: flood hazard extent Q10/Q100/Q1000 and depth class — SVP
        - ochrana: Natura 2000 (CHVÚ, ÚEV), protected areas and degree of protection — ŠOP SR
        - vyska: elevation at the reference point (DMR 5.0) — ÚGKK
        - zastavane_uzemie: parcel inside or outside the built-up area of the municipality — cadastral map
          WMS of ÚGKK (register C, official) or ESKN identify (register E, unofficial)
        - tarchy: whether a drawn encumbrance (ťarcha) line crosses the parcel, with internal ids; an unofficial
          estimate from the rendered cadastral map, parcels only (not points); content only in the LV (kataster_lv)
        - pozemkove_upravy: land consolidation projects (in progress / finished, complex / simple) in the parcel's
          cadastral unit, with years and project area — MPRV SR open data; parcels only; says nothing about
          whether the parcel lies inside the project boundary
        - les: forest management units (JPRL: compartment, stand group, forest category hospodársky / ochranný /
          osobitného určenia, forest unit, management plan validity) and forest types, with area shares — NLC;
          no forest owner or manager
        - siete: access and line objects — nearest ZBGIS road (type, surface, distance; adjacent if <= 3 m) and
          roads, railways and power lines (voltage) crossing the parcel or within 50 m, with distance and length
          on the parcel — ZBGIS of GKÚ; parcels only; protection zones are not computed (warning only)
        - sklon: terrain slope of the parcel (mean, max, 95th percentile in % and degrees), prevailing exposure
          (S, SV, V, JV, J, JZ, Z, SZ = N, NE, E, SE, S, SW, W, NW, or rovina = flat), min/max height and
          difference — DMR 5.0 1 m grid of ÚGKK (INSPIRE WCS); parcels only
        A failing layer does not fail the others; it carries its own `chyba`.
        """
        env = Envelope()
        try:
            names = list(dict.fromkeys(vrstvy or ALL))
            if lat is not None or lon is not None:
                if not vrstvy:
                    names = [n for n in names if not L.LAYERS[n].parcel_only]
                if lat is None or lon is None or ku or cislo:
                    raise KatasterError("invalid_input", "Give either ku + cislo, or lat + lon.")
                if not (SK_LAT[0] <= lat <= SK_LAT[1] and SK_LON[0] <= lon <= SK_LON[1]):
                    raise KatasterError("invalid_input", f"Point {lat}, {lon} is outside Slovakia (lat/lon swapped?).")
                target = L.Target(lat=lat, lon=lon)
                key_base = f"pt|{lat:.7f}|{lon:.7f}"
                ciel = {"bod": {"lat": lat, "lon": lon}}
            elif ku and cislo:
                parcel = await find_parcel(app, env, ku, cislo, register)
                rp = parcel["referencny_bod"]
                target = L.Target(lat=rp["lat"], lon=rp["lon"], geom=geo.parcel_shape(parcel["geometria"]),
                                  vymera_m2=parcel["vymera_m2"], register=parcel["register"], cislo=parcel["cislo"],
                                  ku_kod=parcel["ku_kod"])
                key_base = parcel["oznacenie"]
                p = _with_ku(parcel)
                ciel = {"parcela": {k: p[k] for k in ("register", "cislo", "ku_kod", "ku_nazov", "vymera_m2")},
                        "referencny_bod": rp}
            else:
                raise KatasterError("invalid_input", "Give either ku + cislo, or lat + lon.")

            out = {}
            for name in names:
                spec = L.LAYERS[name]
                source_id, url, oficialny = spec.source(target)
                store = app.osoby if spec.personal else app.cache
                key = f"{name}|{key_base}"
                hit = store.get(source_id, VERSIONS[name], key, spec.ttl_days * DAY)
                if hit is not None:
                    out[name], ts = hit
                    env.source(source_id, url, oficialny=oficialny, z_cache=True, cas=ts_iso(ts))
                else:
                    try:
                        if spec.needs_store:
                            data = await spec.fetch(app.http, target, store=app.cache)
                        else:
                            data = await spec.fetch(app.http, target)
                    except KatasterError as e:
                        out[name] = {"chyba": {"typ": e.kind, "sprava": e.message}}
                        env.warn(f"Layer {name} failed: {e.message}")
                        continue
                    ts = store.put(source_id, VERSIONS[name], key, data)
                    env.source(source_id, url, oficialny=oficialny, z_cache=False, cas=ts_iso(ts))
                    out[name] = data
                text = spec.warn(out[name]) if spec.warn else None
                if text:
                    env.warn(text)
            if target.geom is None:
                env.warn("Point query: shares (podiel) are not computed; give a parcel for whole-area results.")
            return env.ok({**ciel, "vrstvy": out})
        except KatasterError as e:
            raise env.fail(e) from e
