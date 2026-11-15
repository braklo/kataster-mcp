"""ESKN building detail (detail_st): UNOFFICIAL, reached only by a JSF form post from the parcel detail.

The building link on the parcel detail ("Súpisné číslo: N" in the section "Stavby") is a PrimeFaces command link:
the browser posts the form `bodyForm` with the link id and the ViewState and the portal redirects to
/eskn-portal/search/parcels/detail_st. The page shows the LV of the building, its kind, description and placement,
the C parcels under it and its owners with shares.

A building divided into units (block of flats) lists owners of the units without shares, ten at a time behind
"Zobraziť viac". Those rows are owners of flats, not of the building, so they are counted, never returned.
"""

import json
import re

from . import eskn

SOURCE = "eskn_detail_st"
_INPUT = re.compile(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"')
_LINK_ID = re.compile(r'<a[^>]*\bid="([^"]+)"')
_ACTION = re.compile(r'action="([^"]+)"')
_LABELS = {
    "Číslo listu vlastníctva": "lv",
    "Druh stavby": "druh_stavby",
    "Popis stavby": "popis_stavby",
    "Umiestnenie stavby": "umiestnenie_stavby",
    "Miestna časť": "miestna_cast",
    "Druh chránenej nehnuteľnosti k stavbe": "chranena_nehnutelnost",
}


def building_link(page: str, index: int, house_number: str | None) -> str:
    """Id of the command link of the index-th building in the section "Stavby" of a parcel detail."""
    parts = [body for title, body in eskn.sections(page) if title.casefold().startswith("stavb")]
    items = [li for body in parts for li in eskn._LI_ANY.findall(body)]
    if index >= len(items):
        raise ValueError(f"building {index + 1} not found in the section Stavby ({len(items)} buildings)")
    li = items[index]
    if house_number and eskn._building(li).get("supisne_cislo") != house_number:
        raise ValueError(f"building {index + 1} has another house number than {house_number}")
    m = _LINK_ID.search(li)
    if m is None:
        raise ValueError("building row without a link")
    return m.group(1)


def postback(page: str, link_id: str) -> tuple[str, dict]:
    """(action, fields) of the post that clicking the command link `link_id` inside bodyForm makes."""
    i = page.find('id="bodyForm"')
    if i < 0:
        raise ValueError("bodyForm not found on the parcel detail")
    form = page[page.rfind("<form", 0, i): page.find("</form>", i)]
    if f'id="{link_id}"' not in form:
        raise ValueError("building link is not inside bodyForm")
    action = _ACTION.search(form)
    fields = dict(_INPUT.findall(form))
    if action is None or "javax.faces.ViewState" not in fields:
        raise ValueError("bodyForm without action or ViewState")
    fields[link_id] = link_id
    return action.group(1).replace("&amp;", "&"), fields


def parse_building(page: str) -> dict:
    """Building detail -> LV, kind, description, parcels under it and owners (or the number of unit owner rows)."""
    parts = [(title, body) for title, body in eskn.sections(page) if title != "Popis služby"]
    info = next((body for title, body in parts if title == "Informácie o stavbe"), None)
    if info is None:
        raise ValueError("not a building detail page")
    fields: dict = {}
    other: dict = {}
    for label, value in eskn._LI.findall(info):
        label = eskn._text(label)
        val = eskn._text(value) or None
        if label in _LABELS:
            fields[_LABELS[label]] = val
        else:
            other[label] = val
    if "lv" not in fields:
        raise ValueError("LV of the building not found")

    parcels = [eskn._text(li) for title, body in parts if title.startswith("Číslo parcely")
               for li in eskn._LI_ANY.findall(body)]

    owner_parts = [body for title, body in parts if title.casefold().startswith("vlastn")]
    rows = [li for body in owner_parts for li in eskn._LI_ANY.findall(body) if "ucastnik-link" in li]
    shares = sum(body.count("Podiel:") for body in owner_parts)
    owners: list | None = []
    unit_rows = 0
    if rows and shares == 0:
        owners, unit_rows = None, len(rows)  # owners of flats/units: counted, never returned
    elif len(rows) != shares:
        raise ValueError(f"building owner section: {len(rows)} owner rows, {shares} shares on the page")
    else:
        for li in rows:
            owners.extend(eskn._owner_row(li))
        links = len(eskn._OWNER_LINK.findall(page))
        if links > len(owners):
            raise ValueError(f"page has {links} owner links but {len(owners)} owners were parsed")

    valid = eskn._VALID.findall(page)
    return {
        **fields,
        "ostatne": other,
        "parcely_c": parcels,
        "vlastnici": owners,
        "vlastnici_sekcia": bool(owner_parts),
        "jednotky": unit_rows > 0,
        "riadky_vlastnikov_jednotiek": unit_rows,
        "dalsie_riadky": unit_rows > 0 and "Zobraziť viac" in page,
        "stav_k": valid[0] if valid else None,
    }


PARSERS = (building_link, postback, parse_building, *eskn.PARSERS_DETAIL)
PARSER_REGEXES = (_INPUT.pattern, _LINK_ID.pattern, _ACTION.pattern, json.dumps(_LABELS, ensure_ascii=False),
                  *eskn.PARSER_DETAIL_REGEXES)
