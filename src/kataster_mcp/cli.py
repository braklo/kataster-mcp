"""sk-kataster: local command interface with compact JSON.

doctor, ku, lv url, lv inspect and lv import --dry-run need no server and make no request. lv import, person
overview, tools and call run the MCP tools in this process through create_server, so call shares the pause between
requests, the daily owners cap and the portal block with every other process (fixed per-user limits directory).
--data-dir / KATASTER_DATA_DIR move only the data directory (cache, imports), never the limits.
"""

import argparse
import asyncio
import json
import sqlite3
import sys
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import __version__, ku, lv
from .cache import configured_data_dir, default_limits_dir, today
from .envelope import Envelope, KatasterError
from .policy import PUBLIC_POLICY


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise KatasterError("invalid_input", message)


def parser() -> Parser:
    root = Parser(prog="sk-kataster", description="Kataster: local LV files, cadastral units and the MCP tools.")
    root.add_argument("--json", action="store_true", help="Compact JSON; indented otherwise.")
    root.add_argument("--data-dir", type=Path, default=None,
                      help="Data directory (cache, imports); default KATASTER_DATA_DIR or the per-user one. "
                           "The limits directory never moves.")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Version, directories and today's owners quota; no network, no writes.")
    units = commands.add_parser("ku", help="Cadastral units, no network.").add_subparsers(dest="action", required=True)
    search = units.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    units.add_parser("resolve").add_argument("query")
    documents = commands.add_parser("lv", help="Read, import and build the URL of an LV extract.").add_subparsers(
        dest="action", required=True)
    for action in ("inspect", "import"):
        command = documents.add_parser(action)
        command.add_argument("path", type=Path)
        command.add_argument("--full", action="store_true", help="Also parcels and personal data; summary by default.")
        if action == "import":
            command.add_argument("--dry-run", action="store_true", help="Read without storing.")
    url = documents.add_parser("url")
    url.add_argument("--ku", required=True)
    url.add_argument("--lv", required=True)
    person = commands.add_parser("person", help="Overview from your own imports only.").add_subparsers(
        dest="action", required=True)
    overview = person.add_parser("overview")
    overview.add_argument("--surname")
    overview.add_argument("--first-name")
    overview.add_argument("--name", help="Legal person: name.")
    overview.add_argument("--ico")
    overview.add_argument("--birth-date")
    overview.add_argument("--ku")
    commands.add_parser("tools", help="Tool names, or the schema of one.").add_argument("name", nargs="?")
    raw = commands.add_parser("call", help="Run one MCP tool in this process; it may query the portal (same pause "
                                           "and daily cap as the server) or write files.")
    raw.add_argument("name")
    raw.add_argument("arguments", help="JSON object; see tools NAME for the schema.")
    return root


def compact(data: dict[str, Any]) -> dict[str, Any]:
    """Summary of a parsed LV without parcels or personal data."""
    return {
        "lv": data["lv"],
        "hlavicka": data["hlavicka"],
        "zdroj_typ": data["zdroj_typ"],
        "parcely": len(data.get("cast_a") or []),
        "stavby": len(data.get("stavby") or []),
        "vlastnici": len(data["cast_b"]["vlastnici"]),
        "tarchy": data.get("tarchy"),
        **({"tarchy_poznamka": data["tarchy_poznamka"]} if "tarchy_poznamka" in data else {}),
        "uplnost": data.get("uplnost"),
        **({"ulozenie": data["ulozenie"]} if "ulozenie" in data else {}),
    }


def inspect_file(path: Path, full: bool) -> dict[str, Any]:
    with path.expanduser().open("rb") as handle:
        raw = handle.read(lv.MAX_BYTES + 1)
    if len(raw) > lv.MAX_BYTES:
        raise KatasterError("invalid_input", f"File too large (over {lv.MAX_BYTES} B).")
    data = lv.parse_page(raw)
    env = Envelope()
    env.source(lv.IMPORT, str(path), oficialny=False, z_cache=False)
    lv._coverage_warnings(env, data["lv"], data)
    return env.ok(data if full else compact(data))


