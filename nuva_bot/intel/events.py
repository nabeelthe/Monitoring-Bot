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
