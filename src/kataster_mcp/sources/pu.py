"""Land consolidation projects (pozemkové úpravy, PÚ) per cadastral unit: open data of MPRV SR.

Four datasets in the national open data catalogue (NKOD), CC BY 4.0, updated quarterly (verified 2026-10-07):
complex / simple projects (komplexné / jednoduché) x finished / in progress (ukončené / rozpracované).
CSV in cp1250 with ';'. They carry the cadastral unit code, never the project boundary: the boundary exists
only as a line in the cadastral map. Preparatory proceedings (prípravné konanie) are not in the data.

Pitfalls: one cell can hold several k.ú. codes ("848531, 848522"); one k.ú. can have several projects;
a few rows are empty or have a malformed year.
"""

import csv
import io
import re

from ..cache import DAY, parser_version
from ..envelope import KatasterError

DOWNLOAD = "https://data.slovensko.sk/download?id="
SOURCE = "mprv_pu"
URL = "https://data.slovensko.sk (MPRV SR: zoznamy projektov pozemkových úprav)"
TTL_DAYS = 30

# (typ, stav, CSV distribution id in NKOD)
DATASETS = [
    ("komplexne", "ukoncene", "845d1039-801d-4237-b48e-a4ed5e85243a"),
    ("komplexne", "rozpracovane", "161a6540-8e85-482b-8ede-1f42fe225482"),
    ("jednoduche", "ukoncene", "f98d894c-268a-4771-94c4-21d0bb0fde50"),
    ("jednoduche", "rozpracovane", "8867c7a1-869e-4994-81b7-043b80d8f5b9"),
]

# column -> how its header starts (headers differ slightly between the four files)
COLUMNS = {
    "nazov": ("Názov projektu",),
    "ku": ("kód k.ú.",),
    "zdroj_financovania": ("zdroj financovania",),
    "rok_nariadenia": ("rok nariadenia",),
    "rok_schvalenia": ("rok právoplatného rozhodnutia", "schválenie vykonania"),
    "vymera_obvodu_ha": ("výmera obvodu",),
}
REQUIRED = ("nazov", "ku", "rok_nariadenia")

NOTE = ("Land consolidation projects registered for the whole cadastral unit (MPRV SR open data). "
        "It does not say whether this parcel lies inside the project boundary; preparatory proceedings are not included.")
WARNING = ("pozemkove_upravy: status of the whole cadastral unit, not whether the parcel lies inside the project "
           "boundary; preparatory proceedings are not in the data.")


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1250"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("unknown text encoding")


def _year(v: str):
    v = v.strip()
    if not v:
        return None
    return int(v) if re.fullmatch(r"(19|20)\d\d", v) else v  # keep a malformed value as given


def _number(v: str):
    v = v.strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(v) if "." in v else int(v)
    except ValueError:
        return None


def parse_csv(raw: bytes, typ: str, stav: str) -> list[dict]:
    text = _decode(raw)
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    if not rows:
        raise ValueError("empty CSV")
    header = [h.strip() for h in rows[0]]
    idx = {}
    for key, prefixes in COLUMNS.items():
        hit = [i for i, h in enumerate(header) if any(h.startswith(p) for p in prefixes)]
        if hit:
            idx[key] = hit[0]
    missing = [k for k in REQUIRED if k not in idx]
    if missing:
        raise ValueError(f"columns not found: {missing}; header {header}")

    def cell(r, key):
        i = idx.get(key)
        return r[i] if i is not None and i < len(r) else ""

    out = []
    for r in rows[1:]:
        codes = re.findall(r"\d{6}", cell(r, "ku"))
        if not codes:
            continue
        out.append({
            "ku_kody": codes,
            "typ": typ,
            "stav": stav,
            "nazov": cell(r, "nazov").strip() or None,
            "rok_nariadenia": _year(cell(r, "rok_nariadenia")),
            "rok_schvalenia": _year(cell(r, "rok_schvalenia")) if "rok_schvalenia" in idx else None,
            "vymera_obvodu_ha": _number(cell(r, "vymera_obvodu_ha")),
            "zdroj_financovania": cell(r, "zdroj_financovania").strip() or None,
        })
    return out


VERSION = parser_version(parse_csv, _year, _number, _decode)


async def _dataset(http, store, typ: str, stav: str, dist_id: str) -> list[dict]:
    key = f"{typ}|{stav}|{dist_id}"
    hit = store.get(SOURCE, VERSION, key, TTL_DAYS * DAY) if store is not None else None
    if hit is not None:
        return hit[0]
    resp = await http.get(DOWNLOAD + dist_id)
    try:
        rows = parse_csv(resp.content, typ, stav)
    except ValueError as e:
        raise KatasterError("parser_drift", f"MPRV land consolidation list ({typ}, {stav}) changed: {e}") from e
    if store is not None:
        store.put(SOURCE, VERSION, key, rows)
    return rows


def for_ku(rows: list[dict], ku_kod: str) -> list[dict]:
    hits = [{k: v for k, v in r.items() if k != "ku_kody"} | ({"ine_ku": [c for c in r["ku_kody"] if c != ku_kod]}
            if len(r["ku_kody"]) > 1 else {}) for r in rows if ku_kod in r["ku_kody"]]
    order = {"rozpracovane": 0, "ukoncene": 1}
    return sorted(hits, key=lambda r: (order[r["stav"]], -(r["rok_nariadenia"] if isinstance(r["rok_nariadenia"], int) else 0)))


async def pozemkove_upravy(http, t, store=None) -> dict:
    if not t.ku_kod:
        raise KatasterError("invalid_input", "Land consolidation needs a parcel (ku + cislo), not a point.")
    rows = []
    for typ, stav, dist_id in DATASETS:
        rows += await _dataset(http, store, typ, stav, dist_id)
    projects = for_ku(rows, t.ku_kod)
    return {
        "ku_kod": t.ku_kod,
        "prebiehaju": any(p["stav"] == "rozpracovane" for p in projects),
        "projekty": projects,
        "poznamka": NOTE,
    }
