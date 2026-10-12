"""Local command interface with compact JSON and on-demand MCP discovery."""

import argparse
import asyncio
import json
import sys
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import __version__, ku, lv, osoba
from .cache import PERSONAL_TTL, Cache, configured_data_dir
from .envelope import Envelope, KatasterError


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise KatasterError("invalid_input", message)


def parser() -> Parser:
    root = Parser(
        prog="sk-kataster",
        description="Kataster: lokálne LV, katastrálne územia a MCP nástroje.",
    )
    root.add_argument(
        "--json", action="store_true", help="Kompaktný JSON; inak odsadený JSON."
    )
    root.add_argument(
        "--data-dir",
        type=Path,
        default=configured_data_dir(),
        help="Adresár lokálnych importov.",
    )
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "doctor", help="Verzia a lokálne nastavenie, bez siete alebo zápisu."
    )
    units = commands.add_parser(
        "ku", help="Katastrálne územia, bez siete."
    ).add_subparsers(dest="action", required=True)
    search = units.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    units.add_parser("resolve").add_argument("query")
    documents = commands.add_parser(
        "lv", help="Kontrola, import a URL výpisu LV."
    ).add_subparsers(dest="action", required=True)
    for action in ("inspect", "import"):
        command = documents.add_parser(action)
        command.add_argument("path", type=Path)
        command.add_argument(
            "--full",
            action="store_true",
            help="Aj parcely a osobné údaje; predvolene len súhrn.",
        )
        if action == "import":
            command.add_argument(
                "--dry-run", action="store_true", help="Prečítať bez zápisu do cache."
            )
    url = documents.add_parser("url")
    url.add_argument("--ku", required=True)
    url.add_argument("--lv", required=True)
    person = commands.add_parser(
        "person", help="Prehľad len z vlastných importov."
    ).add_subparsers(dest="action", required=True)
    overview = person.add_parser("overview")
    overview.add_argument("--name")
    overview.add_argument("--ico")
    overview.add_argument("--birth-date")
    overview.add_argument("--ku")
    commands.add_parser(
        "tools", help="Názvy nástrojov alebo schéma jedného."
    ).add_argument("name", nargs="?")
    raw = commands.add_parser(
        "call",
        help="MCP nástroj v lokálnom procese; môže volať portál alebo zapisovať súbory.",
    )
    raw.add_argument("name")
    raw.add_argument(
        "arguments", help="JSON objekt argumentov; schému zistí tools NAME."
    )
    return root


def compact(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "lv": data["lv"],
        "hlavicka": data["hlavicka"],
        "zdroj_typ": data["zdroj_typ"],
        "parcely": len(data["cast_a"]),
        "stavby": len(data.get("stavby", [])),
        "vlastnici": len(data["cast_b"]["vlastnici"]),
        "tarchy": data["tarchy"],
        "uplnost": data["uplnost"],
        **({"ulozenie": data["ulozenie"]} if "ulozenie" in data else {}),
    }


def inspect_file(path: Path, full: bool) -> dict[str, Any]:
    with path.expanduser().open("rb") as handle:
        raw = handle.read(lv.MAX_BYTES + 1)
    if len(raw) > lv.MAX_BYTES:
        raise KatasterError("invalid_input", "Súbor prekračuje limit importu.")
    data = lv.parse_page(raw)
    env = Envelope()
    env.source(lv.IMPORT, str(path), oficialny=False, z_cache=False)
    env.warn(
        "Rozsah výpisu nie je overený ako úplný. Stav tiarch sa týka iba prečítanej časti C."
    )
    if data["tarchy"] == "unknown":
        env.warn("Chýbajúca alebo prázdna časť C nepotvrdzuje neprítomnosť tiarch.")
    return env.ok(data if full else compact(data))


