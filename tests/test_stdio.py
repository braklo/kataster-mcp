"""Real local stdio handshake and tool calls; synthetic inputs, data and limits in tmp_path.

The server is started through create_server with limits_dir in code (not `python -m kataster_mcp.server`), so the
test never touches the real per-user limits directory."""

import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from kataster_mcp import __version__

LAUNCH = ("import sys; from pathlib import Path; from kataster_mcp.server import create_server, PUBLIC_POLICY; "
          "create_server(PUBLIC_POLICY, data_dir=Path(sys.argv[1]), limits_dir=Path(sys.argv[2])).run()")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_stdio_discovery_and_incomplete_lv(tmp_path):
    params = StdioServerParameters(command=sys.executable,
                                   args=["-c", LAUNCH, str(tmp_path / "data"), str(tmp_path / "limity")])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        init = await client.initialize()
        assert init.server_info.version == __version__
        tools = await client.list_tools()
        assert "kataster_lv_import" in {tool.name for tool in tools.tools}
        result = await client.call_tool("kataster_lv_import", {"html": "<p>VÝPIS Z LISTU VLASTNÍCTVA č. 1</p>"})
        assert not result.is_error
        out = result.structured_content or json.loads(result.content[0].text)
        assert out["data"]["tarchy"] == "unknown" and out["varovania"]
        result = await client.call_tool("kataster_osoba_prehlad", {"ico": "abc"})
        assert result.is_error
    assert (tmp_path / "data" / "osoby.sqlite").exists()
    assert (tmp_path / "limity" / "stav.sqlite").exists()
