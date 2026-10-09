"""Behaviour that depends on the OS: data directory, file permissions, time zone."""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kataster_mcp import cache
from kataster_mcp.policy import PUBLIC_POLICY
from kataster_mcp.server import create_server

from test_core import _call


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_data_dir_per_platform():
    home = Path.home()
    assert cache.default_data_dir("win32", {"LOCALAPPDATA": r"C:\Users\x\AppData\Local"}) == \
        Path(r"C:\Users\x\AppData\Local") / "kataster-mcp"
    assert cache.default_data_dir("win32", {}) == home / "AppData" / "Local" / "kataster-mcp"
    assert cache.default_data_dir("darwin", {"XDG_DATA_HOME": "/ignored"}) == \
        home / "Library" / "Application Support" / "kataster-mcp"
    assert cache.default_data_dir("linux", {"XDG_DATA_HOME": "/x/data"}) == Path("/x/data/kataster-mcp")
    assert cache.default_data_dir("linux", {}) == home / ".local" / "share" / "kataster-mcp"


def test_restrict_is_a_noop_without_posix(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("chmod must not be called")

    monkeypatch.setattr(cache, "POSIX", False)
    monkeypatch.setattr(os, "chmod", boom)
    cache.restrict(tmp_path, 0o700)
    c = cache.Cache(tmp_path / "d" / "osoby.sqlite", private=True)  # no chmod on the way
    assert c.path.exists() and "ACL" in cache.protection()


def test_restrict_on_posix(tmp_path):
    if not cache.POSIX:
        pytest.skip("POSIX only")
    f = tmp_path / "f"
    f.write_text("x", encoding="utf-8")
    cache.restrict(f, 0o600)
    assert (f.stat().st_mode & 0o777) == 0o600 and "0600" in cache.protection()


def test_time_zone_loads():
    """Europe/Bratislava must load on every OS (Windows: from the tzdata package)."""
    assert cache.TZ.key == "Europe/Bratislava"
    assert datetime(2026, 1, 1, 12, tzinfo=timezone.utc).astimezone(cache.TZ).hour == 13
    assert datetime(2026, 7, 1, 12, tzinfo=timezone.utc).astimezone(cache.TZ).hour == 14


@pytest.mark.anyio
async def test_status_reports_real_data_dir(tmp_path):
    s = create_server(PUBLIC_POLICY, data_dir=tmp_path / "data")
    d = (await _call(s, "kataster_status", {}))["data"]
    assert d["data_dir"] == str((tmp_path / "data").resolve()) and Path(d["data_dir"]).is_absolute()
    assert d["platforma"] and d["ochrana_suborov"] == cache.protection()
