"""Make a test fixture from a saved ESKN 'Náhľad listu vlastníctva' page: keep header and
parts A-C, replace every owner and acquisition title with fictitious text, drop scripts and
session state.

    uv run python scripts/anonymize_lv_nahlad.py <saved.html> tests/fixtures/<name>.html

Never commit the input file. Refuses to write if any piece of an original owner or title survives.
"""
import re
import sys

sys.path.insert(0, "src")
from kataster_mcp.parsers.nahlad import _TOOLTIP, parse_nahlad  # noqa: E402

FAKE_OWNER = ["FIKTÍVNY Vlastník, 01.01.1900, Testovacia 1/1, Nikde, 000 00",
              "VYMYSLENÁ Osoba r. Skúšobná, 02.02.1900, Pokusná 2, Nikde, 000 00"]
FAKE_TITLE = "Titul nadobudnutia<br>Zmluva č. TEST-1/2000 - 1/00<br>"


def main(src, dst):
    page = open(src, encoding="utf8").read()
    orig = parse_nahlad(page)
    start = page.rfind("<div", 0, page.find("Katastrálne územie"))
    end = page.find("</main>", page.find('class="cast-c"'))
    frag = page[start:end]
    frag = re.sub(r"<script.*?</script>", "", frag, flags=re.S)
    frag = re.sub(r'(name="javax\.faces\.ViewState"[^>]*value=")[^"]*', r"\1REMOVED", frag)
    frag = re.sub(r"(nahlad\?p1=)[^\"&]+", r"\1REMOVED", frag)
    i = 0

    def owner(m):
        nonlocal i
        out = m.group(1) + FAKE_OWNER[i % len(FAKE_OWNER)] + m.group(3)
        i += 1
        return out

    b0, b1 = frag.find('class="cast-b"'), frag.find('class="cast-c"')
    part_b = re.sub(r"(<a[^>]*>)([^<]*\d{2}\.\d{2}\.\d{4}[^<]*|[^<]*IČO[^<]*)(</a>)", owner, frag[b0:b1])
    part_b = re.sub(r'(<td[^>]*colspan="3"[^>]*>).*?(</td>)', lambda m: m.group(1) + FAKE_TITLE + m.group(2),
                    part_b, flags=re.S)
    frag = frag[:b0] + part_b + frag[b1:]
    # code legends live in PrimeFaces tooltips placed after </main>
    frag += "".join(f'<div class="ui-tooltip">{m.group(0)}</div>' for m in _TOOLTIP.finditer(page[end:]))
    pieces = set()
    for o in orig["cast_b"]["vlastnici"]:
        pieces.update(p.strip() for p in o["text"].split(",") if len(p.strip()) > 3)
        pieces.update(t for t in re.split(r"[\s,]+", o.get("titul_nadobudnutia", "")) if len(t) > 4 and any(c.isdigit() for c in t))
    pieces -= {"Vyšný Kubín"}
    leaks = [p for p in pieces if p in frag]
    if leaks:
        sys.exit(f"refusing: {len(leaks)} original pieces survived")
    with open(dst, "w", encoding="utf8") as f:
        f.write("<html><body><main>" + frag + "</main></body></html>\n")
    print(f"wrote {dst}: {i} owners replaced, checked {len(pieces)} pieces, {len(frag)} chars")


if __name__ == "__main__":
    main(*sys.argv[1:3])
