"""Persistent JSON state: seen-item dedupe, registered chats, baselines, mute status."""

import json
import os
import tempfile
import time
from pathlib import Path

SEEN_CAP = 1200  # max remembered ids per namespace


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data = {"seen": {}, "chats": [], "kv": {}, "baselined": {}}
        self._seen_sets: dict[str, set] = {}
        self._dirty = False
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    for key in self._data:
                        if key in loaded:
                            self._data[key] = loaded[key]
            except (json.JSONDecodeError, OSError):
                # Corrupt state: keep a copy and start fresh rather than crash-loop.
                try:
                    self.path.rename(self.path.with_suffix(".corrupt"))
                except OSError:
                    pass

    # ---- seen / dedupe -------------------------------------------------
    def _seen_set(self, ns: str) -> set:
        if ns not in self._seen_sets:
            self._seen_sets[ns] = set(self._data["seen"].get(ns, []))
        return self._seen_sets[ns]

    def new_ids(self, ns: str, ids) -> list:
        """Return ids not seen before in this namespace, and mark them seen."""
        seen = self._seen_set(ns)
        fresh = []
        bucket = self._data["seen"].setdefault(ns, [])
        for i in ids:
            i = str(i)
            if i and i not in seen:
                fresh.append(i)
                seen.add(i)
                bucket.append(i)
        if len(bucket) > SEEN_CAP:
            del bucket[: len(bucket) - SEEN_CAP]
            self._seen_sets[ns] = set(bucket)
        if fresh:
            self._dirty = True
        return fresh

    def is_seen(self, ns: str, item_id: str) -> bool:
        return str(item_id) in self._seen_set(ns)

    # ---- baseline flags ------------------------------------------------
    def is_baselined(self, ns: str) -> bool:
        return bool(self._data["baselined"].get(ns))

    def set_baselined(self, ns: str):
        self._data["baselined"][ns] = True
        self._dirty = True

    # ---- key/value -----------------------------------------------------
    def kv_get(self, key: str, default=None):
        return self._data["kv"].get(key, default)

    def kv_set(self, key: str, value):
        self._data["kv"][key] = value
        self._dirty = True

    # ---- chats ---------------------------------------------------------
    @property
    def chats(self) -> list:
        return self._data["chats"]

    def add_chat(self, chat_id: int, title: str = "") -> bool:
        if any(c["id"] == chat_id for c in self._data["chats"]):
            return False
        self._data["chats"].append({"id": chat_id, "title": title})
        self._dirty = True
        return True

    def remove_chat(self, chat_id: int) -> bool:
        before = len(self._data["chats"])
        self._data["chats"] = [c for c in self._data["chats"] if c["id"] != chat_id]
        if len(self._data["chats"]) != before:
            self._dirty = True
            return True
        return False

    # ---- mute ----------------------------------------------------------
    def mute_until(self) -> float:
        return float(self._data["kv"].get("mute_until", 0) or 0)

    def set_mute(self, minutes: float):
        self._data["kv"]["mute_until"] = time.time() + minutes * 60 if minutes > 0 else 0
        self._dirty = True

    def is_muted(self) -> bool:
        return time.time() < self.mute_until()

    # ---- persistence ---------------------------------------------------
    def save(self, force: bool = False):
        if not (self._dirty or force):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, separators=(",", ":"))
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
