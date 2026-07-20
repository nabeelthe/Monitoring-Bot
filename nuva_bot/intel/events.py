"""Event model + SQLite-backed historical memory.

Every signal the collectors produce becomes an Event: scored, correlated into a
story, persisted forever, and queryable (/history, /search, reports, dashboard).
"""

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

PRIORITIES = ("critical", "high", "medium", "low", "ignore")


@dataclass
class Event:
    monitor: str
    layer: str
    title: str
    body: str = ""
    url: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    ts: float = field(default_factory=time.time)
    priority_class: str = "escalate"        # route-map class: always | escalate | kw
    escalation_hits: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    confidence: int = 50                    # 0-100, set by the scorer
    priority: str = "medium"                # critical|high|medium|low|ignore
    story_id: str = ""                      # set by the correlator
    sent: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class EventStore:
    """Append-only intelligence memory. sqlite3 ops here are sub-millisecond,
    so calls from async code are done directly under a thread lock."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events(
                id TEXT PRIMARY KEY,
                ts REAL NOT NULL,
                monitor TEXT, layer TEXT, title TEXT, body TEXT, url TEXT,
                priority_class TEXT, escalation TEXT, tags TEXT,
                confidence INTEGER, priority TEXT, story_id TEXT, sent INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
            CREATE INDEX IF NOT EXISTS idx_events_story ON events(story_id);
            CREATE INDEX IF NOT EXISTS idx_events_layer ON events(layer, ts);
            CREATE TABLE IF NOT EXISTS ticks(
                ts REAL NOT NULL, price REAL, volume REAL
            );
            CREATE INDEX IF NOT EXISTS idx_ticks_ts ON ticks(ts);
            CREATE TABLE IF NOT EXISTS wallet_flows(
                ts REAL NOT NULL, chain TEXT, wallet TEXT,
                direction TEXT, amount REAL, denom TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_wallet_flows ON wallet_flows(chain, wallet, ts);
            CREATE TABLE IF NOT EXISTS outcomes(
                event_id TEXT PRIMARY KEY,
                ts REAL NOT NULL,
                kind TEXT NOT NULL,
                ret_1h REAL, ret_24h REAL,
                done_1h INTEGER DEFAULT 0, done_24h INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_outcomes_kind ON outcomes(kind);
        """)
        self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    # ---- writes ---------------------------------------------------------
    def add(self, ev: Event):
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ev.id, ev.ts, ev.monitor, ev.layer, ev.title, ev.body, ev.url,
                 ev.priority_class, json.dumps(ev.escalation_hits), json.dumps(ev.tags),
                 ev.confidence, ev.priority, ev.story_id, int(ev.sent)),
            )
            self._db.commit()

    def mark_sent(self, event_id: str):
        with self._lock:
            self._db.execute("UPDATE events SET sent=1 WHERE id=?", (event_id,))
            self._db.commit()

    # ---- reads ----------------------------------------------------------
    @staticmethod
    def _row_to_event(row) -> Event:
        return Event(
            id=row["id"], ts=row["ts"], monitor=row["monitor"], layer=row["layer"],
            title=row["title"], body=row["body"] or "", url=row["url"],
            priority_class=row["priority_class"],
            escalation_hits=json.loads(row["escalation"] or "[]"),
            tags=json.loads(row["tags"] or "[]"),
            confidence=row["confidence"], priority=row["priority"],
            story_id=row["story_id"] or "", sent=bool(row["sent"]),
        )

    def recent(self, hours: float = 24, *, layer: str | None = None,
               min_priority: str | None = None, limit: int = 300) -> list[Event]:
        q = "SELECT * FROM events WHERE ts >= ?"
        args: list = [time.time() - hours * 3600]
        if layer:
            q += " AND layer = ?"
            args.append(layer)
        if min_priority and min_priority in PRIORITIES:
            allowed = PRIORITIES[: PRIORITIES.index(min_priority) + 1]
            q += f" AND priority IN ({','.join('?' * len(allowed))})"
            args.extend(allowed)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [self._row_to_event(r) for r in rows]

    def search(self, text: str, limit: int = 30) -> list[Event]:
        like = f"%{text}%"
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM events WHERE title LIKE ? OR body LIKE ? "
                "ORDER BY ts DESC LIMIT ?", (like, like, limit),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def story_events(self, story_id: str, limit: int = 50) -> list[Event]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM events WHERE story_id=? ORDER BY ts ASC LIMIT ?",
                (story_id, limit),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def similar_past(self, ev: Event, limit: int = 5) -> list[Event]:
        """Historical comparison: same monitor, similar title, older than 24h."""
        token = ""
        for word in ev.title.lower().split():
            if len(word) >= 5 and word.isalpha():
                token = word
                break
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM events WHERE monitor=? AND ts < ? AND title LIKE ? "
                "ORDER BY ts DESC LIMIT ?",
                (ev.monitor, ev.ts - 86400, f"%{token}%" if token else "%",
                 limit),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def counts_by_layer(self, hours: float = 24) -> dict:
        with self._lock:
            rows = self._db.execute(
                "SELECT layer, COUNT(*) n FROM events WHERE ts >= ? GROUP BY layer",
                (time.time() - hours * 3600,),
            ).fetchall()
        return {r["layer"]: r["n"] for r in rows}

    def counts_by_priority(self, hours: float = 24) -> dict:
        with self._lock:
            rows = self._db.execute(
                "SELECT priority, COUNT(*) n FROM events WHERE ts >= ? GROUP BY priority",
                (time.time() - hours * 3600,),
            ).fetchall()
        return {r["priority"]: r["n"] for r in rows}

    def escalation_hits(self, keyword: str, hours: float = 48) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) n FROM events WHERE ts >= ? AND escalation LIKE ?",
                (time.time() - hours * 3600, f'%"{keyword}"%'),
            ).fetchone()
        return row["n"]

    def total(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]

    # ---- market time series (price/volume ticks) --------------------------
    def add_tick(self, price: float, volume: float, ts: float | None = None,
                 min_gap: float = 240):
        """Record a market tick; skips if the last tick is younger than min_gap
        seconds so restarts/races can't double-write."""
        ts = ts or time.time()
        with self._lock:
            row = self._db.execute("SELECT MAX(ts) m FROM ticks").fetchone()
            if row["m"] is not None and ts - row["m"] < min_gap:
                return
            self._db.execute("INSERT INTO ticks VALUES (?,?,?)", (ts, price, volume))
            # keep 90 days max
            self._db.execute("DELETE FROM ticks WHERE ts < ?", (ts - 90 * 86400,))
            self._db.commit()

    def ticks(self, hours: float = 24, limit: int = 2500) -> list[tuple[float, float, float]]:
        """Return [(ts, price, volume), ...] oldest-first for the window."""
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, price, volume FROM ticks WHERE ts >= ? ORDER BY ts ASC LIMIT ?",
                (time.time() - hours * 3600, limit),
            ).fetchall()
        return [(r["ts"], r["price"], r["volume"]) for r in rows]

    # ---- wallet flows (for Wallet Intelligence) ----------------------------
    def record_wallet_flow(self, chain: str, wallet: str, direction: str,
                           amount: float, denom: str = "", ts: float | None = None):
        if not wallet or amount is None or amount <= 0 or direction not in ("in", "out"):
            return
        ts = ts or time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO wallet_flows VALUES (?,?,?,?,?,?)",
                (ts, chain, wallet.lower(), direction, float(amount), denom),
            )
            self._db.execute("DELETE FROM wallet_flows WHERE ts < ?", (ts - 30 * 86400,))
            self._db.commit()

    def wallet_daily_flows(self, chain: str, days: float = 7) -> dict:
        """{wallet: {'YYYY-MM-DD': {'in': x, 'out': y}}} for the window."""
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, wallet, direction, amount FROM wallet_flows "
                "WHERE chain=? AND ts >= ? ORDER BY ts ASC",
                (chain, time.time() - days * 86400),
            ).fetchall()
        out: dict = {}
        for r in rows:
            day = datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%Y-%m-%d")
            bucket = out.setdefault(r["wallet"], {}).setdefault(day, {"in": 0.0, "out": 0.0})
            bucket[r["direction"]] += r["amount"]
        return out

    # ---- signal outcomes (self-measured hit rates) --------------------------
    def price_near(self, ts: float, tolerance: float = 900) -> float | None:
        """Closest recorded price within ±tolerance seconds of ts, else None."""
        with self._lock:
            row = self._db.execute(
                "SELECT price FROM ticks WHERE ts BETWEEN ? AND ? AND price > 0 "
                "ORDER BY ABS(ts - ?) ASC LIMIT 1",
                (ts - tolerance, ts + tolerance, ts),
            ).fetchone()
        return row["price"] if row else None

    def outcome_record(self, event_id: str, ts: float, kind: str):
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO outcomes(event_id, ts, kind) VALUES (?,?,?)",
                (event_id, ts, kind),
            )
            self._db.commit()

    def outcomes_pending(self, horizon: str, before_ts: float, limit: int = 200) -> list[tuple[str, float]]:
        """(event_id, ts) rows whose `horizon` ('1h'|'24h') is not yet annotated
        and whose measurement time has passed."""
        col = "done_1h" if horizon == "1h" else "done_24h"
        with self._lock:
            rows = self._db.execute(
                f"SELECT event_id, ts FROM outcomes WHERE {col}=0 AND ts <= ? LIMIT ?",
                (before_ts, limit),
            ).fetchall()
        return [(r["event_id"], r["ts"]) for r in rows]

    def outcome_set(self, event_id: str, horizon: str, ret: float | None):
        """Mark a horizon measured; ret may be None (no price data ⇒ excluded from stats)."""
        col_ret = "ret_1h" if horizon == "1h" else "ret_24h"
        col_done = "done_1h" if horizon == "1h" else "done_24h"
        with self._lock:
            self._db.execute(
                f"UPDATE outcomes SET {col_ret}=?, {col_done}=1 WHERE event_id=?",
                (ret, event_id),
            )
            self._db.commit()

    def hit_rates(self, min_n: int = 3, positive_pct: float = 2.0) -> dict:
        """Per signal kind: how often a measured +move followed, and average returns.
        {kind: {n, up_rate, avg_1h, avg_24h}} — only kinds with >= min_n samples."""
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, ret_1h, ret_24h FROM outcomes WHERE done_24h=1 AND ret_24h IS NOT NULL",
            ).fetchall()
        agg: dict = {}
        for r in rows:
            a = agg.setdefault(r["kind"], {"n": 0, "ups": 0, "sum_1h": 0.0, "n_1h": 0, "sum_24h": 0.0})
            a["n"] += 1
            a["sum_24h"] += r["ret_24h"]
            if r["ret_24h"] >= positive_pct:
                a["ups"] += 1
            if r["ret_1h"] is not None:
                a["sum_1h"] += r["ret_1h"]
                a["n_1h"] += 1
        out = {}
        for kind, a in agg.items():
            if a["n"] >= min_n:
                out[kind] = {
                    "n": a["n"],
                    "up_rate": a["ups"] / a["n"],
                    "avg_24h": a["sum_24h"] / a["n"],
                    "avg_1h": (a["sum_1h"] / a["n_1h"]) if a["n_1h"] else None,
                }
        return out
