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

from . import ku as ku_mod
from . import navod
from .cache import (
    DAY,
    O_BINARY,
    PERSONAL_TTL,
    next_day_iso,
    parser_version,
    purge_files,
    today,
    ts_iso,
)
from .envelope import Envelope, KatasterError
from .normalize import parcel_number
from .sources import eskn, eskn_building

OWNERS_TTL = 7 * DAY
IDENTIFY_TTL = 7 * DAY

IDENTIFY_VER = parser_version(*eskn.PARSERS_IDENTIFY)
DETAIL_VER = hashlib.sha256(
    (parser_version(*eskn.PARSERS_DETAIL) + "".join(eskn.PARSER_DETAIL_REGEXES)).encode()
).hexdigest()[:12]

WARN_UNOFFICIAL = ("Owners come from the unofficial ESKN portal page (informative data, not an official "
                   "extract). Administrators, tenants and encumbrances (ťarchy, časť C) are only in the "
                   "LV extract: use kataster_lv.")


WARN_BUILDING = ("A building stands on this parcel (house number {nums}). A building can be registered on a "
                 "different LV than the land under it; this parcel detail does not show the building's LV or owners "
                 "(detail_stavieb=true opens the building detail, one more query per building).")


def detail_warnings(env: Envelope, result: dict) -> None:
    """Warnings derived from a parsed detail; also for cache hits, so they never get lost."""
    if not result.get("vlastnici") and result.get("vlastnici_sekcia") is True:
        env.warn("The owner section of the parcel detail lists no owner; the answer is not cached. "
                 "Check the LV with kataster_lv.")
    if not result.get("vlastnici") and result.get("vlastnici_sekcia") is False:
        env.warn("The parcel detail page has no owner section (sections: "
                 f"{', '.join(result.get('sekcie') or [])}); owners are not known from this source. "
                 "Check the LV with kataster_lv.")
    if result.get("stavby"):
        nums = ", ".join(b.get("supisne_cislo") or b.get("text", "?") for b in result["stavby"])
        env.warn(WARN_BUILDING.format(nums=nums))


BUILDING_VER = hashlib.sha256(
    (parser_version(*eskn_building.PARSERS) + "".join(eskn_building.PARSER_REGEXES)).encode()
).hexdigest()[:12]


def parser_versions() -> dict:
    return {eskn.SOURCE_IDENTIFY: IDENTIFY_VER, eskn.SOURCE_DETAIL: DETAIL_VER, eskn_building.SOURCE: BUILDING_VER}


def save_snapshot(app, source: str, text: str) -> str:
    """Keep the page that broke the parser. May contain personal data -> owner-only on POSIX, deleted after 7 days."""
    d = app.data_dir / "snapshots"
    d.mkdir(mode=0o700, exist_ok=True)
    purge_files(d, PERSONAL_TTL)
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
                        obnovit: bool = False, potvrdene: bool = False, detail_stavieb: bool = False) -> dict:
    """Owners of one parcel with quota, cache and drift handling. Shared by the tool and extensions."""
    from .server import find_parcel

    parcel = await find_parcel(app, env, ku, cislo, register)
    key = parcel["oznacenie"]
    page = None

    hit = None if obnovit else app.osoby.get(eskn.SOURCE_DETAIL, DETAIL_VER, key, OWNERS_TTL)
    if hit is not None:
        data, ts = hit
        env.source(eskn.SOURCE_DETAIL, data["url"], oficialny=False, z_cache=True, cas=ts_iso(ts))
        env.warn(WARN_UNOFFICIAL)
        detail_warnings(env, data["vysledok"])
        result = data["vysledok"]
    else:
        result, page = await _fetch_owners(app, env, parcel, potvrdene)
    if detail_stavieb and result.get("stavby"):
        result = {**result, **await lookup_buildings(app, env, parcel, result["stavby"], page, obnovit, potvrdene)}
    return {**result, "limit": _limit_info(app)}


