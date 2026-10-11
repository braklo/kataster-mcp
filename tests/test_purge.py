"""Expired personal data is physically deleted (checked with a separate sqlite3 connection, not the cache API)."""

import os
import sqlite3
import time

from kataster_mcp.cache import DAY, PERSONAL_TTL, Cache
from kataster_mcp.policy import PUBLIC_POLICY
from kataster_mcp.server import create_server

OLD = "Starý Fiktívny"
NEW = "Nový Fiktívny"


def _raw_rows(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT zdroj, kluc, data FROM cache").fetchall()
    finally:
        con.close()


def _age(path, kluc, seconds):
    con = sqlite3.connect(path)
    con.execute("UPDATE cache SET ulozene = ? WHERE kluc = ?", (time.time() - seconds, kluc))
    con.commit()
    con.close()


def test_expired_rows_deleted_on_open_and_after_write(tmp_path):
    path = tmp_path / "osoby.sqlite"
    c = Cache(path, private=True, max_age_s=PERSONAL_TTL)
    c.put("eskn_detail", "v1", "old", {"meno": OLD})
    c.put("lv_import", "v0", "old-version", {"meno": OLD})  # another source and an old parser version too
    c.put("eskn_detail", "v1", "new", {"meno": NEW})
    c.db.close()
    _age(path, "old", PERSONAL_TTL + 60)
    _age(path, "old-version", PERSONAL_TTL + 60)
    _age(path, "new", PERSONAL_TTL - DAY)

    c = Cache(path, private=True, max_age_s=PERSONAL_TTL)  # opening purges
    rows = _raw_rows(path)
    assert [k for _, k, _ in rows] == ["new"]  # positive control: the fresh row stays
    text = path.read_bytes().decode("utf-8", "ignore")
    assert OLD not in text and NEW in text  # VACUUM + secure_delete: no trace in the file (NEW: positive control)

    _age(path, "new", PERSONAL_TTL + 60)
    c.put("eskn_detail", "v1", "other", {"meno": "Iný Fiktívny"})  # a write purges too
    assert [k for _, k, _ in _raw_rows(path)] == ["other"]
    assert c.db.execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_non_personal_cache_is_not_purged(tmp_path):
    path = tmp_path / "cache.sqlite"
    c = Cache(path, private=False)
    c.put("ugkk_inspire_wfs", "v1", "k", {"x": 1})
    _age(path, "k", 365 * DAY)
    c.put("ugkk_inspire_wfs", "v1", "k2", {"x": 2})
    assert len(_raw_rows(path)) == 2


def test_server_start_purges_osoby_and_snapshots(tmp_path):
    Cache(tmp_path / "osoby.sqlite", private=True, max_age_s=PERSONAL_TTL).put("eskn_detail", "v", "old", {"meno": OLD})
    _age(tmp_path / "osoby.sqlite", "old", PERSONAL_TTL + 60)
    snaps = tmp_path / "snapshots"
    snaps.mkdir()
    old, new = snaps / "old.html", snaps / "new.html"
    old.write_text(OLD, encoding="utf-8")
    new.write_text(NEW, encoding="utf-8")
    t = time.time() - PERSONAL_TTL - 60
    os.utime(old, (t, t))

    create_server(PUBLIC_POLICY, data_dir=tmp_path)
    assert _raw_rows(tmp_path / "osoby.sqlite") == []
    assert not old.exists() and new.exists()
