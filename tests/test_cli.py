"""sk-kataster contracts. Commands without a server run as a process outside the checkout; commands that start the
MCP tools run in-process with limits_dir passed in code, so no test touches the real limits directory."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import kataster_mcp.server
from kataster_mcp import cache, cli
from kataster_mcp.cache import Cache, Quota

FIXTURE = Path(__file__).parent / "fixtures" / "lv_vypis_anon.html"


def run_cli(tmp_path, *args, env=None):
    """As a process; only for commands that never start the server (they cannot reach the limits directory)."""
    result = subprocess.run(
        [sys.executable, "-m", "kataster_mcp.cli", "--json", "--data-dir", str(tmp_path / "data"), *args],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1", **(env or {})},
    )
    return result, json.loads(result.stdout)


def run_inproc(tmp_path, capsys, *args, data_dir=None):
    limits = tmp_path / "limity"
    code = cli.main(["--json", "--data-dir", str(data_dir or tmp_path / "data"), *args], limits_dir=limits)
    return code, json.loads(capsys.readouterr().out)


def test_doctor_reads_without_creating_anything(tmp_path):
    # the limits directory comes from the account's home (not $HOME): only check that doctor does not create it
    existed = cache.default_limits_dir().exists()
    result, out = run_cli(tmp_path, "doctor")
    assert result.returncode == 0
    d = out["data"]
    assert d["network_checked"] is False and "mode" not in d and "auth" not in d
    assert d["kvota_vlastnici"]["limit"] == 50
    assert not (tmp_path / "data").exists()
    assert Path(d["limits_dir"]).exists() == existed


def test_doctor_reports_todays_quota_from_the_limits_directory(tmp_path):
    limits = tmp_path / "limity"
    q = Quota(Cache(limits / "stav.sqlite", private=True).db)
    q.record("871541|E|503")
    q.record("871541|C|660/66")
    out = cli.doctor(tmp_path / "data", limits)["data"]
    assert out["kvota_vlastnici"]["pouzite"] == 2 and out["limits_dir"] == str(limits)


def test_inspect_defaults_to_compact_without_personal_fields(tmp_path):
    result, out = run_cli(tmp_path, "lv", "inspect", str(FIXTURE.resolve()))
    assert result.returncode == 0
    assert out["data"]["lv"] == "1926" and out["data"]["parcely"] == 26
    assert "FIKTÍVNY" not in result.stdout
    assert out["data"]["tarchy"] == "none"
    assert not (tmp_path / "data").exists()


def test_import_dry_run_stores_nothing(tmp_path):
    result, out = run_cli(tmp_path, "lv", "import", str(FIXTURE.resolve()), "--dry-run")
    assert result.returncode == 0 and out["data"]["dry_run"]
    assert not (tmp_path / "data").exists()


def test_import_and_person_overview(tmp_path, capsys):
    code, out = run_inproc(tmp_path, capsys, "lv", "import", str(FIXTURE.resolve()))
    assert code == 0 and out["data"]["ulozenie"]["stav"] == "stored"
    assert "FIKTÍVNY" not in json.dumps(out, ensure_ascii=False)
    assert (tmp_path / "data" / "osoby.sqlite").exists()
    code, out = run_inproc(tmp_path, capsys, "person", "overview", "--surname", "Fiktívny", "--first-name", "Vlastník")
    assert code == 0 and out["data"]["osoby"][0]["pocet_parciel"] == 26


def test_missing_file_and_invalid_flags_are_json_errors(tmp_path):
    for args in [("lv", "inspect", "missing.html"), ("nonsense",)]:
        result, out = run_cli(tmp_path, *args)
        assert result.returncode != 0
        assert out["chyba"]["typ"] in ("invalid_input", "not_found")


def test_tool_error_is_json_with_exit_code(tmp_path, capsys):
    code, out = run_inproc(tmp_path, capsys, "person", "overview", "--ico", "abc")
    assert code == 1 and out["chyba"]["typ"] == "invalid_input"


def test_ku_resolve_and_local_url(tmp_path):
    result, out = run_cli(tmp_path, "ku", "resolve", "Vyšný Kubín")
    assert result.returncode == 0 and out["data"]["kod"] == "871541"
    result, out = run_cli(tmp_path, "lv", "url", "--ku", "871541", "--lv", "1926")
    assert result.returncode == 0 and "prfNumber=1926" in out["data"]["url"]
    assert not (tmp_path / "data").exists()


def test_tools_lists_names_until_one_is_requested(tmp_path, capsys):
    code, out = run_inproc(tmp_path, capsys, "tools")
    assert code == 0 and "kataster_lv_import" in out["data"]["tools"]
    assert "inputSchema" not in json.dumps(out)
    code, out = run_inproc(tmp_path, capsys, "tools", "kataster_lv_import")
    assert code == 0 and out["data"]["name"] == "kataster_lv_import"


def test_call_goes_through_create_server_with_the_shared_pacer(tmp_path, capsys, monkeypatch):
    seen = []
    real = kataster_mcp.server.create_server

    def spy(*a, **kw):
        server = real(*a, **kw)
        seen.append((kw.get("limits_dir"), server.app_state.http.pacer))
        return server

    monkeypatch.setattr(kataster_mcp.server, "create_server", spy)
    code, out = run_inproc(tmp_path, capsys, "call", "kataster_ku", '{"query": "Vyšný Kubín"}')
    assert code == 0, out
    assert seen and seen[0][0] == tmp_path / "limity" and seen[0][1] is not None


def test_data_dir_does_not_move_the_quota(tmp_path, capsys):
    q = Quota(Cache(tmp_path / "limity" / "stav.sqlite", private=True).db)
    for i in range(3):
        q.record(f"871541|E|{i}")
    for data in ("a", "b"):
        code, out = run_inproc(tmp_path, capsys, "call", "kataster_status", "{}", data_dir=tmp_path / data)
        assert code == 0 and out["data"]["kvota_vlastnici"]["pouzite"] == 3


def test_data_dir_variable_moves_only_the_data(monkeypatch, tmp_path):
    monkeypatch.delenv("KATASTER_DATA_DIR", raising=False)
    assert cache.configured_data_dir() == cache.default_data_dir()
    before = cache.default_limits_dir()
    monkeypatch.setenv("KATASTER_DATA_DIR", str(tmp_path / "isolated"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert cache.configured_data_dir() == tmp_path / "isolated"
    assert cache.default_limits_dir() == before


def test_cli_json_is_utf8_even_with_legacy_console_encoding(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "kataster_mcp.cli", "--json", "ku", "resolve", "871541"],
        cwd=tmp_path, capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"},
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert json.loads(result.stdout.decode("utf-8"))["data"]["nazov"] == "Vyšný Kubín"


@pytest.fixture
def anyio_backend():
    return "asyncio"
