"""Tests never touch the real limits directory: every create_server gets limits_dir as an argument."""
import pytest

import kataster_mcp.server


@pytest.fixture(autouse=True)
def _no_real_limits_dir(monkeypatch):
    def refuse():
        raise AssertionError("test called create_server without limits_dir (would use the real quota)")
    monkeypatch.setattr(kataster_mcp.server, "default_limits_dir", refuse)
