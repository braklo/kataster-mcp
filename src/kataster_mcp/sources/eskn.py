"""ESKN portal of ÚGKK SR: UNOFFICIAL interfaces, used at human pace (at least 3 s between requests).

- ESRI identify (what GisPortal45 calls on a map click) maps a point to the internal
  parcel ID and administrative units.
- The parcel detail page (detail_pc / detail_pe) shows the LV number, land type,
  buildings on the parcel (house number only) and owners with shares. It does NOT show
  administrators, tenants or encumbrances, nor the LV of a building.

Identify must be called on the WFS referencePoint, not on the centroid: the centroid of
an irregular parcel can fall outside it.
"""

import html as htmllib
import json
import math
import re

from ..envelope import KatasterError

HOST = "https://kataster.skgeodesy.sk"
IDENTIFY_URL = f"{HOST}/eskn/rest/services/VRM/identify/MapServer/identify"
DETAIL_URL = {"C": f"{HOST}/eskn-portal/search-sgi/detail_pc", "E": f"{HOST}/eskn-portal/search-sgi/detail_pe"}
SOURCE_IDENTIFY = "eskn_identify"
SOURCE_DETAIL = "eskn_detail"


def to_3857(lat: float, lon: float) -> tuple[float, float]:
    x = lon * 20037508.34 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) * 6378137
    return x, y


def identify_params(lat: float, lon: float) -> dict:
    x, y = to_3857(lat, lon)
    return {
        "f": "json", "tolerance": 0, "returnGeometry": "false", "imageDisplay": "1536,524,96",
        "geometry": json.dumps({"x": x, "y": y}), "geometryType": "esriGeometryPoint", "sr": 102100,
        "mapExtent": f"{x - 500},{y - 500},{x + 500},{y + 500}", "layers": "all",
    }


def _null(v):
    return None if v in (None, "", "Null") else v


def parse_identify(data: dict, register: str, number: str) -> dict:
    """Pick the parcel of the given register and number and the admin units at the point."""
    results = data["results"]
    parcel = None
    admin = {}
    for r in results:
        a = r["attributes"]
        layer = r["layerName"]
        if layer.endswith(f"KN_PARCELS_{register}"):
            label = a.get("PARCEL_NUMBER_FULL") or a.get("PARCEL_NUMBER_LABEL") or a.get("PARCEL_NUMBER")
            if label == number and parcel is None:
                parcel = a
        elif layer.endswith("KN_C_REGIONS"):
            admin["kraj"] = a["REGION_NAME"]
        elif layer.endswith("KN_C_DISTRICTS"):
            admin["okres"] = a["DISTRICT_NAME"]
        elif layer.endswith("KN_C_MUNICIPALITIES"):
            admin["obec"] = a["MUNICIPALITY_NAME"]
            admin["obec_kod"] = a["MUNICIPALITY_CODE"]
        elif layer.endswith("KN_C_CADASTRAL_UNITS"):
            admin["ku_kod"] = a["CADASTRAL_UNIT_CODE"]
            admin["ku_nazov"] = a["CADASTRAL_UNIT_NAME"]
    if parcel is None:
        return {"id": None, "uzemie": admin}
    protected = _null(parcel.get("PROTECTED_PROPERTIES_NAME_LIST"))
    return {
        "id": parcel["ID"],
        "lv_interne_id": _null(parcel.get("FOLIO_ID")),
        "poloha_zu_kod": _null(parcel.get("PLOT_LOCALIZATION_ID")),
        "chranene_nehnutelnosti": [s.strip() for s in protected.split(",")] if protected else [],
        "uzemie": admin,
    }


_LI = re.compile(r"<li[^>]*>\s*([^<:]{2,80}?):\s*(.*?)</li>", re.S)
_TAG = re.compile(r"<[^>]+>")
_PERSON = re.compile(r'<a[^>]*class="[^"]*(?<![\w-])ucastnik-link[^"]*"[^>]*>(.*?)</a>', re.S)
_BSM_PERSON = re.compile(r'<a[^>]*class="[^"]*\bbsm-ucastnik-link[^"]*"[^>]*>(.*?)</a>', re.S)
_OWNER_LINK = re.compile(r'<a[^>]*class="[^"]*ucastnik-link')  # also bsm-ucastnik-link: one per person
_NO_RECORD = re.compile(r"^Neexistuje záznam")
_ORDER = re.compile(r"(BSM\s+)?(\d+)\.")
_SHARE = re.compile(r"Podiel:\s*([0-9]+\s*/\s*[0-9]+)")
_H3 = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S)
_LI_ANY = re.compile(r"<li[^>]*>(.*?)</li>", re.S)
_SUPISNE = re.compile(r"Súpisné číslo:\s*(\S+)")
_VALID = re.compile(r'title="Údaje platné k dátumu">\s*([0-9.]+)\s*<')
_DATE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")

_LABELS = {
    "Číslo listu vlastníctva": "lv",
    "Výmera parcely v m2": "vymera_m2",
    "Druh pozemku": "druh_pozemku",
    "Umiestnenie pozemku": "umiestnenie",
    "Spoločná nehnuteľnosť": "spolocna_nehnutelnost",
    "Druh právneho vzťahu": "pravny_vztah",
    "Číslo pôvodného katastrálneho územia": "povodne_ku_kod",
    "Názov pôvodného katastrálneho územia": "povodne_ku_nazov",
}


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(_TAG.sub(" ", fragment))).strip()


