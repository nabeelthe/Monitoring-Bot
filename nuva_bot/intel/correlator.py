"""Event correlation: clusters related signals across layers into stories.

Wallet accumulation → dev commits → governance → TVL → price → listing rumor →
announcement is ONE story. An event joins an active story when it shares a
signal tag with it inside the correlation window; otherwise it starts one.
"""

import time
import uuid

from .events import Event, EventStore
from .scoring import extract_tags


class Correlator:
    def __init__(self, config, store: EventStore):
        self.store = store
        self.window = float(config.get("intelligence.story_window_hours", 6)) * 3600
        # story_id -> {"tags": set, "last_ts": float, "layers": set, "n": int}
        self._active: dict[str, dict] = {}

    def _gc(self, now: float):
        dead = [sid for sid, s in self._active.items() if now - s["last_ts"] > self.window]
        for sid in dead:
            del self._active[sid]

    def assign(self, ev: Event) -> Event:
        """Mutates ev: sets tags + story_id. Returns the event."""
        now = ev.ts
        self._gc(now)
        ev.tags = extract_tags(ev.title, ev.body, ev.layer)
        signal = {t for t in ev.tags if t != ev.layer}

        best_id, best_overlap = None, 0
        for sid, story in self._active.items():
            overlap = len(signal & story["tags"])
            if overlap > best_overlap:
                best_id, best_overlap = sid, overlap

        if best_id and best_overlap > 0:
            story = self._active[best_id]
            story["tags"] |= signal
            story["last_ts"] = now
            story["layers"].add(ev.layer)
            story["n"] += 1
            ev.story_id = best_id
        else:
            sid = uuid.uuid4().hex[:10]
            self._active[sid] = {"tags": set(signal), "last_ts": now,
                                 "layers": {ev.layer}, "n": 1}
            ev.story_id = sid
        return ev

    def corroboration(self, ev: Event) -> list[Event]:
        """Other events in this story, from other layers (independent confirmations)."""
        related = self.store.story_events(ev.story_id)
        return [e for e in related if e.id != ev.id and e.layer != ev.layer]

    def story_context(self, ev: Event) -> dict:
        related = [e for e in self.store.story_events(ev.story_id) if e.id != ev.id]
        history = self.store.similar_past(ev)
        return {
            "related": related,
            "cross_layer": [e for e in related if e.layer != ev.layer],
            "layers": sorted({e.layer for e in related} | {ev.layer}),
            "history": history,
        }
