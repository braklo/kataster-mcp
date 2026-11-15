"""State of the limits in a fixed per-user directory: no environment variable moves it."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from kataster_mcp import cache
from kataster_mcp.cache import today
from kataster_mcp.http import BLOCK_SOURCE
from kataster_mcp.policy import ESKN_HOST, PUBLIC_POLICY
from kataster_mcp.server import create_server

SRC = str(Path(__file__).parents[1] / "src")


def test_fixed_dir_per_platform_ignores_environment(monkeypatch):
    home = cache._home()
    assert cache.default_limits_dir("linux") == home / ".local" / "state" / "kataster-mcp"
    assert cache.default_limits_dir("darwin") == home / "Library" / "Application Support" / "kataster-mcp-limits"
    assert cache.default_limits_dir("win32") == home / "AppData" / "Local" / "kataster-mcp-limits"
    for var in ("XDG_DATA_HOME", "XDG_STATE_HOME", "LOCALAPPDATA", "KATASTER_DATA_DIR"):
        monkeypatch.setenv(var, "/elsewhere")
    if os.name == "posix":
        monkeypatch.setenv("HOME", "/elsewhere")  # the account database decides, not $HOME
    assert cache.default_limits_dir("linux") == home / ".local" / "state" / "kataster-mcp"


def test_other_data_dir_and_xdg_share_the_quota(tmp_path):
    """Two processes with different data dirs count into one quota on every OS; on Linux the second data dir also
    comes from another XDG_DATA_HOME, the variable that used to move the quota with the data."""
    script = tmp_path / "count.py"
    script.write_text(
        "import pathlib, sys\nfrom kataster_mcp.cache import default_data_dir\nfrom kataster_mcp.policy import PUBLIC_POLICY\n"
        "from kataster_mcp.server import create_server\n"
        "data = pathlib.Path(sys.argv[3]) if sys.argv[3] != '-' else default_data_dir()\n"
        "s = create_server(PUBLIC_POLICY, data_dir=data, limits_dir=pathlib.Path(sys.argv[1]))\n"
        "s.app_state.quota.record(sys.argv[2])\n"
        "print(s.app_state.data_dir, s.app_state.quota.used(), s.app_state.http.pacer.dir, sep='|')\n", encoding="utf8")
    out = []
    for i, parcel in enumerate(("871541|E 503", "871541|C 660/66")):
        env = {**os.environ, "PYTHONPATH": SRC, "XDG_DATA_HOME": str(tmp_path / f"xdg{i}")}
        # elsewhere XDG_DATA_HOME means nothing: the data dir is given as an argument
        data = "-" if sys.platform.startswith("linux") else str(tmp_path / f"data{i}")
        out.append(subprocess.run([sys.executable, str(script), str(tmp_path / "limity"), parcel, data], env=env,
                                  capture_output=True, text=True, timeout=30, check=True).stdout.strip().split("|"))
    assert out[0][0] != out[1][0]  # really two data dirs
    assert (out[0][1], out[1][1]) == ("1", "2")
    assert out[0][2] == out[1][2] == str(tmp_path / "limity" / "tempo")  # and the same pause


def test_todays_quota_carries_over_from_the_data_dir(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    old = sqlite3.connect(data / "osoby.sqlite")
    old.execute("CREATE TABLE kvota (den TEXT, parcela TEXT, PRIMARY KEY (den, parcela))")
    old.executemany("INSERT INTO kvota VALUES (?, ?)", [(today(), "a"), (today(), "b"), ("2000-01-01", "c")])
    old.commit()
    old.close()
    s = create_server(PUBLIC_POLICY, data_dir=data, limits_dir=tmp_path / "limity")
    s.app_state.quota.record("z")  # newer state in the fixed place
    assert s.app_state.quota.used() == 3
    again = create_server(PUBLIC_POLICY, data_dir=data, limits_dir=tmp_path / "limity")
    assert again.app_state.quota.used() == 3  # union, never reset, never double


def test_block_in_the_data_dir_still_holds(tmp_path):
    data = tmp_path / "data"
    first = create_server(PUBLIC_POLICY, data_dir=data, limits_dir=tmp_path / "old-limits")
    first.app_state.cache.put(BLOCK_SOURCE, "1", ESKN_HOST, {"typ": "captcha_required"})  # where it was kept before
    s = create_server(PUBLIC_POLICY, data_dir=data, limits_dir=tmp_path / "limity")
    assert s.app_state.http.blocked_until() is not None


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_limits_dir_permissions(tmp_path):
    s = create_server(PUBLIC_POLICY, data_dir=tmp_path / "data", limits_dir=tmp_path / "limity")
    assert (s.app_state.limits_dir.stat().st_mode & 0o777) == 0o700
    assert ((tmp_path / "limity" / "stav.sqlite").stat().st_mode & 0o777) == 0o600


def _smoke_server():
    import importlib.util
    spec = importlib.util.spec_from_file_location("smoke_server", Path(__file__).parents[1] / "scripts" / "smoke_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_smoke_offline_has_temporary_limits_and_no_network(tmp_path):
    import asyncio

    from kataster_mcp.envelope import KatasterError
    s = _smoke_server().build("--offline", tmp_path)
    assert s.app_state.limits_dir == tmp_path / "limity" and s.app_state.data_dir == tmp_path / "data"
    with pytest.raises(KatasterError) as e:
        asyncio.run(s.app_state.http.get("https://kataster.skgeodesy.sk/x"))
    assert e.value.kind == "unavailable" and "offline smoke" in e.value.message


def test_smoke_live_uses_the_real_limits_dir(tmp_path, monkeypatch):
    import kataster_mcp.server
    monkeypatch.setattr(kataster_mcp.server, "default_limits_dir", lambda: tmp_path / "real-limits")
    s = _smoke_server().build("--live", tmp_path)
    assert s.app_state.limits_dir == tmp_path / "real-limits" and s.app_state.data_dir == tmp_path / "data"
    with pytest.raises(SystemExit):
        _smoke_server().build("--whatever", tmp_path)