def with_cache(args: argparse.Namespace) -> dict[str, Any]:
    cache = Cache(args.data_dir / "osoby.sqlite", private=True, max_age_s=PERSONAL_TTL)
    try:
        if args.command == "lv":
            result = lv.import_lv(cache, path=str(args.path))
            if not args.full and "cast_a" in result["data"]:
                result["data"] = compact(result["data"])
            return result
        ico = osoba.norm_ico(args.ico)
        want = osoba.tokens(args.name or "")
        if ico is None and not want:
            raise KatasterError("invalid_input", "Zadajte --name alebo --ico.")
        result = osoba.overview(
            SimpleNamespace(osoby=cache),
            want,
            osoba.norm_date(args.birth_date),
            ku.resolve(args.ku)["kod"] if args.ku else None,
            ico,
        )
        env = Envelope()
        env.warn(osoba.NOT_SUPIS)
        return env.ok(result)
    finally:
        cache.db.close()


async def mcp_command(args: argparse.Namespace, data_dir: Path) -> dict[str, Any]:
    from .server import create_server

    server = create_server(data_dir=data_dir)
    try:
        if args.command == "tools":
            tools = await server.list_tools()
            if args.name:
                tool = next((tool for tool in tools if tool.name == args.name), None)
                if tool is None:
                    raise KatasterError("not_found", "Nástroj sa nenašiel.")
                return Envelope().ok(tool.model_dump(by_alias=True, exclude_none=True))
            return Envelope().ok({"tools": [tool.name for tool in tools]})
        arguments = json.loads(args.arguments)
        if not isinstance(arguments, dict):
            raise KatasterError("invalid_input", "Argumenty musia byť JSON objekt.")
        result = await server.call_tool(args.name, arguments)
        return result.structured_content or json.loads(result.content[0].text)
    finally:
        await server.app_state.http.aclose()
        server.app_state.cache.db.close()
        server.app_state.osoby.db.close()


def execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "doctor":
        return Envelope().ok(
            {
                "version": __version__,
                "sdk": version("mcp"),
                "mode": "local",
                "auth": "not_required",
                "network_checked": False,
                "data_dir": str(args.data_dir),
            }
        )
    if args.command == "ku":
        if args.action == "resolve":
            return Envelope().ok(ku.resolve(args.query))
        if not 1 <= args.limit <= 100:
            raise KatasterError("invalid_input", "Limit musí byť 1 až 100.")
        hits = ku.search(args.query)
        return Envelope().ok({"items": hits[: args.limit], "total": len(hits)})
    if args.command == "lv":
        if args.action == "url":
            if not args.lv.isascii() or not args.lv.isdigit():
                raise KatasterError("invalid_input", "Číslo LV musí byť číselné.")
            return Envelope().ok(
                {"url": lv.lv_url(ku.resolve(args.ku)["kod"], args.lv, "html", [])}
            )
        if args.action == "inspect" or args.dry_run:
            result = inspect_file(args.path, args.full)
            if args.action == "import":
                result["data"]["dry_run"] = True
            return result
        return with_cache(args)
    if args.command == "person":
        return with_cache(args)
    if args.command == "tools":
        with TemporaryDirectory(prefix="kataster-tools-") as directory:
            return asyncio.run(mcp_command(args, Path(directory)))
    return asyncio.run(mcp_command(args, args.data_dir))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    code = 0
    try:
        result = execute(parser().parse_args(argv))
    except ToolError as error:
        try:
            result = json.loads(str(error))
        except ValueError:
            result = {
                "chyba": {"typ": "invalid_input", "sprava": "Volanie nástroja zlyhalo."}
            }
        code = 1
    except (KatasterError, OSError, ValueError) as error:
        kind = (
            error.kind
            if isinstance(error, KatasterError)
            else (
                "not_found" if isinstance(error, FileNotFoundError) else "invalid_input"
            )
        )
        result = {
            "chyba": {"typ": kind, "sprava": str(error)},
            "zdroje": [],
            "varovania": [],
        }
        code = 1
    print(
        json.dumps(result, ensure_ascii=False, indent=None if "--json" in argv else 2)
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
