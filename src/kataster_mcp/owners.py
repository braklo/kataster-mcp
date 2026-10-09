"""kataster_vlastnici: owners of ONE parcel from the unofficial ESKN detail.

Limits: at least 3 s between requests to the portal and a daily cap on parcels. Quota: a parcel
counts once per calendar day when the portal is actually queried;
cache hits do not count, failed attempts do. Owners are cached 7 days in osoby.sqlite (owner-only permissions on POSIX).
"""

import hashlib
import os
import time
from typing import Annotated, Literal

from pydantic import Field

from .cache import O_BINARY, DAY, parser_version, today, ts_iso
from .envelope import Envelope, KatasterError
from .sources import eskn

OWNERS_TTL = 7 * DAY
IDENTIFY_TTL = 7 * DAY

IDENTIFY_VER = parser_version(*eskn.PARSERS_IDENTIFY)
DETAIL_VER = hashlib.sha256(
    (parser_version(*eskn.PARSERS_DETAIL) + "".join(eskn.PARSER_DETAIL_REGEXES)).encode()
).hexdigest()[:12]

WARN_UNOFFICIAL = ("Owners come from the unofficial ESKN portal page (informative data, not an official "
                   "extract). Administrators, tenants and encumbrances (ťarchy, časť C) are only in the "
                   "LV extract: use kataster_lv.")


def parser_versions() -> dict:
    return {eskn.SOURCE_IDENTIFY: IDENTIFY_VER, eskn.SOURCE_DETAIL: DETAIL_VER}


def save_snapshot(app, source: str, text: str) -> str:
    """Keep the page that broke the parser. May contain personal data -> owner-only on POSIX."""
    d = app.data_dir / "snapshots"
    d.mkdir(mode=0o700, exist_ok=True)
    path = d / f"{time.strftime('%Y%m%d-%H%M%S')}_{source}.html"
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | O_BINARY, 0o600)
    with os.fdopen(fd, "w", encoding="utf8", newline="") as f:
        f.write(text)
    return str(path)


def _limit_info(app) -> dict:
    used = app.quota.used()
    hard = app.policy.owners_daily_hard
    info = {"den": today(), "pouzite": used, "limit": hard, "zostava": max(0, hard - used)}
    if app.policy.owners_daily_soft is not None:
        info["upozornenie_pri"] = app.policy.owners_daily_soft
    return info


def check_and_count(app, key: str, confirmed: bool) -> None:
    """Enforce the daily cap before the portal is queried, then count the parcel."""
    if app.quota.counted(key):
        return
    used = app.quota.used()
    policy = app.policy
    if used >= policy.owners_daily_hard:
        raise KatasterError(
            "limit_reached",
            f"Daily limit of {policy.owners_daily_hard} parcels for owner lookups reached ({today()}). "
            "Cached parcels still work; new ones tomorrow.",
            details=_limit_info(app),
        )
    soft = policy.owners_daily_soft
    if soft is not None and used >= soft and not confirmed:
        raise KatasterError(
            "limit_reached",
            f"{used} parcels looked up today; continuing needs the user's explicit approval "
            "(then call again with potvrdene=true).",
            details={**_limit_info(app), "potvrdenie_potrebne": True},
        )
    app.quota.record(key)


