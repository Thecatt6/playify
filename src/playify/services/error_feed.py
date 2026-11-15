"""Keep the last warnings and errors around so the panel can show them.

A headless bot fails quietly: a track that will not start, a Spotify token that
expired, a voice reconnect that never completed — all of it lands in the log
file, which nobody reads while sitting in a voice channel. This module keeps a
small ring buffer of the recent WARNING/ERROR records, fed by a logging handler,
and the web panel shows it as a discreet diagnostics card.

The buffer is bounded (``capacity`` records, message truncated) because log
messages can be long and a runaway loop can produce thousands of them.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Iterable

DEFAULT_CAPACITY = 50
MAX_MESSAGE = 300


class ErrorFeed:
    """Bounded, thread-safe ring buffer of recent warnings and errors."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY, clock=time.time) -> None:
        self.capacity = max(1, int(capacity))
        self._entries: deque[dict[str, Any]] = deque(maxlen=self.capacity)
        self._lock = threading.Lock()
        self._clock = clock
        self._counts = {"warning": 0, "error": 0}

    def record(self, level: str, logger_name: str, message: str) -> None:
        entry = {
            "time": float(self._clock()),
            "level": level,
            "logger": str(logger_name),
            "message": str(message)[:MAX_MESSAGE],
        }
        with self._lock:
            self._entries.append(entry)
            if level == "error":
                self._counts["error"] += 1
            else:
                self._counts["warning"] += 1

    def record_from_log(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        level = "error" if record.levelno >= logging.ERROR else "warning"
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - broken format string
            message = str(record.msg)
        self.record(level, record.name, message)

    def snapshot(self, limit: int = 5) -> dict[str, Any]:
        with self._lock:
            recent: Iterable[dict[str, Any]] = list(self._entries)[-max(1, limit):]
            entries = [dict(item) for item in recent]
            counts = dict(self._counts)
        return {
            "total": counts["warning"] + counts["error"],
            "warnings": counts["warning"],
            "errors": counts["error"],
            "recent": entries,
        }

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._counts = {"warning": 0, "error": 0}


class ErrorFeedHandler(logging.Handler):
    """Logging handler that mirrors warnings and errors into an :class:`ErrorFeed`."""

    def __init__(self, feed: ErrorFeed, level: int = logging.WARNING) -> None:
        super().__init__(level=level)
        self.feed = feed

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        try:
            self.feed.record_from_log(record)
        except Exception:  # logging must never raise
            pass


# The process-wide feed used by the bot and read by the web panel.
FEED = ErrorFeed()


def install(feed: ErrorFeed | None = None, level: int = logging.WARNING) -> ErrorFeed:
    """Attach the feed to the root logger once and return it."""
    target = feed or FEED
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, ErrorFeedHandler) and handler.feed is target:
            return target
    root.addHandler(ErrorFeedHandler(target, level=level))
    return target


def recent(feed: ErrorFeed | None = None, limit: int = 5) -> dict[str, Any]:
    return (feed or FEED).snapshot(limit=limit)
