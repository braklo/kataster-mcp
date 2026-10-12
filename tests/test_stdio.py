"""Real local stdio handshake and tool calls; synthetic inputs and isolated state."""

import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from kataster_mcp import __version__


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_stdio_discovery_and_incomplete_lv(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "kataster_mcp.server"],
        env={**os.environ, "KATASTER_DATA_DIR": str(tmp_path / "data")},
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as client,
    ):
        init = await client.initialize()
        assert init.server_info.version == __version__
        tools = await client.list_tools()
        assert "kataster_lv_import" in {tool.name for tool in tools.tools}
        result = await client.call_tool(
            "kataster_lv_import", {"html": "<p>VÝPIS Z LISTU VLASTNÍCTVA č. 1</p>"}
        )
        assert not result.is_error
        out = result.structured_content or json.loads(result.content[0].text)
        assert out["data"]["tarchy"] == "unknown"
        assert out["varovania"]
        result = await client.call_tool("kataster_osoba_prehlad", {"ico": "abc"})
        assert result.is_error
    assert (tmp_path / "data" / "osoby.sqlite").exists()