def quota_today(limits_dir: Path) -> dict[str, Any]:
    """Today's owners quota read from the fixed limits state, read-only: nothing is created when it is missing."""
    used = 0
    state = limits_dir / "stav.sqlite"
    if state.exists():
        db = sqlite3.connect(f"{state.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = db.execute("SELECT COUNT(*) FROM kvota WHERE den=?", (today(),)).fetchone()
            used = row[0] if row else 0
        except sqlite3.OperationalError:  # no quota table yet
            used = 0
        finally:
            db.close()
    return {"den": today(), "pouzite": used, "limit": PUBLIC_POLICY.owners_daily_hard}


def doctor(data_dir: Path, limits_dir: Path) -> dict[str, Any]:
    return Envelope().ok({
        "version": __version__,
        "sdk": version("mcp"),
        "network_checked": False,
        "data_dir": str(data_dir),
        "limits_dir": str(limits_dir),
        "kvota_vlastnici": quota_today(limits_dir),
    })


def _structured(result: Any) -> dict[str, Any]:
    """The tool's envelope from call_tool, whatever shape the SDK returns."""
    structured = getattr(result, "structured_content", None)  # SDK 2.x CallToolResult
    if structured is None and isinstance(result, tuple):  # SDK 1.x (content, structured)
        structured = result[1]
    if structured:
        return structured.get("result", structured)
    content = getattr(result, "content", None) or (result[0] if isinstance(result, tuple) else result)
    return json.loads(content[0].text)


async def run_tool(name: str, arguments: dict[str, Any], data_dir: Path, limits_dir: Path | None) -> dict[str, Any]:
    from .server import create_server

    server = create_server(PUBLIC_POLICY, data_dir=data_dir, limits_dir=limits_dir)
    app = server.app_state
    try:
        if name == "":
            tools = await server.list_tools()
            return Envelope().ok({"tools": [t.name for t in tools]})
        if name.startswith("schema:"):
            wanted = name.removeprefix("schema:")
            tool = next((t for t in await server.list_tools() if t.name == wanted), None)
            if tool is None:
                raise KatasterError("not_found", f"No tool named '{wanted}'.")
            return Envelope().ok(tool.model_dump(by_alias=True, exclude_none=True))
        return _structured(await server.call_tool(name, arguments))
    finally:
        await app.http.aclose()
        for cache in (app.cache, app.osoby):
            cache.db.close()


def execute(args: argparse.Namespace, limits_dir: Path | None = None) -> dict[str, Any]:
    data_dir = args.data_dir or configured_data_dir()
    if args.command == "doctor":
        return doctor(data_dir, limits_dir or default_limits_dir())
    if args.command == "ku":
        if args.action == "resolve":
            return Envelope().ok(ku.resolve(args.query))
        if not 1 <= args.limit <= 100:
            raise KatasterError("invalid_input", "Limit must be 1 to 100.")
        hits = ku.search(args.query)
        return Envelope().ok({"items": hits[: args.limit], "total": len(hits)})
    if args.command == "lv":
        if args.action == "url":
            if not args.lv.isascii() or not args.lv.isdigit():
                raise KatasterError("invalid_input", "LV must be a number.")
            return Envelope().ok({"url": lv.lv_url(ku.resolve(args.ku)["kod"], args.lv, "html", [])})
        if args.action == "inspect" or args.dry_run:
            result = inspect_file(args.path, args.full)
            if args.action == "import":
                result["data"]["dry_run"] = True
            return result
        result = asyncio.run(run_tool("kataster_lv_import", {"path": str(args.path)}, data_dir, limits_dir))
        if not args.full and "cast_a" in (result.get("data") or {}):
            result["data"] = compact(result["data"])
        return result
    if args.command == "person":
        arguments = {"priezvisko": args.surname, "meno": args.first_name, "nazov": args.name, "ico": args.ico,
                     "datum_narodenia": args.birth_date, "ku": args.ku}
        return asyncio.run(run_tool("kataster_osoba_prehlad", {k: v for k, v in arguments.items() if v is not None},
                                    data_dir, limits_dir))
    if args.command == "tools":
        with TemporaryDirectory(prefix="kataster-tools-") as directory:  # listing needs no data
            return asyncio.run(run_tool(f"schema:{args.name}" if args.name else "", {}, Path(directory), limits_dir))
    arguments = json.loads(args.arguments)
    if not isinstance(arguments, dict):
        raise KatasterError("invalid_input", "Arguments must be a JSON object.")
    return asyncio.run(run_tool(args.name, arguments, data_dir, limits_dir))


def main(argv: list[str] | None = None, *, limits_dir: Path | None = None) -> int:
    """limits_dir is for tests only (code, never an option or environment variable)."""
    argv = sys.argv[1:] if argv is None else argv
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    code = 0
    try:
        result = execute(parser().parse_args(argv), limits_dir)
    except ToolError as error:
        text = str(error)
        try:
            result = json.loads(text[text.index("{"):])
        except ValueError:
            result = {"chyba": {"typ": "invalid_input", "sprava": text}, "zdroje": [], "varovania": []}  # e.g. bad arguments
        code = 1
    except (KatasterError, OSError, ValueError) as error:
        kind = error.kind if isinstance(error, KatasterError) else (
            "not_found" if isinstance(error, FileNotFoundError) else "invalid_input")
        message = error.message if isinstance(error, KatasterError) else str(error)
        result = {"chyba": {"typ": kind, "sprava": message}, "zdroje": [], "varovania": []}
        code = 1
    print(json.dumps(result, ensure_ascii=False, indent=None if "--json" in argv else 2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
