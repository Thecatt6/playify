"""Parse LRC ("lyrics with timestamps") text.

LRCLIB returns either plain lyrics or LRC, which looks like::

    [ar:M83]
    [ti:Midnight City]
    [00:12.34] Waiting in a car
    [00:16.80][01:02.10] Waiting for a ride

The panel needs the timestamps to highlight the line being sung, and the same
parser cleans the text for the plain view. It is pure and therefore cheap to
test — the network part stays in :mod:`src.playify.services.lyrics`.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# [mm:ss], [mm:ss.xx] and [mm:ss.xxx]. Minutes are free to exceed 59: [65:30]
# is an hour and five minutes, which is how long mixes are written.
TIMESTAMP = re.compile(r"\[(\d{1,3}):(\d{1,3})(?:[.:](\d{1,3}))?\]")
# [ar:...], [ti:...], [offset:...] and friends.
METADATA = re.compile(r"^\[([a-zA-Z#]+):(.*)\]$")


def _to_seconds(minutes: str, seconds: str, fraction: str | None) -> float:
    total = int(minutes) * 60 + int(seconds)
    if fraction:
        total += int((fraction + "00")[:3]) / 1000
    return float(total)


def parse_lrc(text: str) -> list[dict[str, Any]]:
    """Return ``[{"time": seconds, "text": line}]`` sorted by time.

    Lines without timestamps (or metadata tags) are ignored; a line with several
    timestamps becomes one entry per timestamp, which is how LRC encodes a
    chorus that repeats.
    """
    entries: list[dict[str, Any]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        stamps = list(TIMESTAMP.finditer(line))
        if not stamps:
            continue
        lyrics = TIMESTAMP.sub("", line).strip()
        if not lyrics:
            continue
        for stamp in stamps:
            entries.append(
                {
                    "time": round(
                        _to_seconds(stamp.group(1), stamp.group(2), stamp.group(3)), 3
                    ),
                    "text": lyrics,
                }
            )
    entries.sort(key=lambda item: item["time"])
    return entries


def strip_lrc(text: str) -> str:
    """Return the plain text of an LRC document (metadata and stamps removed)."""
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if METADATA.match(line):
            continue
        cleaned = TIMESTAMP.sub("", line).strip()
        if not cleaned:
            continue
        if lines and lines[-1] == cleaned:
            continue  # the same chorus line repeated for another timestamp
        lines.append(cleaned)
    return "\n".join(lines)


def is_lrc(text: str) -> bool:
    return bool(text) and TIMESTAMP.search(text) is not None


def current_index(lines: Iterable[dict[str, Any]], position: float) -> int:
    """Index of the line being sung at ``position`` seconds (-1 before the first)."""
    index = -1
    for candidate, line in enumerate(lines):
        try:
            start = float(line.get("time") or 0.0)
        except (TypeError, ValueError):
            continue
        if start <= position:
            index = candidate
        else:
            break
    return index