async def _parcel_id(app, env: Envelope, parcel: dict) -> dict:
    """ESKN identify of the parcel (internal id, administrative units) at the WFS reference point, cached."""
    key, reg = parcel["oznacenie"], parcel["register"]
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
    return ident


async def _fetch_owners(app, env: Envelope, parcel: dict, potvrdene: bool) -> tuple[dict, tuple[str, str]]:
    """Query the parcel detail (counts towards the daily cap); returns (result, (raw page, its URL))."""
    from .server import _with_ku

    key, reg = parcel["oznacenie"], parcel["register"]
    app.http.guard()  # a remembered block: fail before the quota is counted
    check_and_count(app, key, potvrdene)
    ident = await _parcel_id(app, env, parcel)
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
    if result["vlastnici"] or not result.get("vlastnici_sekcia"):
        ts = app.osoby.put(eskn.SOURCE_DETAIL, DETAIL_VER, key, {"url": url, "vysledok": result})
    else:  # owner section without a single owner: answer, but never keep it as a valid result
        ts = time.time()
    env.source(eskn.SOURCE_DETAIL, url, oficialny=False, z_cache=False, cas=ts_iso(ts))
    env.warn(WARN_UNOFFICIAL)
    detail_warnings(env, result)
    return result, (resp.text, str(resp.url))


async def lookup_buildings(app, env: Envelope, parcel: dict, buildings: list, page: tuple[str, str] | None,
                           obnovit: bool, potvrdene: bool) -> dict:
    """Building details (detail_st) for the buildings on this parcel; each one reaching the portal is one query
    towards the daily cap. `page` is the parcel detail just downloaded (reused for the first building)."""
    key, reg = parcel["oznacenie"], parcel["register"]
    out, queries = [], 0
    if len(buildings) > 1:
        env.warn(f"{len(buildings)} buildings on this parcel: each building detail is a separate query "
                 "towards the daily limit.")
    for i, b in enumerate(buildings):
        house = b.get("supisne_cislo")
        bkey = f"{key}|{i}|{house}"
        hit = None if obnovit else app.osoby.get(eskn_building.SOURCE, BUILDING_VER, bkey, OWNERS_TTL)
        if hit is not None:
            data, ts = hit
            env.source(eskn_building.SOURCE, data["url"], oficialny=False, z_cache=True, cas=ts_iso(ts))
            out.append(data["vysledok"])
            continue
        app.http.guard()
        check_and_count(app, f"stavba|{bkey}", potvrdene)
        queries += 1
        if page is None:
            ident = await _parcel_id(app, env, parcel)
            resp = await app.http.get(eskn.DETAIL_URL[reg], {"id": ident["id"]})
            page = (resp.text, str(resp.url))
        text, page_url = page
        try:
            action, fields = eskn_building.postback(text, eskn_building.building_link(text, i, house))
        except ValueError as e:
            snap = save_snapshot(app, eskn.SOURCE_DETAIL, text)
            raise KatasterError("parser_drift", f"Building link on the parcel detail changed: {e}",
                                details={"snapshot": snap}) from e
        resp = await app.http.post(eskn.HOST + action, fields, headers={"Origin": eskn.HOST, "Referer": page_url})
        page = None  # the ViewState is spent; the next building starts from a fresh parcel detail
        try:
            if "/detail_st" not in str(resp.url):
                raise ValueError(f"the portal did not open the building detail ({resp.status_code})")
            detail = eskn_building.parse_building(resp.text)
        except ValueError as e:
            snap = save_snapshot(app, eskn_building.SOURCE, resp.text)
            raise KatasterError("parser_drift", f"ESKN building detail page changed: {e}",
                                details={"snapshot": snap}) from e
        detail = {"supisne_cislo": house, **detail}
        url = str(resp.url).split("?")[0]
        ts = app.osoby.put(eskn_building.SOURCE, BUILDING_VER, bkey, {"url": url, "vysledok": detail})
        env.source(eskn_building.SOURCE, url, oficialny=False, z_cache=False, cas=ts_iso(ts))
        out.append(detail)
    result = {"stavby_detail": out, "stavby_dotazy": queries}
    for d in out:
        if d.get("jednotky"):
            env.warn(f"Building {d.get('supisne_cislo')} (LV {d.get('lv')}) is divided into units (flats); the portal "
                     "lists owners of the units without shares. They are not returned and the server does not "
                     "download the LV of the building; the guide (navod) shows how to open and import it yourself.")
            unit = ku_mod.table()["_by_code"].get(parcel.get("ku_kod"))
            result["navod"] = navod.building_lv(unit, reg, parcel.get("cislo"), d.get("supisne_cislo"), d.get("lv"))
        elif d.get("lv"):
            env.warn(f"Building {d.get('supisne_cislo')} is on LV {d['lv']}; its owners may differ from the land's.")
    return result


