"""Build the JSON payload the Web UI renders.

Everything here is duck-typed and side-effect free so snapshots can be produced
on every WebSocket tick without touching Discord.
"""

from __future__ import annotations

import time
from typing import Any

AUDIO_FILTER_KEYS = [
    "slowed",
    "spedup",
    "nightcore",
    "reverb",
    "8d",
    "muffled",
    "bassboost",
    "earrape",
]

FILTER_LABELS = {
    "slowed": "Slowed",
    "spedup": "Sped Up",
    "nightcore": "Nightcore",
    "reverb": "Reverb",
    "8d": "8D Audio",
    "muffled": "Muffled",
    "bassboost": "Bass Boost",
    "earrape": "Earrape",
}


def _first_thumbnail(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, (list, tuple)):
        for candidate in value:
            if isinstance(candidate, dict) and candidate.get("url"):
                return str(candidate["url"])
    return None


def _requester_name(value: Any) -> str | None:
    if value is None:
        return None
    name = (
        getattr(value, "display_name", None)
        or getattr(value, "name", None)
        or getattr(value, "username", None)
    )
    return str(name) if name else None


def track_payload(track: Any) -> dict[str, Any] | None:
    """Normalise a queue item (dict or ``LazySearchItem``) for the UI."""
    if track is None:
        return None

    if isinstance(track, dict):
        resolved = track.get("resolved_info") or {}
        title = (
            track.get("original_title")
            or track.get("title")
            or resolved.get("title")
            or "Unknown title"
        )
        artist = (
            track.get("original_artist")
            or track.get("artist")
            or track.get("uploader")
            or ""
        )
        payload = {
            "title": str(title),
            "artist": str(artist) if artist else "",
            "duration": float(track.get("duration") or 0),
            "thumbnail": _first_thumbnail(track.get("thumbnail")),
            "url": track.get("original_url")
            or track.get("webpage_url")
            or track.get("url"),
            "platform": track.get("original_platform") or track.get("source_type"),
            "requester": _requester_name(track.get("requester")),
        }
        return payload

    # LazySearchItem (or any object exposing the same attributes).
    resolved = getattr(track, "resolved_info", None) or {}
    title = (
        resolved.get("original_title")
        or resolved.get("title")
        or getattr(track, "title", None)
        or "Unknown title"
    )
    artist = (
        resolved.get("original_artist")
        or resolved.get("artist")
        or getattr(track, "artist", None)
        or ""
    )
    payload = {
        "title": str(title),
        "artist": str(artist) if artist else "",
        "duration": float(getattr(track, "duration", 0) or resolved.get("duration") or 0),
        "thumbnail": _first_thumbnail(getattr(track, "thumbnail", None)),
        "url": getattr(track, "webpage_url", None) or getattr(track, "url", None),
        "platform": getattr(track, "original_platform", None),
        "requester": _requester_name(getattr(track, "requester", None)),
    }
    return payload


def playback_position(player: Any, now: float | None = None) -> float:
    if player is None:
        return 0.0
    now = time.time() if now is None else now
    base = float(getattr(player, "start_time", 0) or 0)
    started_at = getattr(player, "playback_started_at", None)
    if started_at:
        speed = float(getattr(player, "playback_speed", 1.0) or 1.0)
        return base + max(0.0, now - float(started_at)) * speed
    return base


def _playback_state(player: Any, vc: Any) -> str:
    if player is None or vc is None:
        return "idle"
    if getattr(player, "is_paused_by_leave", False):
        return "paused"
    is_paused = getattr(vc, "is_paused", None)
    if callable(is_paused) and is_paused():
        return "paused"
    is_playing = getattr(vc, "is_playing", None)
    if callable(is_playing) and is_playing():
        if getattr(player, "is_playing_silence", False):
            return "keepalive"
        return "playing"
    return "idle"


def queue_payload(player: Any, limit: int = 500) -> dict[str, Any]:
    if player is None:
        return {"items": [], "count": 0, "total_duration": 0.0}
    queue = getattr(player, "queue", None)
    items = list(getattr(queue, "_queue", []) or [])
    total_duration = 0.0
    payload_items = []
    for index, item in enumerate(items):
        track = track_payload(item)
        if track is None:
            continue
        total_duration += track["duration"] or 0
        if index < limit:
            payload_items.append({"index": index, **track})
    return {
        "items": payload_items,
        "count": len(items),
        "total_duration": total_duration,
    }


def build_snapshot(
    state: Any,
    session: Any,
    *,
    now: float | None = None,
    started_at: float | None = None,
) -> dict[str, Any]:
    """Full state of one guild, ready to be serialised and sent to the browser."""
    now = time.time() if now is None else now
    player = getattr(state, "music_player", None)
    # The player's client is the one that emits audio; the session may still
    # hold an older one after a reconnect.
    vc = getattr(player, "voice_client", None) or getattr(session, "voice_client", None)

    current = track_payload(getattr(player, "current_info", None))
    position = playback_position(player, now)
    duration = float((current or {}).get("duration") or 0)

    active_filters = sorted(getattr(state, "server_filters", set()) or set())
    playback_state = _playback_state(player, vc)
    if current is None and playback_state == "playing":
        playback_state = "idle"

    if current is None:
        # Nothing to show: the panel must not display a running clock.
        position = 0.0
    elif playback_state != "playing":
        # A paused or finished track keeps the position it reached; only a
        # playing one advances (otherwise the timer runs past the end).
        position = float(getattr(player, "start_time", 0) or 0)
    if duration:
        position = min(position, duration)

    volume = float(getattr(player, "volume", 1.0) or 0.0)

    return {
        "server_time": now,
        "bot_uptime": (now - started_at) if started_at else 0,
        "session": session.public(),
        "playback": {
            "state": playback_state,
            "is_live": bool(getattr(player, "is_current_live", False)),
            "position": position,
            "current": current,
            "requester": (current or {}).get("requester"),
        },
        "queue": queue_payload(player),
        "history": {
            "count": len(getattr(player, "history", []) or []),
            "previous": track_payload(
                (getattr(player, "history", []) or [None])[-1]
            )
            if getattr(player, "history", None)
            else None,
        },
        "settings": {
            "volume": round(volume * 100),
            "default_volume": round(
                float(getattr(state, "default_volume", 1.0) or 0.0) * 100
            ),
            "loop": bool(getattr(player, "loop_current", False)),
            "autoplay": bool(getattr(player, "autoplay_enabled", False)),
            "mode_24_7": bool(getattr(state, "_24_7_mode", False)),
            "kawaii": str(getattr(getattr(state, "locale", None), "value", "")) == "en-x-kawaii",
            "filters": active_filters,
        },
        "filters": [
            {"id": key, "label": FILTER_LABELS.get(key, key.title())}
            for key in AUDIO_FILTER_KEYS
        ],
        "capabilities": {
            "can_seek": bool(current) and not bool(getattr(player, "is_current_live", False)),
            "can_skip": bool(current) or bool(queue_payload(player)["count"]),
            "is_manager": bool(getattr(session, "is_manager", False)),
        },
    }
