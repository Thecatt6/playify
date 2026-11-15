"""Process-wide counters that are cheap to bump and easy to scrape."""

from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.Lock()
_COUNTERS: dict[str, int] = {"tracks_played": 0, "reconnects": 0}


def inc(name: str, amount: int = 1) -> None:
    with _LOCK:
        _COUNTERS[name] = _COUNTERS.get(name, 0) + int(amount)


def get(name: str, default: int = 0) -> int:
    with _LOCK:
        return int(_COUNTERS.get(name, default))


def snapshot() -> dict[str, Any]:
    with _LOCK:
        return dict(_COUNTERS)


def reset() -> None:
    with _LOCK:
        for key in _COUNTERS:
            _COUNTERS[key] = 0
