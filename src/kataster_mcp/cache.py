"""Local SQLite cache and the daily owners quota.

Two files under the data dir (0700):
- cache.sqlite  public data (geometry, layers)
- osoby.sqlite  personal data (owners, land users) and the quota, always 0600

Every entry is keyed by (source, parser version, key). The parser version is a hash
of the parser's source code, so fixing a parser silently invalidates old entries.
"""

import hashlib
import inspect
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Owner-only file permissions exist only on POSIX; on Windows chmod just toggles the read-only flag and the
# data directory under %LOCALAPPDATA% is protected by the user profile's ACL instead.
POSIX = os.name == "posix"


TZ = ZoneInfo("Europe/Bratislava")  # on Windows from the tzdata package (a dependency there)

DAY = 86400


def default_data_dir(platform: str | None = None, env: dict | None = None) -> Path:
    """Per-user data directory: Windows %LOCALAPPDATA%, macOS ~/Library/Application Support,
    elsewhere $XDG_DATA_HOME or ~/.local/share."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    if platform == "win32":
        base = env.get("LOCALAPPDATA") or os.path.join(Path.home(), "AppData", "Local")
    elif platform == "darwin":
        base = os.path.join(Path.home(), "Library", "Application Support")
    else:
        base = env.get("XDG_DATA_HOME") or os.path.join(Path.home(), ".local", "share")
    return Path(base) / "kataster-mcp"


# os.open() on Windows opens the C runtime descriptor in text mode unless O_BINARY is given (0 on POSIX).
O_BINARY = getattr(os, "O_BINARY", 0)


def restrict(path, mode: int) -> None:
    """chmod where it means something (POSIX); a no-op elsewhere, never an error."""
    if POSIX:
        os.chmod(path, mode)


def protection() -> str:
    return ("owner only (0700 directory, 0600 personal-data files)" if POSIX
            else "user profile ACL of the data directory (no POSIX permissions on this OS)")


def parser_version(*funcs) -> str:
    h = hashlib.sha256()
    for f in funcs:
        h.update(inspect.getsource(f).encode())
    return h.hexdigest()[:12]


def today() -> str:
    return datetime.now(TZ).date().isoformat()


def _ensure_private_file(path: Path) -> None:
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY | O_BINARY, 0o600))
    restrict(path, 0o600)


class Cache:
    def __init__(self, path: Path, *, private: bool):
        path.parent.mkdir(parents=True, exist_ok=True)
        restrict(path.parent, 0o700)
        if private:
            _ensure_private_file(path)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS cache (zdroj TEXT, ver TEXT, kluc TEXT, ulozene REAL, data TEXT,"
            " PRIMARY KEY (zdroj, ver, kluc))"
        )
        self.db.commit()

    def get(self, zdroj: str, ver: str, kluc: str, ttl_s: float | None):
        row = self.db.execute(
            "SELECT data, ulozene FROM cache WHERE zdroj=? AND ver=? AND kluc=?", (zdroj, ver, kluc)
        ).fetchone()
        if row is None:
            return None
        data, ulozene = row
        if ttl_s is not None and time.time() - ulozene > ttl_s:
            return None
        return json.loads(data), ulozene

    def put(self, zdroj: str, ver: str, kluc: str, data) -> float:
        ts = time.time()
        self.db.execute(
            "INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?, ?)",
            (zdroj, ver, kluc, ts, json.dumps(data, ensure_ascii=False)),
        )
        self.db.commit()
        return ts

    def stats(self) -> dict:
        rows = self.db.execute("SELECT zdroj, ver, COUNT(*) FROM cache GROUP BY zdroj, ver").fetchall()
        return {f"{z}@{v}": n for z, v, n in rows}


class Quota:
    """Parcels per calendar day whose owners were fetched from the portal."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.execute("CREATE TABLE IF NOT EXISTS kvota (den TEXT, parcela TEXT, PRIMARY KEY (den, parcela))")
        db.commit()

    def used(self, den: str | None = None) -> int:
        return self.db.execute("SELECT COUNT(*) FROM kvota WHERE den=?", (den or today(),)).fetchone()[0]

    def counted(self, parcela: str, den: str | None = None) -> bool:
        return self.db.execute(
            "SELECT 1 FROM kvota WHERE den=? AND parcela=?", (den or today(), parcela)
        ).fetchone() is not None

    def record(self, parcela: str, den: str | None = None) -> None:
        self.db.execute("INSERT OR IGNORE INTO kvota VALUES (?, ?)", (den or today(), parcela))
        self.db.commit()


def ts_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).isoformat(timespec="seconds")
