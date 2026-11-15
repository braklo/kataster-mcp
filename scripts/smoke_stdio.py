"""Smoke test over real stdio MCP: start the server as a client would and call tools.

    uv run python scripts/smoke_stdio.py --offline '<tool>' '<json args>' ...
    uv run python scripts/smoke_stdio.py --live '<tool>' '<json args>' ...

The server (scripts/smoke_server.py) always gets a temporary data directory. --live talks to the real portal and
therefore counts into the real limits (pause, daily quota, block); --offline has no network and temporary limits.
"""
import asyncio, json, sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = str(Path(__file__).with_name("smoke_server.py"))


async def main(mode, calls):
    params = StdioServerParameters(command="uv", args=["run", "python", SERVER, mode])
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        init = await s.initialize()
        print("server:", init.server_info.name, init.server_info.version, mode)
        print("tools:", [t.name for t in (await s.list_tools()).tools])
        for name, args in calls:
            res = await s.call_tool(name, json.loads(args))
            text = res.content[0].text if res.content else ""
            print(f"--- {name} {args} isError={res.is_error}\n{text[:1500]}")

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("--live", "--offline"):
        sys.exit(__doc__)
    a = sys.argv[2:]
    asyncio.run(main(sys.argv[1], list(zip(a[::2], a[1::2]))))
