"""config.yaml loader — source of truth for every endpoint, handle, threshold and keyword."""

import os
import re
from pathlib import Path

import yaml

# ${VAR} or ${VAR:-default}
_ENV_RE = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}")


def _expand_env(value):
    if isinstance(value, str):
        def sub(m):
            return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else "")
        return _ENV_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class Config:
    """Read-only dotted-path access over the parsed YAML config."""

    def __init__(self, data: dict):
        self._data = data or {}

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        raw = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
        return cls(_expand_env(data))

    def get(self, dotted: str, default=None):
        node = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, dotted: str) -> dict:
        val = self.get(dotted, {})
        return val if isinstance(val, dict) else {}

    def getlist(self, dotted: str) -> list:
        val = self.get(dotted, [])
        if val is None:
            return []
        return val if isinstance(val, list) else [val]

    def getbool(self, dotted: str, default: bool = False) -> bool:
        val = self.get(dotted, default)
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(val)

    def getfloat(self, dotted: str, default: float = 0.0) -> float:
        try:
            return float(self.get(dotted, default))
        except (TypeError, ValueError):
            return default

    def getint(self, dotted: str, default: int = 0) -> int:
        try:
            return int(float(self.get(dotted, default)))
        except (TypeError, ValueError):
            return default
