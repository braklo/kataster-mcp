"""Pause between requests to one host across processes (two MCP clients, a CLI call next to a server).

Every host gets its own SQLite file `<dir>/<host>.sqlite` with the wall-clock time of the last request and the
instance that made it. A request holds the file's write lock (BEGIN IMMEDIATE) from before the pause until its
time is written after the response, so no two processes ever talk to the same host at once, and a long request
to one host never holds up another host. SQLite's lock is an OS file lock: a crashed process releases it.

Wall time is the only clock processes share. Elapsed time is max(0, now - last), so a clock set back gives the
whole pause, never a shorter one. Requests of the own instance are paced by the monotonic clock in Http.
"""

import asyncio
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from .cache import restrict

_HOST = re.compile(r"[a-z0-9.-]+")


class Pacer:
    def __init__(self, directory: Path, *, wall=time.time, poll_s: float = 0.05):
        self.dir = Path(directory)
        self._wall = wall
        self._poll_s = poll_s
        self._token = uuid.uuid4().hex  # this instance; its own requests are paced by Http's monotonic clock
        self._db: dict[str, sqlite3.Connection] = {}

    def _conn(self, host: str) -> sqlite3.Connection:
        if not _HOST.fullmatch(host) or host.strip(".") != host:
            raise ValueError(f"invalid host name for pacing: {host!r}")
        db = self._db.get(host)
        if db is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            restrict(self.dir, 0o700)
            # timeout=0: never block the event loop in SQLite's busy handler; hold() polls with asyncio.sleep
            db = sqlite3.connect(self.dir / f"{host}.sqlite", timeout=0, isolation_level=None,
                                 check_same_thread=False)
            self._db[host] = db
        return db

    async def _begin(self, db: sqlite3.Connection) -> None:
        while True:
            try:
                db.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) and "busy" not in str(e):
                    raise
            await asyncio.sleep(self._poll_s)

    @asynccontextmanager
    async def hold(self, host: str):
        """Hold the host's lock; yields seconds since the last request to it by another instance (None: none or own).
        The time of this request is written when the block ends, even when the request failed."""
        db = self._conn(host)
        await self._begin(db)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS tempo (id INTEGER PRIMARY KEY CHECK (id = 1), posledny REAL, kto TEXT)")
            row = db.execute("SELECT posledny, kto FROM tempo WHERE id = 1").fetchone()
            since = None if row is None or row[1] == self._token else max(0.0, self._wall() - row[0])
            try:
                yield since
            finally:
                db.execute("INSERT OR REPLACE INTO tempo (id, posledny, kto) VALUES (1, ?, ?)",
                           (self._wall(), self._token))
        finally:
            if db.in_transaction:
                db.execute("COMMIT")

    def close(self) -> None:
        for db in self._db.values():
            db.close()
        self._db.clear()
