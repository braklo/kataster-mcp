"""Make a test fixture from a saved LV extract (výpis z LV, HTML): every owner cell and every
'Titul nadobudnutia' cell is replaced by fictitious text; scripts and session data are dropped.

    uv run python scripts/anonymize_lv_extract.py <saved.html> tests/fixtures/<name>.html

Never commit the input file. Refuses to write if any piece of an original owner or title survives.
"""
import re
import sys

sys.path.insert(0, "src")
from kataster_mcp.parsers.lv import parse_lv  # noqa: E402

FAKE_OWNER = ["FIKTÍVNY Vlastník, Testovacia 1/1, Nikde, PSČ 000 00, SR, Dátum narodenia: 01.01.1900",
              "VYMYSLENÁ Osoba r. Skúšobná, Pokusná 2, Nikde, PSČ 000 00, SR, Dátum narodenia: 02.02.1900"]
FAKE_TITLE = "Titul nadobudnutia:</div><div class=\"black10\">Zmluva č. TEST-1/2000 - 1/00"


def main(src, dst):
    page = open(src, encoding="utf8").read()
    orig = parse_lv(page)
    page = re.sub(r"<script.*?</script>", "", page, flags=re.S)
    page = re.sub(r"<APM_DO_NOT_TOUCH>.*?</APM_DO_NOT_TOUCH>", "", page, flags=re.S)
    i = 0

    def owner(m):
        nonlocal i
        out = m.group(1) + FAKE_OWNER[i % len(FAKE_OWNER)] + m.group(3)
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
    pieces -= {"SR", "Vyšný Kubín", "PSČ 026 01"}  # town/zip of the k.ú. appear in the header anyway
    leaks = [p for p in pieces if p in page]
    if leaks:
        sys.exit(f"refusing: {len(leaks)} original pieces survived")
    open(dst, "w", encoding="utf8").write(page)
    print(f"wrote {dst}: {i} owners replaced, checked {len(pieces)} pieces")


if __name__ == "__main__":
    main(*sys.argv[1:3])
