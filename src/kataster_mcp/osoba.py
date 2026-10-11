"""Parcels of a person: the server never searches by person itself.

kataster_osoba_navod guides the user through the official ESKN search by owner (in the user's browser);
the saved LV previews are read by kataster_lv_import; kataster_osoba_prehlad builds an overview from the
imported LVs only. None of the three makes a network request.
"""

import json
import re
import time
import unicodedata
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import Field

from . import ku as ku_mod
from . import navod
from .cache import PERSONAL_TTL, ts_iso
from .envelope import Envelope, KatasterError
from .lv import IMPORT, LV_VER, ku_code

NOT_SUPIS = ("This overview is assembled only from the LVs you imported. It is not an LV extract and not the "
             "list of a person's property (súpis nehnuteľností) that the cadastral office issues; LVs you did not "
             "import are missing.")
_DATE = re.compile(r"^\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\s*$|^\s*(\d{4})-(\d{2})-(\d{2})\s*$")


def tokens(text: str) -> list[str]:
    """Words without diacritics and case: 'NOVÁKOVÁ Ján' -> ['novakova', 'jan']."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+", plain.casefold())


def norm_date(text: str | None) -> str | None:
    """'1.2.1950', '01.02.1950' or '1950-02-01' -> '01.02.1950'."""
    if text is None or not text.strip():
        return None
    m = _DATE.match(text)
    if m is None:
        raise KatasterError("invalid_input", f"Date of birth must be DD.MM.YYYY, not '{text}'.")
    d, mo, y = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(6), m.group(5), m.group(4))
    return f"{int(d):02d}.{int(mo):02d}.{y}"


def share(text: str | None) -> Fraction | None:
    m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", text or "")
    return Fraction(int(m.group(1)), int(m.group(2))) if m and int(m.group(2)) else None


def fits(owner: dict, want: list[str], ico: str | None) -> bool:
    """Every query word is in the owner's name (any order; the maiden name after 'r.' counts), or the IČO matches."""
    if ico:
        return re.sub(r"\D", "", owner.get("ico") or "") == re.sub(r"\D", "", ico)
    return bool(want) and set(want) <= set(tokens(owner.get("meno") or ""))


def imported(app) -> list[tuple[dict, float]]:
    """Imported LVs still valid (the personal-data cache keeps them 7 days)."""
    rows = app.osoby.db.execute("SELECT data, ulozene FROM cache WHERE zdroj=? AND ver=? AND ulozene >= ?",
                                (IMPORT, LV_VER, time.time() - PERSONAL_TTL)).fetchall()
    return [(json.loads(d), ts) for d, ts in rows]


def overview(app, want: list[str], date: str | None, ku_kod: str | None, ico: str | None) -> dict:
    people: dict[tuple, dict] = {}
    unclear: list[dict] = []
    stamps = []
    for row, ts in imported(app):
        data = row["lv"]
        head = data["hlavicka"]
        ku_head = head.get("Katastrálne územie", "")
        code = ku_code(ku_head)
        if ku_kod and code != ku_kod:
            continue
        stamps.append(ts)
        owners = (data.get("cast_b") or {}).get("vlastnici") or []
        mine = [o for o in owners if fits(o, want, ico)]
        if not mine:
            continue
        parcels = [{k: p.get(k) for k in ("register", "cislo", "vymera_m2", "druh_pozemku")} for p in data.get("cast_a") or []]
        area = sum(p["vymera_m2"] or 0 for p in parcels)
        buildings = [{k: b.get(k) for k in ("supisne_cislo", "parcela", "druh_stavby", "popis_stavby", "lv_pozemku")}
                     for b in data.get("stavby") or []]
        for o in mine:
            if date and o.get("datum_narodenia") not in (None, date):
                continue
            entry = {
                "ku_kod": code, "ku": ku_head, "lv": data["lv"], "poradie": o.get("poradie"), "podiel": o.get("podiel"),
                "bsm": bool(o.get("bsm")),
                "parcely": parcels, "stavby": buildings, "vymera_lv_m2": area if parcels else None,
                "vymera_podielom_m2": round(float(area * share(o.get("podiel"))), 1)
                if parcels and share(o.get("podiel")) else None,
                "ostatni_spoluvlastnici": len(owners) - 1, "udaje_platne_k": head.get("Údaje platné k"),
                "importovane": ts_iso(ts), "platne_do": ts_iso(ts + PERSONAL_TTL),
            }
            if date and o.get("datum_narodenia") is None and not ico:
                unclear.append({"meno": o.get("meno"), "adresa": o.get("adresa"), **entry})
                continue
            who = (o.get("ico") or "", " ".join(sorted(tokens(o.get("meno") or ""))), o.get("datum_narodenia") or "")
            person = people.setdefault(who, {"meno": o.get("meno"), "datum_narodenia": o.get("datum_narodenia"),
                                             "ico": o.get("ico"), "adresy": [], "lv": []})
            if o.get("adresa") and o["adresa"] not in person["adresy"]:
                person["adresy"].append(o["adresa"])
            person["lv"].append(entry)
    for p in people.values():
        p["lv"].sort(key=lambda e: (e["ku_kod"], int(e["lv"]) if str(e["lv"]).isdigit() else 0))
        p["pocet_lv"] = len(p["lv"])
        p["pocet_parciel"] = sum(len(e["parcely"]) for e in p["lv"])
        p["pocet_stavieb"] = sum(len(e["stavby"]) for e in p["lv"])
        p["vymera_podielom_spolu_m2"] = round(sum(e["vymera_podielom_m2"] or 0 for e in p["lv"]), 1)
    return {
        "osoby": sorted(people.values(), key=lambda p: (p["datum_narodenia"] or "", p["meno"] or "")),
        "nejednoznacne": unclear,
        "importy": {"pocet_lv": len(stamps), "najstarsi": ts_iso(min(stamps)) if stamps else None,
                    "najnovsi": ts_iso(max(stamps)) if stamps else None,
                    "platne_do": ts_iso(min(stamps) + PERSONAL_TTL) if stamps else None},
    }


