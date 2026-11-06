"""SQLite persistence for readings, relay events, doses and alert history."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .model import Alert, Reading

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    ts REAL NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS readings_metric_ts ON readings (metric, ts);

CREATE TABLE IF NOT EXISTS events (
    ts REAL NOT NULL,
    actuator TEXT NOT NULL,
    is_on INTEGER NOT NULL,
    reason TEXT,
    source TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);

CREATE TABLE IF NOT EXISTS doses (
    ts REAL NOT NULL,
    pump TEXT NOT NULL,
    ml REAL NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS doses_ts ON doses (ts);

CREATE TABLE IF NOT EXISTS alerts (
    ts REAL NOT NULL,
    rule TEXT NOT NULL,
    state TEXT NOT NULL,
    severity TEXT,
    metric TEXT,
    value REAL,
    message TEXT
);
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts (ts);
"""


class Storage:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.commit()

    # -- writes ----------------------------------------------------------
    def add_readings(self, readings: list[Reading]) -> None:
        if not readings:
            return
        rows = [(r.ts, r.metric, r.value) for r in readings]
        with self._lock:
            self._db.executemany("INSERT INTO readings (ts, metric, value) VALUES (?,?,?)", rows)
            self._db.commit()

    def add_event(self, ts: float, actuator: str, on: bool, reason: str, source: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO events (ts, actuator, is_on, reason, source) VALUES (?,?,?,?,?)",
                (ts, actuator, int(on), reason, source),
            )
            self._db.commit()

    def add_dose(self, ts: float, pump: str, ml: float, reason: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO doses (ts, pump, ml, reason) VALUES (?,?,?,?)",
                (ts, pump, ml, reason),
            )
            self._db.commit()

    def add_alert(self, alert: Alert, state: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO alerts (ts, rule, state, severity, metric, value, message) "
                "VALUES (?,?,?,?,?,?,?)",
                (time.time(), alert.rule, state, alert.severity, alert.metric,
                 alert.value, alert.message),
            )
            self._db.commit()

    # -- reads -----------------------------------------------------------
    def latest(self, metrics: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        with self._lock:
            for metric in metrics:
                row = self._db.execute(
                    "SELECT value FROM readings WHERE metric=? ORDER BY ts DESC LIMIT 1",
                    (metric,),
                ).fetchone()
                if row:
                    out[metric] = row["value"]
        return out

    def series(self, metrics: list[str], since: float, points: int = 240) -> dict[str, dict]:
        """Downsample each metric into at most `points` time buckets."""
        now = time.time()
        span = max(60.0, now - since)
        bucket = max(5.0, span / max(1, points))
        out: dict[str, dict] = {}
        with self._lock:
            for metric in metrics:
                rows = self._db.execute(
                    "SELECT CAST(ts / ? AS INTEGER) AS b, AVG(value) AS v "
                    "FROM readings WHERE metric=? AND ts>=? GROUP BY b ORDER BY b",
                    (bucket, metric, since),
                ).fetchall()
                out[metric] = {
                    "t": [round(r["b"] * bucket, 1) for r in rows],
                    "v": [round(r["v"], 4) for r in rows],
                    "bucket_s": round(bucket, 1),
                }
        return out

    def events(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, actuator, is_on, reason, source FROM events "
                "ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"ts": r["ts"], "actuator": r["actuator"], "on": bool(r["is_on"]),
                 "reason": r["reason"], "source": r["source"]} for r in rows]

    def doses(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, pump, ml, reason FROM doses ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def doses_since(self, since: float) -> dict[str, float]:
        with self._lock:
            rows = self._db.execute(
                "SELECT pump, SUM(ml) AS total FROM doses WHERE ts>=? GROUP BY pump",
                (since,),
            ).fetchall()
        return {r["pump"]: float(r["total"] or 0.0) for r in rows}

    def alert_history(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, rule, state, severity, metric, value, message "
                "FROM alerts ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        with self._lock:
            count = self._db.execute("SELECT COUNT(*) AS n FROM readings").fetchone()["n"]
            first = self._db.execute("SELECT MIN(ts) AS t FROM readings").fetchone()["t"]
        return {"readings": count, "first_ts": first, "path": self.path}

    # -- maintenance -----------------------------------------------------
    def prune(self, days: int) -> int:
        cutoff = time.time() - days * 86400
        with self._lock:
            cur = self._db.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
            self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._db.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))
            self._db.commit()
        return cur.rowcount or 0

    def close(self) -> None:
        with self._lock:
            self._db.commit()
            self._db.close()