def _by_hand(ku: str, cislo: str, register: str) -> list[str]:
    """Guide for the same parcel in the browser (owners via the parcel detail and the LV preview)."""
    try:
        unit = ku_mod.resolve(ku)
    except KatasterError:
        unit = None
    try:
        reg, number = parcel_number(cislo, register)
    except KatasterError:
        reg, number = None, cislo
    return navod.parcel(unit, reg if reg in ("C", "E") else None, number)


def register(mcp, app) -> None:

    @mcp.tool()
    async def kataster_vlastnici(
        ku: Annotated[str, Field(description="Cadastral unit (k.ú.): name or 6-digit code.")],
        cislo: Annotated[str, Field(description="Parcel number, e.g. '503', 'E 503'. Exactly one parcel.")],
        register: Annotated[Literal["auto", "C", "E"], Field(description="Register C or E; 'auto' fails if the number exists in both.")] = "auto",
        obnovit: Annotated[bool, Field(description="Ignore the 7-day cache and ask the portal again (counts towards the daily limit).")] = False,
        potvrdene: Annotated[bool, Field(description="Only when a soft daily limit is configured: true after the user explicitly approved continuing.")] = False,
        detail_stavieb: Annotated[bool, Field(description="Also open the detail of each building on the parcel (LV of the building, kind, parcels under it, owners); each building is one more query towards the daily limit.")] = False,
    ) -> dict:
        """Owners and shares (vlastníci a podiely) of ONE parcel, with the LV number (list vlastníctva).

        Source: the unofficial ESKN portal parcel detail, queried at human pace with a daily limit
        on parcels. Returns LV number, land type (druh pozemku), location in/outside the built-up
        area, legal relation, owners (name, birth date, address, share), buildings on the parcel
        (stavby: house number only; a building may be on another LV than the land) and the data date (stav_k).
        With detail_stavieb=true also stavby_detail: per building its LV, kind, description, C parcels under it
        and owners with shares (a block of flats: unit owners are not returned); stavby_dotazy = queries spent.
        Personal data: use only for the parcel the user asked about; never walk parcels to find a person
        (for a person's parcels use kataster_osoba_navod). On CAPTCHA, a block or the daily limit the error
        carries a guide (navod) for doing the same in the browser.
        """
        env = Envelope()
        try:
            return env.ok(await lookup_owners(app, env, ku, cislo, register, obnovit, potvrdene, detail_stavieb))
        except KatasterError as e:
            if e.kind in ("captcha_required", "blocked", "limit_reached") and not e.navod:
                e.navod = _by_hand(ku, cislo, register)
                if e.kind == "limit_reached" and isinstance(e.details, dict):
                    e.details["kvota_obnovena"] = next_day_iso()
                    e.message += f" The daily limit resets at {e.details['kvota_obnovena']}; until then use the guide (navod)."
            raise env.fail(e) from e
