"""SQLite usage store.

Backends report *deltas* (bytes transferred since the previous poll). The store adds
those deltas into per-day buckets, so the totals "since the start date" are always a
simple SUM over days >= start_date – changing the start date is instant.
"""
import sqlite3
from datetime import date
from typing import Dict, Iterable, Tuple

from .settings import data_dir


class UsageStore:
    def __init__(self, path=None):
        self.db = sqlite3.connect(str(path or data_dir() / "usage.db"))
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS usage(
                day TEXT, kind TEXT, key TEXT, bytes INTEGER,
                PRIMARY KEY(day, kind, key));
            CREATE TABLE IF NOT EXISTS devices(
                mac TEXT PRIMARY KEY, name TEXT, ip TEXT, dtype TEXT);
        """)

    # kind = "dev" (key = MAC) or "site" (key = domain)
    def add(self, kind: str, deltas: Dict[str, int], day: date = None) -> None:
        d = (day or date.today()).isoformat()
        rows = [(d, kind, k, int(v)) for k, v in deltas.items() if v > 0]
        self.db.executemany("""
            INSERT INTO usage(day, kind, key, bytes) VALUES(?,?,?,?)
            ON CONFLICT(day, kind, key) DO UPDATE SET bytes = bytes + excluded.bytes
        """, rows)
        self.db.commit()

    def remember_devices(self, devices: Iterable[Tuple[str, str, str, str]]) -> None:
        self.db.executemany("""
            INSERT INTO devices(mac, name, ip, dtype) VALUES(?,?,?,?)
            ON CONFLICT(mac) DO UPDATE SET name=excluded.name, ip=excluded.ip, dtype=excluded.dtype
        """, list(devices))
        self.db.commit()

    def device_meta(self) -> Dict[str, Tuple[str, str, str]]:
        return {m: (n, ip, t) for m, n, ip, t in self.db.execute("SELECT mac, name, ip, dtype FROM devices")}

    def totals(self, kind: str, since: date) -> Dict[str, int]:
        cur = self.db.execute(
            "SELECT key, SUM(bytes) FROM usage WHERE kind=? AND day>=? GROUP BY key",
            (kind, since.isoformat()))
        return {k: int(v) for k, v in cur}

    def is_empty(self) -> bool:
        return self.db.execute("SELECT 1 FROM usage LIMIT 1").fetchone() is None
