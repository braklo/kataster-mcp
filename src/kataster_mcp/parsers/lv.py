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


def parse_lv(page: str) -> dict:
    """LV extract saved as HTML."""
    return {**parse_rows(rows_of(page)), "zdroj_typ": "vypis_lv_html"}


def parse_rows(rows: list[list[str]]) -> dict:
    """State machine over rows of cells; shared by the HTML and the PDF extract."""
    flat = [" ".join(r) for r in rows]
    if not any("LISTU VLASTNÍCTVA" in t for t in flat):
        raise ValueError("not an LV extract (no 'VÝPIS Z LISTU VLASTNÍCTVA')")
    head: dict = {}
    result: dict = {"hlavicka": head, "cast_a": [], "cast_b": {"vlastnici": [], "ine_opravnene_osoby": []},
                    "cast_c": [], "legenda": {}}
    section = None
    register = None
    columns: list[str] = []
    legend_key = None
    owner = None
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
            section = "A"
            continue
        if text.startswith("ČASŤ B"):
            section, owner = "B", None
            continue
        if text.startswith("ČASŤ C"):
            section = "C"
            continue
        if section == "A":
            mreg = re.search(r"Parcely registra\s*[„\"“]?([CE])", text)
            if mreg:
                register = mreg.group(1)
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
            if len(cells) == 1 and cells[0] in _LEGEND_KEYS:
                legend_key = _LEGEND_KEYS[cells[0]]
                result["legenda"].setdefault(legend_key, {})
                continue
            if len(cells) == 2 and legend_key:
                result["legenda"][legend_key][cells[0]] = cells[1]
                continue
        if section == "B":
            if len(cells) == 3 and cells[0].isdigit() and "/" in cells[2]:
                owner = {"poradie": int(cells[0]), **split_person(cells[1]), "podiel": cells[2].replace(" ", "")}
                result["cast_b"]["vlastnici"].append(owner)
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
                result["cast_b"]["ine_opravnene_osoby"].append({"typ": mo.group(1), "text": mo.group(2).strip()})
                owner = None
                continue
        if section == "C":
            if text.startswith("Výpis je nepoužiteľný"):
                continue
            result["cast_c"].append(text.replace("\n", " "))
    # translate coded columns by the legend
    for row in result["cast_a"]:
        for key, mapping in result["legenda"].items():
            code = row.get(key)
            if code in mapping:
                row[key] = mapping[code]
    if "lv" not in result:
        raise ValueError("LV number not found")
    result["tarchy"] = "none" if result["cast_c"] in ([], ["Bez tiarch."]) else "see cast_c"
    return result


PARSERS = (parse_lv, parse_rows, rows_of, split_person, _kv, _clean, _Rows)
