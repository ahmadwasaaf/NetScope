"""SQLite usage store.

Backends report *deltas* (bytes since the previous poll). The store adds them into
per-minute buckets, so "usage since the start moment" is a SUM over buckets >= start –
changing the start date (or restarting "from now") is instant and exact to the minute.
"""
import sqlite3
from datetime import date, datetime
from typing import Dict, Iterable, Tuple, Union

from .settings import data_dir

Moment = Union[date, datetime]


def bucket(t: Moment) -> str:
    if isinstance(t, datetime):
        return t.strftime("%Y-%m-%dT%H:%M")
    return t.isoformat() + "T00:00"


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
        # older versions stored whole days ("2026-11-20") -> start of that day
        self.db.execute("UPDATE OR IGNORE usage SET day = day || 'T00:00' WHERE length(day) = 10")
        self.db.commit()

    # kind: "dev" (key MAC) · "site" (router per-site) · "wan" · "pcsite" (this PC, bytes)
    #       "devsite" (key "mac|site", DNS activity per device)
    def add(self, kind: str, deltas: Dict[str, int], when: datetime = None) -> None:
        b = bucket(when or datetime.now())
        rows = [(b, kind, k, int(v)) for k, v in deltas.items() if v > 0]
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

    def totals(self, kind: str, since: Moment) -> Dict[str, int]:
        cur = self.db.execute(
            "SELECT key, SUM(bytes) FROM usage WHERE kind=? AND day>=? GROUP BY key",
            (kind, bucket(since)))
        return {k: int(v) for k, v in cur}

    def is_empty(self) -> bool:
        return self.db.execute("SELECT 1 FROM usage LIMIT 1").fetchone() is None
