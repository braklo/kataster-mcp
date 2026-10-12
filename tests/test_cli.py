"""CLI contracts exercised as a process, outside the checkout."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "lv_vypis_anon.html"


def run_cli(tmp_path, *args):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kataster_mcp.cli",
            "--json",
            "--data-dir",
            str(tmp_path / "data"),
            *args,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    return result, json.loads(result.stdout)


def test_doctor_runs_without_network_or_creating_cache(tmp_path):
    result, out = run_cli(tmp_path, "doctor")
    assert result.returncode == 0
    assert out["data"]["mode"] == "local"
    assert out["data"]["auth"] == "not_required"
    assert not (tmp_path / "data").exists()


def test_inspect_defaults_to_compact_without_personal_fields(tmp_path):
    result, out = run_cli(tmp_path, "lv", "inspect", str(FIXTURE.resolve()))
    assert result.returncode == 0
    assert out["data"]["lv"] == "1926"
    assert out["data"]["parcely"] == 26
    assert "FIKTÍVNY" not in result.stdout
    assert out["data"]["tarchy"] == "none"
    assert out["varovania"]
    assert not (tmp_path / "data").exists()


def test_import_dry_run_and_actual_import(tmp_path):
    result, out = run_cli(tmp_path, "lv", "import", str(FIXTURE.resolve()), "--dry-run")
    assert result.returncode == 0 and out["data"]["dry_run"]
    assert not (tmp_path / "data").exists()
    result, out = run_cli(tmp_path, "lv", "import", str(FIXTURE.resolve()))
    assert result.returncode == 0 and out["data"]["ulozenie"]["stav"] == "stored"
    assert (tmp_path / "data" / "osoby.sqlite").exists()
    result, out = run_cli(tmp_path, "person", "overview", "--name", "Fiktívny Vlastník")
    assert result.returncode == 0
    assert out["data"]["osoby"][0]["pocet_parciel"] == 26


def test_missing_file_and_invalid_flags_are_json_errors(tmp_path):
    for args in [("lv", "inspect", "missing.html"), ("nonsense",)]:
        result, out = run_cli(tmp_path, *args)
        assert result.returncode != 0
        assert out["chyba"]["typ"] in ("invalid_input", "not_found")


def test_ku_resolve_and_local_url(tmp_path):
    result, out = run_cli(tmp_path, "ku", "resolve", "Vyšný Kubín")
    assert result.returncode == 0 and out["data"]["kod"] == "871541"
    result, out = run_cli(tmp_path, "lv", "url", "--ku", "871541", "--lv", "1926")
    assert result.returncode == 0 and "prfNumber=1926" in out["data"]["url"]
    assert not (tmp_path / "data").exists()


def test_discovery_only_emits_names_until_tool_is_requested(tmp_path):
    result, out = run_cli(tmp_path, "tools")
    assert result.returncode == 0 and "kataster_lv_import" in out["data"]["tools"]
    assert "inputSchema" not in result.stdout
    result, out = run_cli(tmp_path, "tools", "kataster_lv_import")
    assert result.returncode == 0 and out["data"]["name"] == "kataster_lv_import"


def test_data_dir_keeps_upstream_default_and_is_configurable(monkeypatch, tmp_path):
    from kataster_mcp import cache

    monkeypatch.delenv("KATASTER_DATA_DIR", raising=False)
    assert cache.configured_data_dir() == cache.default_data_dir()
    monkeypatch.setenv("KATASTER_DATA_DIR", str(tmp_path / "isolated"))
    assert cache.configured_data_dir() == tmp_path / "isolated"


def test_cli_json_is_utf8_even_with_legacy_console_encoding(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "kataster_mcp.cli", "--json", "ku", "resolve", "871541"],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"},
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert json.loads(result.stdout.decode("utf-8"))["data"]["nazov"] == "Vyšný Kubín"
