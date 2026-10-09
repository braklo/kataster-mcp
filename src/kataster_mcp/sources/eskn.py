"""ESKN portal of ÚGKK SR: UNOFFICIAL interfaces, used at human pace (at least 3 s between requests).

- ESRI identify (what GisPortal45 calls on a map click) maps a point to the internal
  parcel ID and administrative units.
- The parcel detail page (detail_pc / detail_pe) shows the LV number, land type and
  owners with shares. It does NOT show administrators, tenants or encumbrances.

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
_OWNER = re.compile(
    r"<li[^>]*>\s*<span[^>]*>\s*<span[^>]*>\s*(\d+)\.\s*<a[^>]*ucastnik-link[^>]*>(.*?)</a>.*?Podiel:\s*([0-9]+\s*/\s*[0-9]+)",
    re.S,
)
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


def parse_detail(page: str) -> dict:
    if "Informácie o parcele" not in page:
        raise ValueError("not a parcel detail page")
    info_start = page.index("Informácie o parcele")
    owners_start = page.find("VLASTN", info_start)
    info = page[info_start: owners_start if owners_start > 0 else None].replace("<sup>2</sup>", "2")
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
    owners = []
    if owners_start > 0:
        for order, person, share in _OWNER.findall(page[owners_start:]):
            owners.append({"poradie": int(order), **split_person(_text(person)), "podiel": share.replace(" ", "")})
    valid = _VALID.findall(page)
    return {
        **fields,
        "ostatne": other,
        "vlastnici": owners,
        "stav_k": valid[0] if valid else None,
        "spravcovia_najomcovia_tarchy": "nie sú v detaile parcely, len vo výpise LV",
    }


def parse_identify_safe(data: dict, register: str, number: str) -> dict:
    try:
        return parse_identify(data, register, number)
    except (KeyError, TypeError) as e:
        raise KatasterError("parser_drift", f"ESKN identify response changed: {e}") from e


PARSERS_IDENTIFY = (parse_identify, _null)
PARSERS_DETAIL = (parse_detail, split_person, _text)
PARSER_DETAIL_REGEXES = (_LI.pattern, _OWNER.pattern, _VALID.pattern, _DATE.pattern, json.dumps(_LABELS, ensure_ascii=False))
