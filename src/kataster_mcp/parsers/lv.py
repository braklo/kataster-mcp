"""Parser of the LV extract (výpis z listu vlastníctva) saved as HTML from GeneratePrfPublic.

The page is a sequence of tables. We linearise it into rows of cells (a <p> outside a table
is a one-cell row) and walk them with a small state machine keyed on the section headings.
Column meaning in part A comes from the table header, codes from the legend.
"""

import re
from html.parser import HTMLParser

_DATE = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b")


class _Rows(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._p: list[str] | None = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "tr":
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = []
        elif tag in ("div", "br") and self._cell is not None:
            self._cell.append("\n")
        elif tag == "p" and self._cell is None:
            self._p = []

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip -= 1
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._row.append(_clean("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None
        elif tag == "p" and self._p is not None:
            t = _clean("".join(self._p))
            if t:
                self.rows.append([t])
            self._p = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._cell is not None:
            self._cell.append(data)
        elif self._p is not None:
            self._p.append(data)


def _clean(text: str) -> str:
    lines = [re.sub(r"[ \t\r\f\v\xa0]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def rows_of(page: str) -> list[list[str]]:
    p = _Rows()
    p.feed(page)
    return p.rows


def split_person(text: str) -> dict:
    """'NAME First, street, town, PSČ 000 00, SR, Dátum narodenia: dd.mm.yyyy' or '..., IČO: n'."""
    parts = [s.strip() for s in text.replace("\n", ", ").split(",") if s.strip()]
    out = {"text": text.replace("\n", ", "), "meno": parts[0] if parts else None,
           "datum_narodenia": None, "ico": None, "adresa": None}
    rest = []
    for s in parts[1:]:
        if s.lower().startswith("dátum narodenia"):
            m = _DATE.search(s)
            out["datum_narodenia"] = m.group(0) if m else s.split(":", 1)[-1].strip()
        elif s.upper().startswith("IČO"):
            out["ico"] = s.split(":", 1)[-1].strip()
        elif out["datum_narodenia"] is None and not rest and _DATE.fullmatch(s):
            out["datum_narodenia"] = s  # LV preview: bare date right after the name
        else:
            rest.append(s)
    out["adresa"] = ", ".join(rest) or None
    return out


def split_spouses(text: str) -> list[dict] | None:
    """Joint property of spouses in the extract: 'NAME1 a NAME2, address, Dátum narodenia: d1, Dátum narodenia: d2'
    (one row, one share). Returns both persons, or None when the row is not such a pair."""
    flat = text.replace("\n", ", ")
    dates = re.findall(r"Dátum narodenia:\s*([\d.]+)", flat)
    if len(dates) != 2:
        return None
    parts = [p.strip() for p in flat.split(",") if p.strip()]
    i = next((k for k, p in enumerate(parts) if " a " in p), None)
    if i is None:
        return None
    left, right = parts[i].split(" a ", 1)
    first = ", ".join([*parts[:i], left])
    rest = [p for p in parts[i + 1:] if not p.lower().startswith("dátum narodenia")]
    return [split_person(", ".join([name, *rest, f"Dátum narodenia: {d}"])) for name, d in ((first, dates[0]),
                                                                                                (right, dates[1]))]


def _kv(cells: list[str]) -> dict:
    """['Okres', ':', '503', 'Dolný Kubín', 'Dátum vyhotovenia', ':', '07.10.2026'] -> pairs."""
    out = {}
    i = 0
    while i < len(cells):
        if i + 1 < len(cells) and cells[i + 1] == ":":
            label = cells[i]
            vals = []
            j = i + 2
            while j < len(cells) and not (j + 1 < len(cells) and cells[j + 1] == ":"):
                vals.append(cells[j])
                j += 1
            out[label] = vals
            i = j
        else:
            i += 1
    return out


_HEADER_KEYS = {
    "Parcelné číslo": "cislo",
    "Výmera v m²": "vymera_m2",
    "Výmera v m2": "vymera_m2",
    "Druh pozemku": "druh_pozemku",
    "Spôsob využívania pozemku": "sposob_vyuzivania",
    "Pôvodné katastrálne územie": "povodne_ku",
    "Spoločná nehnuteľnosť": "spolocna_nehnutelnost",
    "Umiestnenie pozemku": "umiestnenie",
    "Druh chránenej nehnuteľnosti": "chranena_nehnutelnost",
    "Druh právneho vzťahu": "pravny_vztah",
}
_LEGEND_KEYS = {"Umiestnenie pozemku": "umiestnenie", "Spoločná nehnuteľnosť": "spolocna_nehnutelnost",
                "Spoločná nehnutelnosť": "spolocna_nehnutelnost", "Spôsob využívania pozemku": "sposob_vyuzivania",
                "Druh chránenej nehnuteľnosti": "chranena_nehnutelnost", "Druh právneho vzťahu": "pravny_vztah",
                "Pôvodné katastrálne územie": "povodne_ku"}
_PARCEL_NO = re.compile(r"^\d+(/\d+)?$")
_BUILDING_KEYS = {"Súpisné číslo": "supisne_cislo", "Druh stavby": "druh_stavby", "Popis stavby": "popis_stavby",
                  "Druh chránenej nehnuteľnosti": "chranena_nehnutelnost", "Umiestnenie stavby": "umiestnenie_stavby"}
_BUILDING_LEGEND = {"Druh stavby": "druh_stavby", "Umiestnenie stavby": "umiestnenie_stavby"}
_LAND_LV = re.compile(r"vlastníctva\s*č\.\s*(\d[\d ]*\d|\d)")


def _coded_cell(value: str) -> str:
    """A code cell of the buildings table may carry the portal's tooltip ("9", "Druh stavby", "9 Bytový dom"):
    keep the description from the tooltip, else the bare code (translated later through the legend)."""
    lines = [line.strip() for line in value.split("\n") if line.strip()]
    if len(lines) < 2:
        return value
    code, last = lines[0], lines[-1]
    return last[len(code):].strip() if last.startswith(code + " ") else code


# Units of a building divided into flats (block of flats): a header per unit (label + newline + value cells),
# then its owners. The preview has one header row; the extract splits it in two rows (house number apart).
_UNIT_KINDS = {"Číslo bytu": "byt", "Číslo nebytového priestoru": "nebytovy_priestor"}
_UNIT_KEYS = {"Vchod (číslo)": "vchod", "Poschodie": "poschodie", "Súpisné číslo": "supisne_cislo",
              "Druh nebytového priestoru": "druh_nebytoveho_priestoru"}
_UNIT_COUNT = re.compile(r"Počet bytov:\s*(\d+)")
_PREMISES_COUNT = re.compile(r"Počet nebytových priestorov:\s*(\d+)")
_SHARE = re.compile(r"\d+\s*/\s*\d+")


def unit_fields(cells: list[str]) -> dict:
    """Known unit labels of one row -> fields (kind and number, entrance, floor, house number, kind of premises,
    share of the common parts)."""
    unit: dict = {}
    for cell in cells:
        label, _, value = cell.partition("\n")
        label, lines = label.strip(), [ln.strip() for ln in value.splitlines() if ln.strip()]
        if label in _UNIT_KINDS:
            unit["druh"], unit["cislo"] = _UNIT_KINDS[label], lines[0] if lines else None
        elif label == "Druh nebytového priestoru":
            unit["druh_nebytoveho_priestoru"] = _coded_cell("\n".join(lines)) if lines else None
        elif label in _UNIT_KEYS:
            unit[_UNIT_KEYS[label]] = lines[-1] if lines else None
        elif label.startswith("Podiel priestoru na spoločných častiach"):
            m = _SHARE.search(" ".join(lines))
            unit["podiel_spolocne"] = m.group(0).replace(" ", "") if m else None
    return unit


def unit_header(cells: list[str]) -> dict | None:
    """A row that starts a unit ('Číslo bytu' / 'Číslo nebytového priestoru') -> its fields, else None."""
    unit = unit_fields(cells)
    return unit if "druh" in unit else None


def units_summary(result: dict, units: list[dict], text: str, paged: bool = False) -> bool:
    """Put the units and their counts into result; True when the document shows fewer units than it states."""
    flats = sum(u["druh"] == "byt" for u in units)
    stated = _UNIT_COUNT.search(text)
    stated_p = _PREMISES_COUNT.search(text)
    partial = paged or (stated is not None and int(stated.group(1)) != flats) or (
        stated_p is not None and int(stated_p.group(1)) != len(units) - flats)
    result["jednotky"] = units
    result["jednotky_pocet"] = {"byty": flats, "nebytove_priestory": len(units) - flats,
                                "byty_podla_hlavicky": int(stated.group(1)) if stated else None}
    if partial:
        result["jednotky_poznamka"] = ("The document does not show all units (paging, or fewer units than its header "
                                       "states); owners of the missing units are not in this result.")
    return partial


def buildings(rows: list[list[str]]) -> list[dict]:
    """Buildings table of part A (header 'Súpisné číslo', 'Na pozemku parcelné číslo', ...) -> rows; a following
    'Právny vzťah k pozemku ...' row is attached to the building above it (it names the LV of the land)."""
    out: list[dict] = []
    columns: list[str] = []
    for cells in rows:
        text = " ".join(c for c in cells if c)
        if cells and cells[0].startswith("Súpisné"):
            columns = ["parcela" if c.lower().startswith(("na pozemku", "na parcele")) else
                       _BUILDING_KEYS.get(" ".join(c.split()), c) for c in cells]
            continue
        if columns and len(cells) == len(columns):
            row = dict(zip(columns, cells))
            for key in _BUILDING_LEGEND.values():
                if row.get(key):
                    row[key] = _coded_cell(row[key])
            if _PARCEL_NO.match(row.get("parcela", "")):
                out.append(row)
                continue
        if text.startswith("Právny vzťah") and out:
            out[-1]["pravny_vztah_k_pozemku"] = text.replace("\n", " ")
            m = _LAND_LV.search(text)
            if m:
                out[-1]["lv_pozemku"] = m.group(1).replace(" ", "")
    return out


def translate(rows: list[dict], legend: dict) -> None:
    for row in rows:
        for key, mapping in legend.items():
            if row.get(key) in mapping:
                row[key] = mapping[row[key]]


def parse_lv(page: str) -> dict:
    """LV extract saved as HTML."""
    return {**parse_rows(rows_of(page)), "zdroj_typ": "vypis_lv_html"}


def coverage(data: dict, present: set[str], *, partial: bool = False) -> dict:
    """Reading evidence per part (missing / unread / read), never a claim that every page was supplied."""
    read = {
        "A": bool(data.get("cast_a") or data.get("stavby")),
        "B": bool(data["cast_b"]["vlastnici"] or data["cast_b"]["ine_opravnene_osoby"]),
        "C": bool(data["cast_c"]),
    }
    return {
        **{f"cast_{s.lower()}": "missing" if s not in present else "read" if read[s] else "unread"
           for s in ("A", "B", "C")},
        "rozsah": "partial" if partial else "unverified",
    }


def encumbrances(lines: list[str]) -> str:
    """Extract: 'none' only for the explicit 'Bez tiarch.'; a missing or empty part C is unknown."""
    if not lines:
        return "unknown"
    return "none" if lines == ["Bez tiarch."] else "see cast_c"


def partial_heading(flat: list[str]) -> bool:
    """Partial extract (čiastočný výpis) by its heading only: the word anywhere else (e.g. in part C) does not count."""
    for t in flat:
        if t.startswith("ČASŤ"):
            return False
        if "čiastočný výpis" in t.casefold():
            return True
    return False


def parse_rows(rows: list[list[str]]) -> dict:
    """State machine over rows of cells; shared by the HTML and the PDF extract."""
    flat = [" ".join(r) for r in rows]
    if not any("LISTU VLASTNÍCTVA" in t for t in flat):
        raise ValueError("not an LV extract (no 'VÝPIS Z LISTU VLASTNÍCTVA')")
    head: dict = {}
    result: dict = {"hlavicka": head, "cast_a": [], "cast_b": {"vlastnici": [], "ine_opravnene_osoby": []},
                    "cast_c": [], "legenda": {}}
    section = None
    present: set[str] = set()
    register = None
    columns: list[str] = []
    legend_key = None
    owner = None
    units: list[dict] = []
    building_rows: list[list[str]] | None = None  # part A buildings table (not verified on a real extract)
    for cells in rows:
        text = " ".join(cells)
        m = re.search(r"LISTU VLASTNÍCTVA č\.\s*(\d+)", text)
        if m:
            result["lv"] = m.group(1)
            continue
        if any(c == ":" for c in cells):
            for label, vals in _kv(cells).items():
                head[label] = " ".join(vals)
            continue
        if text.startswith("ČASŤ A"):
            present.add("A")
            section = "A"
            continue
        if text.startswith("ČASŤ B"):
            present.add("B")
            section, owner = "B", None
            continue
        if text.startswith("ČASŤ C"):
            present.add("C")
            section = "C"
            continue
        if section == "A":
            mreg = re.search(r"Parcely registra\s*[„\"“]?([CE])", text)
            if mreg:
                register, building_rows = mreg.group(1), None
                continue
            if re.match(r"Stavby\b", text) or cells[0].startswith("Súpisné"):
                building_rows = [] if building_rows is None else building_rows
                if not text.startswith("Stavby"):
                    building_rows.append(cells)
                continue
            if building_rows is not None and text != "Legenda":
                building_rows.append(cells)
                continue
            if cells[0] in _HEADER_KEYS or cells[0] == "Parcelné číslo":
                columns = [_HEADER_KEYS.get(c, c) for c in cells]
                continue
            if text == "Legenda":
                section = "A-legend"
                continue
            if _PARCEL_NO.match(cells[0]) and columns:
                row = {"register": register}
                for k, v in zip(columns, cells):
                    row[k] = v
                if "vymera_m2" in row and row["vymera_m2"].isdigit():
                    row["vymera_m2"] = int(row["vymera_m2"])
                result["cast_a"].append(row)
                continue
            if text.startswith("Iné údaje:") and result["cast_a"]:
                result["cast_a"][-1]["ine_udaje"] = text.split(":", 1)[1].strip()
                continue
        if section == "A-legend":
            if len(cells) == 1 and (cells[0] in _LEGEND_KEYS or cells[0] in _BUILDING_LEGEND):
                legend_key = _LEGEND_KEYS.get(cells[0]) or _BUILDING_LEGEND[cells[0]]
                result["legenda"].setdefault(legend_key, {})
                continue
            if len(cells) == 2 and legend_key:
                result["legenda"][legend_key][cells[0]] = cells[1]
                continue
        if section == "B-legend":  # legend of the units (kind of non-residential premises) closes part B
            if len(cells) == 1 and cells[0] == "Druh nebytového priestoru":
                legend_key = "druh_nebytoveho_priestoru"
                result["legenda"].setdefault(legend_key, {})
                continue
            if len(cells) == 2 and legend_key == "druh_nebytoveho_priestoru":
                result["legenda"][legend_key][cells[0]] = cells[1]
                continue
        if section == "B":
            unit = unit_fields(cells)
            if "druh" in unit:
                units.append({**unit, "vlastnici_pocet": 0})
                owner = None
                continue
            if unit and units and owner is None:  # second header row of the unit in the extract: house number etc.
                units[-1].update(unit)
                continue
            if text == "Legenda":
                section = "B-legend"
                continue
            if len(cells) == 3 and cells[0].isdigit() and "/" in cells[2]:
                share = cells[2].replace(" ", "")
                pair = split_spouses(cells[1])
                persons = ([{"poradie": int(cells[0]), **p, "podiel": share, "bsm": True} for p in pair] if pair
                           else [{"poradie": int(cells[0]), **split_person(cells[1]), "podiel": share}])
                if units:
                    for person in persons:
                        person["jednotka"] = {k: units[-1].get(k) for k in ("druh", "vchod", "cislo")}
                    units[-1]["vlastnici_pocet"] += len(persons)
                result["cast_b"]["vlastnici"].extend(persons)
                owner = persons[-1]
                continue
            if text.startswith("Titul nadobudnutia") and owner is not None:
                owner["titul_nadobudnutia"] = text.split(":", 1)[1].strip().replace("\n", " ")
                continue
            if text.startswith("Iné údaje:") and owner is not None:
                owner["ine_udaje"] = text.split(":", 1)[1].strip()
                continue
            if text.startswith("Poznámk") and owner is not None:
                owner["poznamka"] = text.split(":", 1)[-1].strip() if ":" in text else text
                continue
            mo = re.match(r"^(Správca|Nájomca|Iná oprávnená osoba)\s*-\s*(.*)$", text, re.S)
            if mo:
                other = {"typ": mo.group(1), "text": mo.group(2).strip()}
                if units:
                    other["jednotka"] = {k: units[-1].get(k) for k in ("druh", "vchod", "cislo")}
                result["cast_b"]["ine_opravnene_osoby"].append(other)
                owner = None
                continue
        if section == "C":
            if text.startswith("Výpis je nepoužiteľný"):
                continue
            result["cast_c"].append(text.replace("\n", " "))
    result["stavby"] = buildings(building_rows or [])
    if not result["stavby"] and any("Súpisné" in t for t in flat):
        result["stavby_neprecitane"] = True
    # translate coded columns by the legend
    translate(result["cast_a"], {k: v for k, v in result["legenda"].items() if k not in _BUILDING_LEGEND.values()})
    translate(result["stavby"], {k: v for k, v in result["legenda"].items() if k != "umiestnenie"})
    if "lv" not in result:
        raise ValueError("LV number not found")
    partial = partial_heading(flat)
    heads = sum(t.count("Vchod (číslo)") for t in flat)  # every unit header has an entrance cell
    if heads != len(units):  # also a layout (e.g. the PDF) whose unit headers this parser does not read
        raise ValueError(f"LV extract: {heads} unit headers but {len(units)} units parsed")
    if units:
        translate(units, {"druh_nebytoveho_priestoru": result["legenda"].get("druh_nebytoveho_priestoru", {})})
        partial = units_summary(result, units, " ".join(flat)) or partial
    result["tarchy"] = encumbrances(result["cast_c"])
    result["uplnost"] = coverage(result, present, partial=partial)
    return result


PARSERS = (parse_lv, parse_rows, rows_of, split_person, split_spouses, buildings, _coded_cell, translate, _kv, _clean, _Rows,
           coverage, encumbrances, partial_heading, unit_fields, unit_header, units_summary)
