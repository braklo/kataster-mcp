"""MCP server entry point.

`create_server(policy, extra_tools)` is the extension point for extra tools.
The public entry point `main()` always uses PUBLIC_POLICY.
"""

import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import __version__, ku as ku_mod
from .cache import DAY, Cache, Quota, default_data_dir, parser_version, protection, today, ts_iso
from .envelope import Envelope, KatasterError
from .http import Http
from .normalize import parcel_number
from .policy import PUBLIC_POLICY, Policy
from .sources import wfs
from . import lv, mapa, owners, skupina, vrstvy

INSTRUCTIONS = """\
Slovak land registry (kataster nehnuteľností SR). Look up a cadastral unit (k.ú., katastrálne územie)
with kataster_ku, then a parcel (parcela, register C or E) with kataster_parcela.
Every answer carries its sources (zdroje) with time and whether it came from the local cache.
Data are informative, not an official extract (výpis z LV)."""

GEOMETRY_TTL = 30 * DAY
NEGATIVE_TTL = 1 * DAY

# Rough bounding box of Slovakia, to catch swapped lat/lon.
SK_LAT = (47.6, 49.7)
SK_LON = (16.8, 22.6)


@dataclass
class App:
    policy: Policy
    http: Http
    cache: Cache
    osoby: Cache
    quota: Quota
    data_dir: Path


ExtraTool = Callable[[FastMCP, App], None]


def create_server(policy: Policy = PUBLIC_POLICY, extra_tools: Iterable[ExtraTool] = (),
                  *, data_dir: Path | None = None, http: Http | None = None) -> FastMCP:
    data_dir = data_dir or default_data_dir()
    osoby = Cache(data_dir / "osoby.sqlite", private=True)
    app = App(
        policy=policy,
        http=http or Http(policy),
        cache=Cache(data_dir / "cache.sqlite", private=False),
        osoby=osoby,
        quota=Quota(osoby.db),
        data_dir=data_dir,
    )
    mcp = FastMCP("kataster", instructions=INSTRUCTIONS)
    _register_core(mcp, app)
    for register in extra_tools:
        register(mcp, app)
    mcp.app_state = app  # for tests and extensions
    return mcp


WFS_VER = parser_version(*wfs.PARSERS)


def _strip(parcel: dict, with_geometry: bool) -> dict:
    out = dict(parcel)
    if not with_geometry:
        out.pop("geometria", None)
    return out


async def _wfs_cached(app: App, env: Envelope, key: str, register: str, fetch) -> list[dict]:
    hit = app.cache.get(wfs.SOURCE_ID, WFS_VER, key, None)
    if hit is not None:
        data, ts = hit
        ttl = GEOMETRY_TTL if data else NEGATIVE_TTL
        if ts + ttl > time.time():
            env.source(wfs.SOURCE_ID, wfs.url_for(register), oficialny=True, z_cache=True, cas=ts_iso(ts))
            return data
    data = await fetch()
    ts = app.cache.put(wfs.SOURCE_ID, WFS_VER, key, data)
    env.source(wfs.SOURCE_ID, wfs.url_for(register), oficialny=True, z_cache=False, cas=ts_iso(ts))
    return data


async def find_parcel(app: App, env: Envelope, ku: str, cislo: str, register: str) -> dict:
    """Resolve ku + parcel number to exactly one WFS parcel (with geometry), or raise."""
    unit = ku_mod.resolve(ku)
    reg, number = parcel_number(cislo, register)
    regs = ["C", "E"] if reg == "auto" else [reg]
    found = []
    for r in regs:
        key = f"no|{unit['kod']}|{r}|{number}"
        found += await _wfs_cached(app, env, key, r, lambda r=r: wfs.by_number(app.http, unit["kod"], number, r))
    if not found:
        raise KatasterError(
            "not_found", f"Parcel {number} not found in k.ú. {unit['nazov']} ({unit['kod']}).",
            coverage=f"INSPIRE WFS, registers {', '.join(regs)}",
        )
    if len(found) > 1:
        raise KatasterError(
            "ambiguous", f"Parcel {number} exists in both registers C and E; say which one.",
            details=[_with_ku(_strip(p, False)) for p in found],
        )
    return found[0]


