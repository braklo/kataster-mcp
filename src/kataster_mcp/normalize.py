"""Normalisation of user input: cadastral unit names and parcel numbers."""

import re
import unicodedata

from .envelope import KatasterError

_PARCEL_RE = re.compile(r"^\d+(/\d+)?$")
_REGISTER_PREFIX = re.compile(r"^(?:parc(?:ela)?\.?\s*(?:č\.?|c\.?)?\s*)?(?:(?:KN|reg(?:ister)?)[\s-]*)?([CE])(?:[\s-]*KN)?[\s.:-]+", re.I)


def fold(text: str) -> str:
    """Lower-case, strip diacritics, collapse whitespace and hyphens."""
    s = unicodedata.normalize("NFKD", text)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[\s\-–]+", " ", s.lower())
    return s.strip()


def parcel_number(text: str, register: str = "auto") -> tuple[str, str]:
    """Return (register, number). Register is 'C', 'E' or 'auto'.

    Accepts '503', '503 / 1', 'E 503', 'C-KN 660/66', 'parc. č. 503'.
    """
    s = text.strip()
    m = _REGISTER_PREFIX.match(s)
    reg = register.upper() if register else "AUTO"
    if m:
        found = m.group(1).upper()
        if reg not in ("AUTO", found):
            raise KatasterError("invalid_input", f"Parcel '{text}' says register {found}, but register={register}.")
        reg = found
        s = s[m.end():]
    s = re.sub(r"\s*/\s*", "/", s.strip())
    if not _PARCEL_RE.match(s):
        raise KatasterError("invalid_input", f"Not a parcel number: '{text}'. Expected e.g. 503 or 660/66.")
    if reg not in ("C", "E", "AUTO"):
        raise KatasterError("invalid_input", f"register must be C, E or auto, not '{register}'.")
    return ("auto" if reg == "AUTO" else reg), s