async def lookup_owners(app, env: Envelope, ku: str, cislo: str, register: str = "auto",
                        obnovit: bool = False, potvrdene: bool = False) -> dict:
    """Owners of one parcel with quota, cache and drift handling. Shared by the tool and extensions."""
    from .server import _with_ku, find_parcel

    parcel = await find_parcel(app, env, ku, cislo, register)
    key = parcel["oznacenie"]
    reg = parcel["register"]

    if not obnovit:
        hit = app.osoby.get(eskn.SOURCE_DETAIL, DETAIL_VER, key, OWNERS_TTL)
        if hit is not None:
            data, ts = hit
            env.source(eskn.SOURCE_DETAIL, data["url"], oficialny=False, z_cache=True, cas=ts_iso(ts))
            env.warn(WARN_UNOFFICIAL)
            return {**data["vysledok"], "limit": _limit_info(app)}

    check_and_count(app, key, potvrdene)

    ident_hit = app.cache.get(eskn.SOURCE_IDENTIFY, IDENTIFY_VER, key, IDENTIFY_TTL)
    if ident_hit is not None:
        ident, ts = ident_hit
        env.source(eskn.SOURCE_IDENTIFY, eskn.IDENTIFY_URL, oficialny=False, z_cache=True, cas=ts_iso(ts))
    else:
        rp = parcel["referencny_bod"]
        raw = await app.http.get_json(eskn.IDENTIFY_URL, eskn.identify_params(rp["lat"], rp["lon"]))
        ident = eskn.parse_identify_safe(raw, reg, parcel["cislo"])
        if ident["id"] is None:
            raise KatasterError(
                "not_found", f"ESKN identify does not show {reg} {parcel['cislo']} at its reference point.",
                coverage="ESKN identify at the WFS referencePoint",
            )
        ts = app.cache.put(eskn.SOURCE_IDENTIFY, IDENTIFY_VER, key, ident)
        env.source(eskn.SOURCE_IDENTIFY, eskn.IDENTIFY_URL, oficialny=False, z_cache=False, cas=ts_iso(ts))

    resp = await app.http.get(eskn.DETAIL_URL[reg], {"id": ident["id"]})
    try:
        detail = eskn.parse_detail(resp.text)
    except (ValueError, KeyError, IndexError) as e:
        snap = save_snapshot(app, eskn.SOURCE_DETAIL, resp.text)
        raise KatasterError("parser_drift", f"ESKN parcel detail page changed: {e}",
                            details={"snapshot": snap}) from e

    if detail.get("vymera_m2") is not None and detail["vymera_m2"] != parcel["vymera_m2"]:
        env.warn(f"Area differs: ESKN {detail['vymera_m2']} m², WFS {parcel['vymera_m2']} m².")
    p = _with_ku(parcel)
    result = {
        "parcela": {k: p[k] for k in ("register", "cislo", "ku_kod", "ku_nazov", "vymera_m2")},
        "uzemie": ident["uzemie"],
        "chranene_nehnutelnosti": ident["chranene_nehnutelnosti"],
        **detail,
    }
    url = f"{eskn.DETAIL_URL[reg]}?id={ident['id']}"
    ts = app.osoby.put(eskn.SOURCE_DETAIL, DETAIL_VER, key, {"url": url, "vysledok": result})
    env.source(eskn.SOURCE_DETAIL, url, oficialny=False, z_cache=False, cas=ts_iso(ts))
    env.warn(WARN_UNOFFICIAL)
    return {**result, "limit": _limit_info(app)}


def register(mcp, app) -> None:

    @mcp.tool()
    async def kataster_vlastnici(
        ku: Annotated[str, Field(description="Cadastral unit (k.ú.): name or 6-digit code.")],
        cislo: Annotated[str, Field(description="Parcel number, e.g. '503', 'E 503'. Exactly one parcel.")],
        register: Annotated[Literal["auto", "C", "E"], Field(description="Register C or E; 'auto' fails if the number exists in both.")] = "auto",
        obnovit: Annotated[bool, Field(description="Ignore the 7-day cache and ask the portal again (counts towards the daily limit).")] = False,
        potvrdene: Annotated[bool, Field(description="Only when a soft daily limit is configured: true after the user explicitly approved continuing.")] = False,
    ) -> dict:
        """Owners and shares (vlastníci a podiely) of ONE parcel, with the LV number (list vlastníctva).

        Source: the unofficial ESKN portal parcel detail, queried at human pace with a daily limit
        on parcels. Returns LV number, land type (druh pozemku), location in/outside the built-up
        area, legal relation, owners (name, birth date, address, share) and the data date (stav_k).
        Personal data: use only for the parcel the user asked about; never search by person.
        """
        env = Envelope()
        try:
            return env.ok(await lookup_owners(app, env, ku, cislo, register, obnovit, potvrdene))
        except KatasterError as e:
            raise env.fail(e) from e
