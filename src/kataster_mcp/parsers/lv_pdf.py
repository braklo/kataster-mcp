"""Parser of the LV extract (výpis z listu vlastníctva) saved as PDF (GeneratePrfPublic?outputType=pdf).

pypdf (pure Python) gives text runs with their position; a run is usually one table cell. The runs are
rebuilt into the same rows of cells that the HTML parser produces, so `parse_rows` is shared:
- runs on the same baseline form a line; a line is a row of cells;
- the part A table header spans several lines: its runs are merged into columns by x, and every data
  cell goes to the column whose header starts left of it (empty cells survive as "");
- in part B the order number of an owner is vertically centred in its block, i.e. on another line:
  it is attached to the owner above it; a label line ending with ':' takes the following line(s).

Built on a PDF printed from the HTML extract and checked on the portal's own PDF (2026-10-09): there the
date of birth wraps to the next line and page furniture sits off the page or in the bottom margin; both
are handled, and the result equals the HTML extract of the same LV field by field.
"""

import io
import re

from . import lv as lv_html

Y_TOL = 1.5
HEADER_GAP = 30.0
COL_TOL = 8.0
_SHARE = re.compile(r"^\d+\s*/\s*\d+$")
_LABELS = ("Titul nadobudnutia:", "Iné údaje:", "Poznámky:", "Poznámka:")
_STARTS = ("Titul nadobudnutia", "Iné údaje", "Poznámk", "Správca", "Nájomca", "Iná oprávnená osoba", "ČASŤ")
_PAGE_NO = re.compile(r"^\d+\s+(z|/|of)(\s+\d+)?$")  # "1 z", "1 / 3"; never "518/1" or a share "1/2"
_DROP = re.compile(r"^(Strana|Str\.)\s*\d+(\s*(/|z)\s*\d+)?$")


def pdf_runs(data: bytes) -> list[dict]:
    """[{p: page, y, x, t: text}] in reading order (page, top to bottom, left to right)."""
    from pypdf import PdfReader

    runs: list[dict] = []
    for pn, page in enumerate(PdfReader(io.BytesIO(data)).pages):

        def visit(text, cm, tm, font, size, pn=pn):
            t = " ".join(text.split())
            if t:
                x = cm[0] * tm[4] + cm[2] * tm[5] + cm[4]
                y = cm[1] * tm[4] + cm[3] * tm[5] + cm[5]
                runs.append({"p": pn, "y": round(y, 1), "x": round(x, 1), "t": t})

        page.extract_text(visitor_text=visit)
        width = float(page.mediabox.width)
        runs = [r for r in runs if r["p"] != pn or not _decoration(r, width)]
    runs.sort(key=lambda r: (r["p"], -r["y"], r["x"]))
    return runs


def _decoration(r: dict, width: float) -> bool:
    """Page furniture of the portal PDF (seen 2026-10-09): runs placed off the page or on its very edge and
    page numbers like '1 z'. Data rows may sit low on a page, so position alone is not enough."""
    return r["x"] < 0 or r["x"] > width or r["y"] < 5 or bool(_PAGE_NO.match(r["t"]))


def _lines(runs: list[dict]) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for r in runs:
        if lines and lines[-1][0]["p"] == r["p"] and abs(lines[-1][0]["y"] - r["y"]) <= Y_TOL:
            lines[-1].append(r)
        else:
            lines.append([r])
    for ln in lines:
        ln.sort(key=lambda r: r["x"])
    return [ln for ln in lines if not _DROP.match(" ".join(r["t"] for r in ln))]


def _columns(band: list[list[dict]]) -> list[tuple[float, str]]:
    """Header lines -> [(left x, title)] with multi-line titles merged by x."""
    cols: list[dict] = []
    for r in sorted((r for ln in band for r in ln), key=lambda r: (-r["y"], r["x"])):
        hit = next((c for c in cols if abs(c["x"] - r["x"]) < HEADER_GAP), None)
        if hit:
            hit["parts"].append(r["t"])
            hit["x"] = min(hit["x"], r["x"])
        else:
            cols.append({"x": r["x"], "parts": [r["t"]]})
    cols.sort(key=lambda c: c["x"])
    return [(c["x"], " ".join(c["parts"])) for c in cols]


