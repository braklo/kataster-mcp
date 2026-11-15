"""Server for smoke tests over stdio (started by smoke_stdio.py). The data directory is always a fresh temporary one.

    python scripts/smoke_server.py --live      real portal: the real limits directory (pause, daily quota, block)
    python scripts/smoke_server.py --offline   no network at all: a temporary limits directory too

Only a run that cannot reach any server may use a temporary limits directory, so --offline replaces the network
with a transport that refuses every request. There is no way to combine live requests with temporary limits.
"""
import sys
import tempfile
from pathlib import Path

import httpx

from kataster_mcp.http import Http
from kataster_mcp.policy import PUBLIC_POLICY
from kataster_mcp.server import create_server


def _refuse(request: httpx.Request):
    raise httpx.ConnectError(f"offline smoke: no request to {request.url.host}", request=request)


def build(mode: str, tmp: Path):
    if mode == "--live":
        return create_server(PUBLIC_POLICY, data_dir=tmp / "data")  # limits: the fixed per-user directory
    if mode == "--offline":
        http = Http(PUBLIC_POLICY, client=httpx.AsyncClient(transport=httpx.MockTransport(_refuse)))
        return create_server(PUBLIC_POLICY, data_dir=tmp / "data", limits_dir=tmp / "limity", http=http)
    raise SystemExit("usage: smoke_server.py --live | --offline")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    with tempfile.TemporaryDirectory(prefix="kataster-smoke-") as tmp:
        build(mode, Path(tmp)).run()
