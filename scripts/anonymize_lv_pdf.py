"""Make a test fixture from a saved LV extract PDF (výpis z LV): the text runs with positions as JSON,
with every owner text and every acquisition title replaced by fictitious text.

    uv run python scripts/anonymize_lv_pdf.py <saved.pdf> tests/fixtures/<name>.json [first last]

For a block of flats give `first last`: only the first and the last units are kept (the header counts follow), a
pair of spouses becomes a fictitious pair, 'Iné údaje'/'Poznámky' of part B become 'Bez zápisu' and every text of
part C is replaced with fictitious text.

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
FAKE_PAIR = ("FIKTÍVNY Vlastník a VYMYSLENÁ Osoba r. Skúšobná, Testovacia 1/1, Nikde, PSČ 000 00, SR, "
             "Dátum narodenia: 01.01.1900, Dátum narodenia: 02.02.1900")
FAKE_C = "Záložné právo v prospech FIKTÍVNA banka, a.s., IČO: 00000000 na byt č. 1 - TEST-2/2000"


def _text(ln: list[dict]) -> str:
    return " ".join(r["t"] for r in ln)


def keep_units(runs: list[dict], first: int, last: int) -> list[dict]:
    """Only the first `first` and the last `last` units of part B (a unit starts at its 'Vchod (číslo)' line);
    the 'Nebytové priestory' group header stays and the counts follow."""
    lines = lv_pdf._lines(runs)
    starts = [k for k, ln in enumerate(lines) if ln[0]["t"].startswith("Vchod (číslo)")]
    group = next((k for k, ln in enumerate(lines) if ln[0]["t"].startswith("Nebytové priestory")), None)
    end = next(k for k in range(starts[-1], len(lines)) if _text(lines[k]) == "Legenda")
    bounds = sorted({*starts, end, *([group] if group is not None else [])})
    nxt = dict(zip(bounds, bounds[1:]))
    keep = [*range(first), *range(len(starts) - last, len(starts))]
    out, flats, premises, grouped = lines[: starts[0]], 0, 0, False
    for k in keep:
        if group is not None and starts[k] > group and not grouped:
            out += lines[group: nxt[group]]
            grouped = True
        block = lines[starts[k]: nxt[starts[k]]]
        head = _text(block[0])
        flats += "Číslo bytu" in head
        premises += "Číslo nebytového" in head
        out += block
    out += lines[end:]
    for ln in out:
        for r in ln:
            r["t"] = re.sub(r"(Počet bytov:\s*)\d+", lambda m: m.group(1) + str(flats), r["t"])
            r["t"] = re.sub(r"^(priestorov:\s*)\d+$", lambda m: m.group(1) + str(premises), r["t"])
            r["t"] = re.sub(r"(Počet nebytových priestorov:\s*)\d+", lambda m: m.group(1) + str(premises), r["t"])
    return [r for ln in out for r in ln]


def flats_pass(runs: list[dict]) -> list[dict]:
    """Pairs of spouses -> one fictitious pair; notes of part B -> 'Bez zápisu'; part C -> fictitious text."""
    lines = lv_pdf._lines(runs)
    section, k = None, 0
    while k < len(lines):
        ln, text = lines[k], _text(lines[k])
        if text.startswith("ČASŤ B"):
            section = "B"
        elif text.startswith("ČASŤ C"):
            section = "C"
        elif section == "C" and not text.startswith("Výpis je"):
            for r in ln:
                r["t"] = FAKE_C if r is ln[0] else ""
        elif section == "B" and lv_pdf._SHARE.match(ln[-1]["t"]) and len(ln) >= 2:
            j = k + 1
            while j < len(lines) and not lv_pdf._starts_new(lines[j]) and _text(lines[j]) not in lv_pdf._LABELS:
                j += 1
            if sum(_text(x).count("narodenia") for x in lines[k:j]) == 2:
                for r in ln[:-1]:
                    if not (r["t"].isdigit() and r["x"] < 100):
                        r["t"] = ""
                next(r for r in ln[:-1] if not (r["t"].isdigit() and r["x"] < 100) or r["t"] == "")["t"] = FAKE_PAIR
                for x in lines[k + 1: j]:
                    for r in x:
                        if not (r["t"].isdigit() and r["x"] < 100):
                            r["t"] = ""
                for r in ln:
                    r["pair"] = True
        elif section == "B" and text.startswith(("Iné údaje", "Poznámk")):
            label = "Iné údaje" if text.startswith("Iné údaje") else text.split(":", 1)[0]
            ln[0]["t"] = f"{label}: Bez zápisu"
            for r in ln[1:]:
                r["t"] = ""
            j = k + 1
            while j < len(lines) and not lv_pdf._starts_new(lines[j]) and not (
                    len(lines[j]) == 1 and lines[j][0]["t"].isdigit() and lines[j][0]["x"] < 100):
                for r in lines[j]:
                    r["t"] = ""
                j += 1
        k += 1
    return [r for r in runs if r["t"]]


def anonymize(runs: list[dict]) -> tuple[list[dict], int]:
    lines = lv_pdf._lines(runs)
    out_ids = set()
    replaced = 0
    in_b = False
    title_next = False
    in_unit = False  # unit header of a block of flats: labels and values, no personal data
    for ln in lines:
        text = " ".join(r["t"] for r in ln)
        if text.startswith("ČASŤ B"):
            in_b = True
        elif text.startswith("ČASŤ C"):
            in_b = False
        if not in_b:
            continue
        if text.startswith("Vchod (číslo)"):
            in_unit = True
        elif text.startswith(lv_pdf._STARTS):
            in_unit = False
        if in_unit:
            continue
        if lv_pdf._SHARE.match(ln[-1]["t"]) and len(ln) >= 2:  # owner line: keep order no. and share
            if ln[0].get("pair"):  # already a fictitious pair (flats_pass)
                replaced += 1
                title_next = False
                continue
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


def main(src, dst, first=None, last=None):
    data = open(src, "rb").read()
    orig = lv_pdf.parse_lv_pdf(data)
    runs = lv_pdf.pdf_runs(data)
    if first is not None:
        runs = flats_pass(keep_units(runs, int(first), int(last)))
        pruned = lv_pdf.parse_runs(json.loads(json.dumps(runs)))
    runs, n = anonymize(runs)
    for r in runs:
        r.pop("pair", None)
    blob = json.dumps(runs, ensure_ascii=False)
    if first is not None:  # header and part A are public (owners' towns appear there): check parts B and C only,
        # without the group header counts ("Počet vlastníkov") whose words an owner's name may contain
        b0 = next(k for k, r in enumerate(runs) if r["t"].startswith("ČASŤ B"))
        blob = re.sub(r"Počet (?:bytov|nebytových|vlastníkov)[^\"]*", "", json.dumps(runs[b0:], ensure_ascii=False))
    fake = " ".join(FAKE_OWNER) + " " + FAKE_TITLE
    pieces = {p for p in pieces_of(orig) if p not in fake}  # a piece equal to our fake text is no leak
    if first is not None:  # notes of owners and the whole of part C are replaced as well
        for o in orig["cast_b"]["vlastnici"]:
            for key in ("poznamka", "ine_udaje"):
                pieces.update(t for t in re.split(r"[\s,]+", o.get(key) or "") if len(t) > 4 and t != "zápisu")
        pieces.update(line for line in orig["cast_c"] if len(line) > 8)
    leaks = [p for p in pieces if p in blob]
    header_words = {w for v in orig["hlavicka"].values() for w in str(v).split()}  # names of the k.ú./obec/okres
    ind = independent_pieces(data) - header_words
    pieces -= header_words
    leaks2 = [p for p in ind if p in blob and p not in " ".join(FAKE_OWNER)]
    owners_expected = (sum(1 for o in pruned["cast_b"]["vlastnici"] if not o.get("bsm"))
                       + sum(1 for o in pruned["cast_b"]["vlastnici"] if o.get("bsm")) // 2) if first is not None \
        else len(orig["cast_b"]["vlastnici"])
    if leaks or leaks2 or n != owners_expected:
        shapes = sorted({re.sub(r"[^\W\d_]", "a", re.sub(r"\d", "9", x)) for x in leaks + leaks2})
        sys.exit(f"refusing: {len(leaks)} + {len(leaks2)} original pieces survived (shapes {shapes}), "
                 f"{n} owner lines replaced vs {owners_expected} expected")
    check = lv_pdf.parse_runs(json.loads(json.dumps(runs)))
    persons = len(pruned["cast_b"]["vlastnici"]) if first is not None else n
    if len(check["cast_b"]["vlastnici"]) != persons or len(check["cast_a"]) != len(orig["cast_a"]):
        sys.exit("refusing: the anonymized runs do not parse to the same structure")
    open(dst, "w", encoding="utf8").write(json.dumps(runs, ensure_ascii=False, indent=0))
    print(f"wrote {dst}: {n} owners replaced, checked {len(pieces)} + {len(ind)} pieces")


if __name__ == "__main__":
    main(*sys.argv[1:5])