def register(mcp, app) -> None:

    @mcp.tool()
    async def kataster_osoba_navod(
        ku: Annotated[str, Field(description="Cadastral unit (k.ú.): name or 6-digit code.")],
        priezvisko: Annotated[str | None, Field(description="Surname (natural person).")] = None,
        meno: Annotated[str | None, Field(description="First name (natural person).")] = None,
        typ: Annotated[Literal["fyzicka", "pravnicka"], Field(description="Natural person (fyzická) or legal person (právnická).")] = "fyzicka",
        nazov: Annotated[str | None, Field(description="Name of the legal person.")] = None,
        ico: Annotated[str | None, Field(description="IČO of the legal person.")] = None,
    ) -> dict:
        """Guide (navod) for finding the parcels of a person in ONE k.ú. in the official ESKN search, by the user.

        The server never searches by person and makes no network request here: the user searches in the
        browser (the portal's invisible CAPTCHA lets a person through), opens the LV previews, saves them into
        one folder, then kataster_lv_import reads the folder and kataster_osoba_prehlad builds the overview.
        Walk the user through the steps ONE AT A TIME (wait until a step is done), do not paste the whole list.
        """
        env = Envelope()
        try:
            unit = ku_mod.resolve(ku)
            env.source("eskn_vyhladavanie", navod.SEARCH_URL, oficialny=True, z_cache=False)
            return env.ok({
                "ku": unit, "typ": typ,
                "navod": navod.person(unit, typ, priezvisko, meno, nazov, ico),
                "poznamka": "The search result lists entries person × LV; namesakes are told apart by date of birth "
                            "and address (natural persons) or IČO (legal persons). CAPTCHA and any login are "
                            "handled by the user; this server does not call the portal for this.",
            })
        except KatasterError as e:
            raise env.fail(e) from e

    @mcp.tool()
    async def kataster_osoba_prehlad(
        priezvisko: Annotated[str | None, Field(description="Surname (natural person).")] = None,
        meno: Annotated[str | None, Field(description="First name (natural person).")] = None,
        datum_narodenia: Annotated[str | None, Field(description="Date of birth DD.MM.YYYY; without it namesakes are listed separately.")] = None,
        ku: Annotated[str | None, Field(description="Only this cadastral unit (name or code).")] = None,
        nazov: Annotated[str | None, Field(description="Legal person: name.")] = None,
        ico: Annotated[str | None, Field(description="Legal person: IČO (takes precedence over the name).")] = None,
    ) -> dict:
        """Overview of a person's parcels from LVs imported with kataster_lv_import (no network request).

        Per LV: k.ú., parcels (register, number, area, land type), buildings (súpisné číslo, the parcel under
        it, kind; lv_pozemku = the LV of the land when it is elsewhere), the person's share, the area by share,
        how many other co-owners (count only); bsm = joint property of spouses (the share belongs to both).
        Totals per person. Without a date of birth, people with the same name are listed separately. Imports are
        kept 7 days; when LVs are missing or expired, call kataster_lv_import again on the same folder. Not an LV extract and not the office's súpis nehnuteľností.
        """
        env = Envelope()
        try:
            want = tokens(" ".join(x for x in (priezvisko, meno, nazov) if x))
            if not ico and len(want) < (1 if nazov else 2):
                raise KatasterError("invalid_input", "Give the surname and first name, or the legal person's name or IČO.")
            date = norm_date(datum_narodenia)
            ku_kod = ku_mod.resolve(ku)["kod"] if ku else None
            out = overview(app, want, date, ku_kod, ico)
            env.source(IMPORT, "local cache (kataster_lv_import)", oficialny=False, z_cache=True)
            env.warn(NOT_SUPIS)
            if not out["importy"]["pocet_lv"]:
                env.warn("No imported LVs (or they expired after 7 days). Use kataster_osoba_navod, then "
                         "kataster_lv_import on the folder with the saved LV previews.")
            else:
                env.warn(f"Imports valid until {out['importy']['platne_do']} (kept 7 days); if LVs are missing, "
                         "call kataster_lv_import again on the same folder.")
            if len(out["osoby"]) > 1:
                env.warn(f"{len(out['osoby'])} different people match (namesakes); check date of birth and address, "
                         "or call again with datum_narodenia.")
            if out["nejednoznacne"]:
                env.warn("Some LV entries match the name but show no date of birth; they are in 'nejednoznacne'.")
            return env.ok(out)
        except KatasterError as e:
            raise env.fail(e) from e
