"""Response envelope and typed errors shared by all tools.

Every tool returns {data, zdroje, varovania}. Errors are raised as ToolError carrying
the same envelope with a `chyba` object instead of `data`.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError

ERROR_KINDS = {
    "blocked",
    "captcha_required",
    "parser_drift",
    "unavailable",
    "ambiguous",
    "limit_reached",
    "not_found",
    "invalid_input",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class KatasterError(Exception):
    def __init__(self, kind: str, message: str, *, coverage: Any = None, details: Any = None):
        assert kind in ERROR_KINDS, kind
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.coverage = coverage
        self.details = details


@dataclass
class Envelope:
    zdroje: list[dict] = field(default_factory=list)
    varovania: list[str] = field(default_factory=list)

    def source(self, id: str, url: str, *, oficialny: bool, z_cache: bool, cas: str | None = None) -> None:
        entry = {"id": id, "url": url, "oficialny": oficialny, "cas": cas or now_iso(), "z_cache": z_cache}
        if entry not in self.zdroje:
            self.zdroje.append(entry)

    def warn(self, text: str) -> None:
        if text not in self.varovania:
            self.varovania.append(text)

    def ok(self, data: Any) -> dict:
        return {"data": data, "zdroje": self.zdroje, "varovania": self.varovania}

    def fail(self, err: KatasterError) -> ToolError:
        chyba: dict[str, Any] = {"typ": err.kind, "sprava": err.message}
        if err.coverage is not None:
            chyba["coverage"] = err.coverage
        if err.details is not None:
            chyba["detail"] = err.details
        body = {"chyba": chyba, "zdroje": self.zdroje, "varovania": self.varovania}
        return ToolError(json.dumps(body, ensure_ascii=False))
