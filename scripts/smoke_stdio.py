"""Smoke test over real stdio MCP: start the server as a client would and call tools.

    uv run python scripts/smoke_stdio.py '<tool>' '<json args>' ...
"""
import asyncio, json, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main(calls):
    params = StdioServerParameters(command="uv", args=["run", "kataster-mcp"])
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        init = await s.initialize()
        print("server:", init.serverInfo.name, init.serverInfo.version)
        print("tools:", [t.name for t in (await s.list_tools()).tools])
        for name, args in calls:
            res = await s.call_tool(name, json.loads(args))
            text = res.content[0].text if res.content else ""
            print(f"--- {name} {args} isError={res.isError}\n{text[:1500]}")

if __name__ == "__main__":
    a = sys.argv[1:]
    asyncio.run(main(list(zip(a[::2], a[1::2]))))