def split_person(text: str) -> dict:
    """'Name, DD.MM.YYYY, street, town, zip, country' -> parts. Legal persons have no date."""
    parts = [p.strip() for p in text.split(",")]
    out = {"text": text, "meno": parts[0], "datum_narodenia": None, "adresa": None}
    rest = parts[1:]
    if rest and _DATE.match(rest[0]):
        out["datum_narodenia"] = rest[0]
        rest = rest[1:]
    if rest:
        out["adresa"] = ", ".join(rest)
    return out


def sections(page: str) -> list[tuple[str, str]]:
    """Split the page at its <h3> headings: [(heading text, html up to the next heading)]."""
    heads = list(_H3.finditer(page))
    return [(_text(m.group(1)), page[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(page)])
            for i, m in enumerate(heads)]


def _building(li: str) -> dict:
    text = _text(li)
    m = _SUPISNE.search(text)
    return {"supisne_cislo": m.group(1) if m else None, "text": text}


def _owner_row(li: str) -> list[dict]:
    """One owner row: "1. <person> Podiel: a/b", or BSM: "BSM 1. <spouse> <spouse> Podiel: a/b"
    (the second spouse has class bsm-ucastnik-link; seen on the building detail 2026-11)."""
    head = _text(li[: li.rfind("<a", 0, li.find("ucastnik-link"))])
    order, share = _ORDER.fullmatch(head), _SHARE.search(li)
    persons = _PERSON.findall(li) + _BSM_PERSON.findall(li)
    if order is None or share is None or not persons:
        raise ValueError("owner row without order, share or person")
    bsm = bool(order.group(1)) or len(persons) > 1
    return [{"poradie": int(order.group(2)), **split_person(_text(person)), "podiel": share.group(1).replace(" ", ""),
             **({"bsm": True} if bsm else {})} for person in persons]


def parse_detail(page: str) -> dict:
    """Parcel detail by sections. The owner heading has changed case before ("VLASTNÍK" -> "Vlastník"),
    so headings are matched case-insensitively. Drift: person links or shares on the page that were not read.
    An owner section with no person link and no share gives an empty list (the caller warns and does not cache)."""
    if "Informácie o parcele" not in page:
        raise ValueError("not a parcel detail page")
    parts = sections(page)
    info = next((body for title, body in parts if title == "Informácie o parcele"), "")
    info = info.replace("<sup>2</sup>", "2")
    fields: dict = {}
    other: dict = {}
    for label, value in _LI.findall(info):
        label = _text(label)
        key = _LABELS.get(label)
        val = _text(value) or None
        if key:
            fields[key] = val
        else:
            other[label] = val
    if "lv" not in fields:
        raise ValueError("LV number not found")
    if fields.get("vymera_m2"):
        fields["vymera_m2"] = int(fields["vymera_m2"])

    owner_parts = [body for title, body in parts if title.casefold().startswith("vlastn")]
    owners = []
    for body in owner_parts:
        items = [li for li in _LI_ANY.findall(body) if "ucastnik-link" in li]
        if len(items) != body.count("Podiel:"):
            raise ValueError(f"owner section: {len(items)} owner rows, {body.count('Podiel:')} shares on the page")
        for li in items:
            owners.extend(_owner_row(li))

    building_parts = [body for title, body in parts if title.casefold().startswith("stavb")]
    buildings = [_building(li) for body in building_parts for li in _LI_ANY.findall(body)]
    if building_parts and not buildings and not any(_NO_RECORD.search(_text(b)) for b in building_parts):
        raise ValueError("building section present but no building parsed")

    # Never report fewer owners than the page links to (e.g. an owner heading renamed to something unknown):
    # an incomplete list would be cached as a valid answer.
    links = len(_OWNER_LINK.findall(page))
    if links > len(owners):
        raise ValueError(f"page has {links} owner links but {len(owners)} owners were parsed")

    valid = _VALID.findall(page)
    return {
        **fields,
        "ostatne": other,
        "vlastnici": owners,
        "vlastnici_sekcia": bool(owner_parts),
        "stavby": buildings,
        "sekcie": [title for title, _ in parts if title != "Popis služby"],
        "stav_k": valid[0] if valid else None,
        "spravcovia_najomcovia_tarchy": "nie sú v detaile parcely, len vo výpise LV",
    }


def parse_identify_safe(data: dict, register: str, number: str) -> dict:
    try:
        return parse_identify(data, register, number)
    except (KeyError, TypeError) as e:
        raise KatasterError("parser_drift", f"ESKN identify response changed: {e}") from e


PARSERS_IDENTIFY = (parse_identify, _null)
PARSERS_DETAIL = (parse_detail, sections, _building, _owner_row, split_person, _text)
PARSER_DETAIL_REGEXES = (_LI.pattern, _PERSON.pattern, _BSM_PERSON.pattern, _ORDER.pattern, _SHARE.pattern,
                         _VALID.pattern, _DATE.pattern, _H3.pattern, _LI_ANY.pattern, _SUPISNE.pattern,
                         _OWNER_LINK.pattern, _NO_RECORD.pattern,
                         json.dumps(_LABELS, ensure_ascii=False))
