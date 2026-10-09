"""Parser of the ESKN portal page 'Náhľad listu vlastníctva' (LV preview, no CAPTCHA),
saved by the user as 'Web page, complete' (the URL stays on the parcel detail, so
'HTML only' re-downloads the detail instead).

Output has the same shape as parsers.lv.parse_lv so kataster_lv_import treats both alike.
"""

import html as htmllib
import re

from .lv import _HEADER_KEYS, rows_of, split_person

_TOOLTIP = re.compile(
    r'<div class="m-bottom-xs">\s*([^<]+?)\s*</div>\s*<div class="display-table">\s*'
    r'<span[^>]*>\s*([^<]*?)\s*</span>\s*<span[^>]*>\s*([^<]*?)\s*</span>', re.S)
_LEGEND_KEYS = {"Umiestnenie pozemku": "umiestnenie", "Spoločná nehnuteľnosť": "spolocna_nehnutelnost",
                "Pôvodné katastrálne územie": "povodne_ku", "Spôsob využívania pozemku": "sposob_vyuzivania",
                "Druh chránenej nehnuteľnosti": "chranena_nehnutelnost"}
_PARCEL_NO = re.compile(r"^\d+(/\d+)?$")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _section(page: str, start: str, end: str | None) -> str:
    i = page.find(f'class="{start}"')
    if i < 0:
        return ""
    j = page.find(f'class="{end}"', i) if end else -1
    return page[i: j if j > 0 else None]


def parse_nahlad(page: str) -> dict:
    m = re.search(r"Náhľad listu vlastníctva\s*(?:č\.)?\s*(\d+)", _text(page))
    if not m:
        raise ValueError("not an LV preview (no 'Náhľad listu vlastníctva')")
    head_text = _text(page[: page.find('class="cast-a"')] if 'class="cast-a"' in page else page)
    head = {}
    hm = re.search(r"Katastrálne územie\s+(.+?)\s+Údaje platné k\s+([\d.]+(?: [\d:]+)?)", head_text)
    if hm:
        head["Katastrálne územie"] = hm.group(1)
        head["Údaje platné k"] = hm.group(2)

    legend: dict = {}
    for label, code, text in _TOOLTIP.findall(page):
        key = _LEGEND_KEYS.get(_text(label))
        if key and code:
            legend.setdefault(key, {})[code] = _text(text)

    cast_a = []
    part_a = _section(page, "cast-a", "cast-b")
    chunks = re.split(r"Parcely registra\s*[„\"“]?([CE])", part_a)
    for reg, chunk in zip(chunks[1::2], chunks[2::2]):
        columns = []
        for cells in rows_of(chunk):
            if cells and cells[0] == "Parcelné číslo":
                columns = [_HEADER_KEYS.get(c.replace("m2", "m²"), _HEADER_KEYS.get(c, c)) for c in cells]
                continue
            if cells and _PARCEL_NO.match(cells[0]) and columns:
                row = {"register": reg}
                for k, v in zip(columns, cells):
                    row[k] = v
                vm = re.match(r"^(\d+)", str(row.get("vymera_m2", "")))
                if vm:
                    row["vymera_m2"] = int(vm.group(1))
                for key, mapping in legend.items():
                    if row.get(key) in mapping:
                        row[key] = mapping[row[key]]
                cast_a.append(row)

    owners = []
    for cells in rows_of(_section(page, "cast-b", "cast-c")):
        nonempty = [c for c in cells if c]
        if len(nonempty) >= 3 and re.search(r"(?m)^\d+\.$", nonempty[0]):
            order = int(nonempty[0].splitlines()[-1].rstrip("."))
            person = "\n".join(ln for ln in nonempty[1].splitlines()[1:]) or nonempty[1]
            share = nonempty[2].splitlines()[-1].replace(" ", "")
            owners.append({"poradie": order, **split_person(person.replace("\n", ", ")), "podiel": share})
        elif nonempty and nonempty[0].startswith("Titul nadobudnutia") and owners:
            owners[-1]["titul_nadobudnutia"] = " ".join(nonempty[0].splitlines()[1:])
    cast_c = [" ".join(c for c in cells if c) for cells in rows_of(_section(page, "cast-c", None).split("</table>")[0])]
    cast_c = [t for t in cast_c if t]
    return {
        "zdroj_typ": "nahlad_lv",
        "lv": m.group(1),
        "hlavicka": head,
        "cast_a": cast_a,
        "cast_b": {"vlastnici": owners, "ine_opravnene_osoby": []},
        "cast_c": cast_c,
        "legenda": legend,
        "tarchy": "none" if not cast_c else "see cast_c",
        "poznamka": "LV preview from the ESKN portal: no administrators/tenants section; encumbrances as shown on the page.",
    }


PARSERS = (parse_nahlad, _text, _section)