def _register_core(mcp: FastMCP, app: App) -> None:
    owners.register(mcp, app)
    vrstvy.register(mcp, app)
    skupina.register(mcp, app)
    mapa.register(mcp, app)
    lv.register(mcp, app)

    @mcp.tool()
    async def kataster_ku(
        query: Annotated[str, Field(description="Name of the cadastral unit (k.ú.), diacritics optional, or its 6-digit code.")],
    ) -> dict:
        """Find a cadastral unit (katastrálne územie, k.ú.) by name or code.

        Returns [{kod, nazov, obec, obec_kod, okres, lat, lon}]; lat/lon is a point inside the unit.
        Municipality (obec) and district (okres) tell apart units with the same name (128 names repeat).
        Works offline from a bundled list.
        """
        env = Envelope()
        try:
            hits = ku_mod.search(query)
            t = ku_mod.table()
            env.source("ugkk_inspire_wfs_ku", t["zdroj"], oficialny=True, z_cache=True, cas=t["stiahnute"])
            if not hits:
                raise KatasterError("not_found", f"No cadastral unit matches '{query}'.",
                                    coverage=f"bundled list of {t['pocet']} k.ú.")
            if len(hits) > ku_mod.MAX_RESULTS:
                env.warn(f"{len(hits)} matches, showing first {ku_mod.MAX_RESULTS}; refine the query.")
            return env.ok(hits[: ku_mod.MAX_RESULTS])
        except KatasterError as e:
            raise env.fail(e) from e

    @mcp.tool()
    async def kataster_parcela(
        ku: Annotated[str | None, Field(description="Cadastral unit (k.ú.): name or 6-digit code.")] = None,
        cislo: Annotated[str | None, Field(description="Parcel number, e.g. '503', '660/66', 'E 503'.")] = None,
        register: Annotated[Literal["auto", "C", "E"], Field(description="Register: C (parcely registra C, current plots) or E (registra E, original ownership plots). 'auto' tries both.")] = "auto",
        lat: Annotated[float | None, Field(description="Latitude WGS84, instead of ku+cislo.")] = None,
        lon: Annotated[float | None, Field(description="Longitude WGS84, instead of ku+cislo.")] = None,
        geometria: Annotated[bool, Field(description="Include the GeoJSON polygon.")] = False,
    ) -> dict:
        """Parcel (parcela) geometry from the official INSPIRE WFS of ÚGKK SR.

        Give either ku + cislo, or lat + lon (returns every C and E parcel at that point).
        Returns register, number, area in m² (výmera), reference point (referencny_bod, a point
        guaranteed inside the parcel), bbox and optionally the polygon. No owners here.
        """
        env = Envelope()
        try:
            if lat is not None or lon is not None:
                if lat is None or lon is None or ku or cislo:
                    raise KatasterError("invalid_input", "Give either ku + cislo, or lat + lon.")
                if not (SK_LAT[0] <= lat <= SK_LAT[1] and SK_LON[0] <= lon <= SK_LON[1]):
                    raise KatasterError("invalid_input", f"Point {lat}, {lon} is outside Slovakia (lat/lon swapped?).")
                regs = ["C", "E"] if register == "auto" else [register]
                found = []
                for reg in regs:
                    key = f"pt|{reg}|{lat:.7f}|{lon:.7f}"
                    found += await _wfs_cached(app, env, key, reg, lambda reg=reg: wfs.at_point(app.http, lat, lon, reg))
                if not found:
                    raise KatasterError("not_found", "No parcel at this point.",
                                        coverage=f"INSPIRE WFS, registers {', '.join(regs)}")
                return env.ok([_with_ku(_strip(p, geometria)) for p in found])

            if not ku or not cislo:
                raise KatasterError("invalid_input", "Give either ku + cislo, or lat + lon.")
            parcel = await find_parcel(app, env, ku, cislo, register)
            return env.ok(_with_ku(_strip(parcel, geometria)))
        except KatasterError as e:
            raise env.fail(e) from e

    @mcp.tool()
    async def kataster_status() -> dict:
        """Server status: access policy, today's owners quota, cache contents and parser versions."""
        env = Envelope()
        t = ku_mod.table()
        used = app.quota.used()
        return env.ok({
            "verzia": __version__,
            "politika": asdict(app.policy),
            "kvota_vlastnici": {"den": today(), "pouzite": used, "limit": app.policy.owners_daily_hard,
                                "zostava": max(0, app.policy.owners_daily_hard - used)},
            "cache": app.cache.stats(),
            "ciselnik_ku": {"pocet": t["pocet"], "stiahnute": t["stiahnute"]},
            "verzie_parserov": {wfs.SOURCE_ID: WFS_VER, **owners.parser_versions(), **vrstvy.VERSIONS},
            "data_dir": str(app.data_dir.resolve()),
            "platforma": sys.platform,
            "ochrana_suborov": protection(),
        })


def _with_ku(p: dict) -> dict:
    unit = ku_mod.table()["_by_code"].get(p["ku_kod"])
    return {**p, "ku_nazov": unit["nazov"] if unit else None}


def main() -> None:
    create_server(PUBLIC_POLICY).run()


if __name__ == "__main__":
    main()
