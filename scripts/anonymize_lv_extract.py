"""Make a test fixture from a saved LV extract (výpis z LV, HTML): every owner cell and every
'Titul nadobudnutia' cell is replaced by fictitious text; scripts and session data are dropped.

    uv run python scripts/anonymize_lv_extract.py <saved.html> tests/fixtures/<name>.html [first last]

For a block of flats give `first last`: only the first and the last units are kept (the header counts follow),
a pair of spouses becomes a fictitious pair and every text of part C is replaced with fictitious text.

Never commit the input file. Refuses to write if any piece of an original owner or title survives.
"""
import re
import sys

sys.path.insert(0, "src")
from kataster_mcp.parsers.lv import parse_lv  # noqa: E402

FAKE_OWNER = ["FIKTÍVNY Vlastník, Testovacia 1/1, Nikde, PSČ 000 00, SR, Dátum narodenia: 01.01.1900",
              "VYMYSLENÁ Osoba r. Skúšobná, Pokusná 2, Nikde, PSČ 000 00, SR, Dátum narodenia: 02.02.1900"]
FAKE_TITLE = "Titul nadobudnutia:</div><div class=\"black10\">Zmluva č. TEST-1/2000 - 1/00"
FAKE_PAIR = ("FIKTÍVNY Vlastník a VYMYSLENÁ Osoba r. Skúšobná, Testovacia 1/1, Nikde, PSČ 000 00, SR, "
             "Dátum narodenia: 01.01.1900, Dátum narodenia: 02.02.1900")
FAKE_C = "Záložné právo v prospech FIKTÍVNA banka, a.s., IČO: 00000000 na byt č. 1 - TEST-2/2000"


def keep_units(page: str, first: int, last: int) -> str:
    """Only the first `first` and the last `last` units (each starts with the table holding 'Vchod (číslo)');
    the 'Nebytové priestory' group header stays, and the counts in the group headers follow."""
    pos = [m.start() for m in re.finditer(r"Vchod \(číslo\)", page)]
    starts = [page.rfind("<table", 0, p) for p in pos]
    group = page.find("Nebytové priestory")
    group = page.rfind("<table", 0, group) if group > 0 else -1
    end = page.rfind("<table", 0, page.find("Legenda", starts[-1]))
    bounds = sorted({*starts, end, *([group] if group > 0 else [])})
    nxt = {a: b for a, b in zip(bounds, bounds[1:])}
    keep = [*range(first), *range(len(starts) - last, len(starts))]
    out, flats, premises, grouped = page[: starts[0]], 0, 0, False
    for k in keep:
        if group > 0 and starts[k] > group and not grouped:
            out += page[group: nxt[group]]
            grouped = True
        block = page[starts[k]: nxt[starts[k]]]
        flats += "Číslo bytu" in block
        premises += "Číslo nebytového priestoru" in block
        out += block
    out += page[end:]
    out = re.sub(r"(Počet bytov:\s*)\d+", lambda m: m.group(1) + str(flats), out, count=1)
    return re.sub(r"(Počet nebytových priestorov:\s*)\d+", lambda m: m.group(1) + str(premises), out, count=1)


def fake_part_c(page: str) -> str:
    c = page.find("ČASŤ C")
    tail = re.sub(r">([^<]*\S[^<]*)<", lambda m: m.group(0) if m.group(1).strip().startswith(("ČASŤ C", "Výpis je"))
                  else ">" + FAKE_C + "<", page[c:])
    return page[:c] + tail


def main(src, dst, first=None, last=None):
    page = open(src, encoding="utf8").read()
    orig = parse_lv(page)
    page = re.sub(r"<script.*?</script>", "", page, flags=re.S)
    page = re.sub(r"<APM_DO_NOT_TOUCH>.*?</APM_DO_NOT_TOUCH>", "", page, flags=re.S)
    if first is not None:
        page = fake_part_c(keep_units(page, int(first), int(last)))
    i = 0

    def owner(m):
        nonlocal i
        fake = FAKE_PAIR if m.group(2).count("Dátum narodenia") == 2 else FAKE_OWNER[i % len(FAKE_OWNER)]
        out = m.group(1) + fake + m.group(3)
        i += 1
        return out

    page = re.sub(r'(<td[^>]*class="black10Bold"[^>]*>)([^<]*Dátum narodenia[^<]*|[^<]*IČO:[^<]*)(</td>)', owner, page)
    page = re.sub(r"Titul nadobudnutia:</div>.*?(?=</td>)", FAKE_TITLE + "</div>", page, flags=re.S)
    pieces = set()
    for o in orig["cast_b"]["vlastnici"]:
        pieces.update(p.strip() for p in o["text"].split(",") if len(p.strip()) > 3)
        pieces.update(t for t in re.split(r"[\s,]+", o.get("titul_nadobudnutia", "")) if len(t) > 4 and any(ch.isdigit() for ch in t))
        if o.get("datum_narodenia"):
            pieces.add(o["datum_narodenia"])
        for key in ("poznamka", "ine_udaje"):
            pieces.update(t for t in re.split(r"[\s,]+", o.get(key) or "") if len(t) > 4 and t != "zápisu")
    if first is not None:  # part C is replaced text by text: no original row may survive
        pieces.update(line for line in orig["cast_c"] if len(line) > 4)
    pieces = {p for p in pieces if len(p) > 3}  # e.g. a birth date given as "-"
    pieces -= {"SR", "Vyšný Kubín", "PSČ 026 01"}  # town/zip of the k.ú. appear in the header anyway
    # header and part A are public (town names of owners' addresses appear there): check parts B and C only,
    # without the group header counts ("Počet vlastníkov") whose words an owner's name may contain
    checked = re.sub(r"Počet (?:bytov|nebytových priestorov|vlastníkov):\s*\d+", "", page[page.find("ČASŤ B"):])
    leaks = [p for p in pieces if p in checked]
    if leaks:
        sys.exit(f"refusing: {len(leaks)} original pieces survived")
    open(dst, "w", encoding="utf8").write(page)
    print(f"wrote {dst}: {i} owners replaced, checked {len(pieces)} pieces")


if __name__ == "__main__":
    main(*sys.argv[1:5])
