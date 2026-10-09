"""kataster_lv: build the URL of an LV extract (výpis z listu vlastníctva). No network.

The extract is behind Google reCAPTCHA; the user opens it in a browser and solves it
(the server never bypasses CAPTCHA), saves the page and imports it with kataster_lv_import.
"""

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode

from pydantic import Field

from . import ku as ku_mod
from .cache import parser_version, ts_iso
from .envelope import Envelope, KatasterError
from .normalize import parcel_number
from .parsers import lv as lv_parser
from .parsers import lv_pdf
from .parsers import nahlad as nahlad_parser

GENERATE_URL = "https://kataster.skgeodesy.sk/Portal45/api/Bo/GeneratePrfPublic"
LV_VER = parser_version(*lv_parser.PARSERS, *nahlad_parser.PARSERS, *lv_pdf.PARSERS)
MAX_BYTES = 20 * 1024 * 1024


def lv_url(ku_kod: str, lv: str, output: str, parcels: list[tuple[str, str]]) -> str:
    params = {"prfNumber": lv, "cadastralUnitCode": ku_kod, "outputType": output}
    if parcels:
        by_reg: dict[str, list[str]] = {}
        for reg, num in parcels:
            by_reg.setdefault(reg, []).append(num)
        params["filter"] = "".join(f"parcels{reg}:{','.join(nums)};" for reg, nums in sorted(by_reg.items()))
    return f"{GENERATE_URL}?{urlencode(params, safe=':;,/')}"


def register(mcp, app) -> None:

    @mcp.tool()
    async def kataster_lv(
        ku: Annotated[str, Field(description="Cadastral unit (k.ú.): name or 6-digit code.")],
        lv: Annotated[str, Field(description="LV number (číslo listu vlastníctva), e.g. from kataster_vlastnici.")],
        parcely: Annotated[list[str] | None, Field(description="Optional parcels for a partial extract (čiastočný výpis), e.g. ['E 503']. Register prefix required.")] = None,
        format: Annotated[Literal["html", "pdf"], Field(description="html can be imported with kataster_lv_import; pdf is for printing.")] = "html",
    ) -> dict:
        """URL of the LV extract (výpis z LV) on the ESKN portal, plus what the user has to do.

        Makes no network request. The portal shows a reCAPTCHA ('Nie som robot') that only the user
        may solve; this tool never bypasses it. The extract is the only public source of
        encumbrances (ťarchy, časť C), administrators and tenants.
        """
        env = Envelope()
        try:
            unit = ku_mod.resolve(ku)
            if not lv.strip().isdigit():
                raise KatasterError("invalid_input", f"LV must be a number, not '{lv}'.")
            parsed = []
            for p in parcely or []:
                reg, num = parcel_number(p)
                if reg == "auto":
                    raise KatasterError("invalid_input", f"Give the register for '{p}', e.g. 'E {p}' or 'C {p}'.")
                parsed.append((reg, num))
            url = lv_url(unit["kod"], lv.strip(), format, parsed)
            env.source("eskn_vypis_lv", GENERATE_URL, oficialny=False, z_cache=False)
            if parsed:
                env.warn("Partial-extract filter syntax is verified only for one E parcel (parcelsE:503;); "
                         "for other combinations check that the extract lists the expected parcels.")
            return env.ok({
                "url": url,
                "ku": unit,
                "lv": lv.strip(),
                "ciastocny": bool(parsed),
                "postup": [
                    "Open the URL in your browser.",
                    "Tick 'Nie som robot' (reCAPTCHA) yourself; the server never does this.",
                    "Save the page (Ctrl+S, 'web page, HTML only') or the PDF.",
                    "Call kataster_lv_import with the saved file path (HTML or PDF).",
                ],
            })
        except KatasterError as e:
            raise env.fail(e) from e

    @mcp.tool()
    async def kataster_lv_import(
        path: Annotated[str | None, Field(description="Path to the LV extract saved from the browser as HTML or PDF.")] = None,
        html: Annotated[str | None, Field(description="The HTML itself, instead of a path.")] = None,
    ) -> dict:
        """Read an LV extract (výpis z LV) saved as HTML or PDF, or the portal's LV preview (Náhľad LV) as HTML.

        Returns the header (district, municipality, k.ú., date of data), part A (parcels with
        area, land type, location; codes translated by the legend), part B (owners with shares,
        acquisition titles, notes; administrators and tenants) and part C (encumbrances, ťarchy).
        zdroj_typ tells the input: vypis_lv_html, vypis_lv_pdf or nahlad_lv (the preview has no
        administrators/tenants section). Makes no network
        request. The result is kept in the local personal-data cache (owner-only permissions on POSIX).
        """
        env = Envelope()
        try:
            if bool(path) == bool(html):
                raise KatasterError("invalid_input", "Give either path or html.")
            if path:
                fp = Path(path).expanduser()
                if not fp.is_file():
                    raise KatasterError("not_found", f"File not found: {fp}")
                if fp.stat().st_size > MAX_BYTES:
                    raise KatasterError("invalid_input", f"File too large ({fp.stat().st_size} B).")
                raw = fp.read_bytes()
                origin = str(fp)
            else:
                raw = html.encode("utf-8")
                origin = "inline html"
            is_pdf = raw.startswith(b"%PDF")
            page = "" if is_pdf else raw.decode("utf-8", errors="replace")
            is_preview = "Náhľad listu vlastníctva" in page
            try:
                if is_pdf:
                    data = lv_pdf.parse_lv_pdf(raw)
                else:
                    data = nahlad_parser.parse_nahlad(page) if is_preview else lv_parser.parse_lv(page)
            except ValueError as e:
                if is_pdf:
                    raise KatasterError("parser_drift", f"Not a readable LV extract PDF: {e}.") from e
                known = is_preview or "LISTU VLASTN" in page
                raise KatasterError(
                    "parser_drift" if known else "invalid_input",
                    f"Not a readable LV page: {e}. Save the extract (GeneratePrfPublic) as 'HTML only', "
                    "or the 'Náhľad listu vlastníctva' page as 'Web page, complete'.",
                ) from e
            ku_head = data["hlavicka"].get("Katastrálne územie", "")
            ku_kod = ku_head.split(" ", 1)[0] if ku_head[:1].isdigit() else ku_head or "?"
            ts = app.osoby.put("lv_import", LV_VER, f"{ku_kod}|{data['lv']}", {"zdroj": origin, "lv": data})
            env.source("lv_import", origin, oficialny=False, z_cache=False, cas=ts_iso(ts))
            env.warn("Informative extract ('Výpis je nepoužiteľný na právne úkony'); data valid as of "
                     + data["hlavicka"].get("Údaje platné k", "?") + ".")
            return env.ok(data)
        except KatasterError as e:
            raise env.fail(e) from e
