"""Make a test fixture from a saved LV extract PDF (výpis z LV): the text runs with positions as JSON,
with every owner text and every acquisition title replaced by fictitious text.

    uv run python scripts/anonymize_lv_pdf.py <saved.pdf> tests/fixtures/<name>.json

Never commit the input PDF; delete it after use. Refuses to write if any piece of an original owner or
title survives, checked twice: against the parsed result, and independently against the raw
layout text of the PDF (dates of birth and the words of every owner line).
"""
import json
import re
import sys

sys.path.insert(0, "src")
from kataster_mcp.parsers import lv_pdf  # noqa: E402

FAKE_OWNER = ["FIKTÍVNY Vlastník, Testovacia 1/1, Nikde, PSČ 000 00, SR, Dátum narodenia: 01.01.1900",
              "VYMYSLENÁ Osoba r. Skúšobná, Pokusná 2, Nikde, PSČ 000 00, SR, Dátum narodenia: 02.02.1900"]
FAKE_TITLE = "Zmluva č. TEST-1/2000 - 1/00"
KEEP = {"SR", "Vyšný Kubín", "PSČ 026 01", "Dátum narodenia", "Titul nadobudnutia"}


def anonymize(runs: list[dict]) -> tuple[list[dict], int]:
    lines = lv_pdf._lines(runs)
    out_ids = set()
    replaced = 0
    in_b = False
    title_next = False
    for ln in lines:
        text = " ".join(r["t"] for r in ln)
        if text.startswith("ČASŤ B"):
            in_b = True
        elif text.startswith("ČASŤ C"):
            in_b = False
        if not in_b:
            continue
        if lv_pdf._SHARE.match(ln[-1]["t"]) and len(ln) >= 2:  # owner line: keep order no. and share
            for r in ln[:-1]:
                if not (r["t"].isdigit() and r["x"] < 100):
                    r["t"] = FAKE_OWNER[replaced % len(FAKE_OWNER)]
                    out_ids.add(id(r))
            replaced += 1
            title_next = False
            continue
        if text.startswith("Titul nadobudnutia"):
            title_next = text.strip() == "Titul nadobudnutia:"
            if not title_next:
                ln[0]["t"] = "Titul nadobudnutia: " + FAKE_TITLE
                for r in ln[1:]:
                    r["t"] = ""
            continue
        if title_next and not lv_pdf._starts_new(ln):
            for r in ln:
                r["t"] = FAKE_TITLE if r is ln[0] else ""
            title_next = False
            continue
        if title_next is False and len(ln) == 1 and ln[0]["x"] > 80 and not lv_pdf._starts_new(ln) \
                and not ln[0]["t"].isdigit() and replaced:
            ln[0]["t"] = "Pokračovanie fiktívneho textu"  # wrapped owner text (continuation line)
    return [r for r in runs if r["t"]], replaced


def pieces_of(parsed: dict) -> set[str]:
    pieces = set()
    for o in parsed["cast_b"]["vlastnici"]:
        pieces.update(p.strip() for p in o["text"].split(",") if len(p.strip()) > 3)
        pieces.update(t for t in re.split(r"[\s,]+", o.get("titul_nadobudnutia", "")) if len(t) > 4 and any(c.isdigit() for c in t))
        if o.get("datum_narodenia"):
            pieces.add(o["datum_narodenia"])
    return pieces - KEEP


def independent_pieces(data: bytes) -> set[str]:
    """From the layout text, not from our parser: dates after 'narodenia' and words of owner lines."""
    from io import BytesIO

    from pypdf import PdfReader
    text = "\n".join(p.extract_text(extraction_mode="layout") for p in PdfReader(BytesIO(data)).pages)
    out = set(re.findall(r"narodenia:?\s*(\d{1,2}\.\d{1,2}\.\d{4})", text))
    for line in text.splitlines():
        if re.search(r"\b\d+\s*/\s*\d+\s*$", line) and ("narodenia" in line or "IČO" in line):
            out.update(w.strip(",") for w in line.split() if len(w.strip(",")) > 3 and not re.fullmatch(r"\d+/\d+", w))
    return out - KEEP - {"Dátum", "narodenia:"}


def main(src, dst):
    data = open(src, "rb").read()
    orig = lv_pdf.parse_lv_pdf(data)
    runs, n = anonymize(lv_pdf.pdf_runs(data))
    blob = json.dumps(runs, ensure_ascii=False)
    fake = " ".join(FAKE_OWNER) + " " + FAKE_TITLE
    pieces = {p for p in pieces_of(orig) if p not in fake}  # a piece equal to our fake text is no leak
    leaks = [p for p in pieces if p in blob]
    header_words = {w for v in orig["hlavicka"].values() for w in str(v).split()}  # names of the k.ú./obec/okres
    ind = independent_pieces(data) - header_words
    pieces -= header_words
    leaks2 = [p for p in ind if p in blob and p not in " ".join(FAKE_OWNER)]
    if leaks or leaks2 or n != len(orig["cast_b"]["vlastnici"]):
        shapes = sorted({re.sub(r"[^\W\d_]", "a", re.sub(r"\d", "9", x)) for x in leaks + leaks2})
        sys.exit(f"refusing: {len(leaks)} + {len(leaks2)} original pieces survived (shapes {shapes}), "
                 f"{n} owner lines replaced vs {len(orig['cast_b']['vlastnici'])} owners")
    check = lv_pdf.parse_runs(runs)
    if len(check["cast_b"]["vlastnici"]) != n or len(check["cast_a"]) != len(orig["cast_a"]):
        sys.exit("refusing: the anonymized runs do not parse to the same structure")
    open(dst, "w", encoding="utf8").write(json.dumps(runs, ensure_ascii=False, indent=0))
    print(f"wrote {dst}: {n} owners replaced, checked {len(pieces)} + {len(ind)} pieces")


if __name__ == "__main__":
    main(*sys.argv[1:3])