def _place(line: list[dict], cols: list[tuple[float, str]]) -> list[str]:
    cells = [""] * len(cols)
    for r in line:
        i = max((k for k, (x, _) in enumerate(cols) if x - COL_TOL <= r["x"]), default=0)
        cells[i] = (cells[i] + " " + r["t"]).strip()
    return cells


def rows_from_runs(runs: list[dict]) -> list[list[str]]:
    lines = _lines(runs)
    rows: list[list[str]] = []
    section = None
    cols: list[tuple[float, str]] = []
    band: list[list[dict]] | None = None
    owner: list[str] | None = None
    i = 0
    while i < len(lines):
        ln = lines[i]
        cells = [r["t"] for r in ln]
        text = " ".join(cells)
        i += 1
        if text.startswith("ČASŤ A"):
            section = "A"
        elif text.startswith("ČASŤ B"):
            section, owner = "B", None
        elif text.startswith("ČASŤ C"):
            section = "C"
        elif text == "Legenda":
            section = "A-legend"

        if section == "A":
            if re.search(r"Parcely registra", text):
                rows.append(cells)
                band, cols = [], []
                continue
            if band is not None:
                if lv_html._PARCEL_NO.match(cells[0]):
                    cols = _columns(band)
                    rows.append([t for _, t in cols])
                    band = None
                else:
                    band.append(ln)
                    continue
            if cols and lv_html._PARCEL_NO.match(cells[0]):
                rows.append(_place(ln, cols))
                continue
        if section == "B":
            if _SHARE.match(cells[-1]) and len(cells) >= 2:
                lead = cells[0] if cells[0].isdigit() and len(cells) >= 3 else ""
                owner = [lead, " ".join(cells[1:-1] if lead else cells[:-1]), cells[-1].replace(" ", "")]
                rows.append(owner)
                continue
            if len(cells) == 1 and cells[0].isdigit() and ln[0]["x"] < 100:
                if owner is not None and not owner[0]:
                    owner[0] = cells[0]
                continue
            if owner is not None and rows and rows[-1] is owner and len(cells) == 1 and not _starts_new(ln) \
                    and text not in _LABELS:
                owner[1] = f"{owner[1]} {text}"  # wrapped owner text (portal PDF: date of birth on the next line)
                continue
            if text in _LABELS:
                parts = [text]
                while i < len(lines) and not _starts_new(lines[i]):
                    nxt = lines[i]
                    if len(nxt) == 1 and nxt[0]["t"].isdigit() and nxt[0]["x"] < 100:
                        if owner is not None and not owner[0]:
                            owner[0] = nxt[0]["t"]
                    else:
                        parts.append(" ".join(r["t"] for r in nxt))
                    i += 1
                rows.append([" ".join(parts)])
                continue
        rows.append(cells)
    return rows


def _number_owners(rows: list[list[str]]) -> int:
    """Owners whose order number was not found get their position; returns how many."""
    guessed = 0
    k = 0
    for r in rows:
        if len(r) == 3 and _SHARE.match(r[2]):
            k += 1
            if not r[0]:
                r[0] = str(k)
                guessed += 1
    return guessed


def _starts_new(ln: list[dict]) -> bool:
    text = " ".join(r["t"] for r in ln)
    return text.startswith(_STARTS) or bool(_SHARE.match(ln[-1]["t"]))


def parse_runs(runs: list[dict]) -> dict:
    rows = rows_from_runs(runs)
    guessed = _number_owners(rows)
    out = {**lv_html.parse_rows(rows), "zdroj_typ": "vypis_lv_pdf"}
    if guessed:
        out["poznamka_pdf"] = f"{guessed} owner order number(s) not found in the PDF; taken from their position."
    return out


def parse_lv_pdf(data: bytes) -> dict:
    if not data.startswith(b"%PDF"):
        raise ValueError("not a PDF")
    from pypdf.errors import PdfReadError

    try:
        runs = pdf_runs(data)
    except PdfReadError as e:
        raise ValueError(f"unreadable PDF: {e}") from e
    if not runs:
        raise ValueError("the PDF has no text layer (scanned?)")
    return parse_runs(runs)


PARSERS = (parse_lv_pdf, parse_runs, pdf_runs, rows_from_runs, _number_owners, _lines, _columns, _place, _starts_new)
