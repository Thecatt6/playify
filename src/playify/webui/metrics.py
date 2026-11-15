"""Prometheus text rendering and the panel's live counters.

Two tiny pieces so ``/metrics`` is testable without a bot:

* :mod:`src.playify.webui.metrics` (this module) renders samples in the
  Prometheus text format;
* counters live in the process and are read by :func:`collect`, which asks the
  bot, the error feed and the websocket registry for their current values.
"""

from __future__ import annotations

import math
import time
from typing import Any, Iterable

PLAYBACK_STATES = {"idle": 0, "playing": 1, "paused": 2, "keepalive": 3, "reconnecting": 4}


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if math.isnan(number) or math.isinf(number):
        return "0"
    if number == int(number):
        return str(int(number))
    return f"{number:.3f}"


def _labels(labels: dict[str, Any] | None) -> str:
    if not labels:
        return ""
    body = ",".join(f'{key}="{_escape(value)}"' for key, value in sorted(labels.items()))
    return "{" + body + "}"


def render_prometheus(samples: Iterable[dict[str, Any]]) -> str:
    """Render samples as Prometheus text exposition format."""
    lines: list[str] = []
    described: set[str] = set()
    for sample in samples:
        name = str(sample.get("name") or "").strip()
        if not name:
            continue
        if name not in described:
            described.add(name)
            if sample.get("help"):
                lines.append(f"# HELP {name} {sample['help']}")
            lines.append(f"# TYPE {name} {sample.get('type', 'gauge')}")
        lines.append(f"{name}{_labels(sample.get('labels'))} {_fmt(sample.get('value', 0))}")
    return "\n".join(lines) + ("\n" if lines else "")


def collect(
    *,
    bot: Any,
    ctx: Any,
    ws_connections: int,
    feeding: dict[str, Any],
    stats: dict[str, Any],
    guilds: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Gather every sample /metrics publishes.

    Deliberately aggregate-only: no user ids, no guild names, no track titles,
    because this endpoint can be scraped without a Discord session.
    """
    started_at = getattr(ctx, "started_at", None)
    uptime = max(0.0, time.time() - started_at) if started_at else 0.0

    samples: list[dict[str, Any]] = [
        {
            "name": "playify_uptime_seconds",
            "help": "Seconds since the bot process started.",
            "type": "gauge",
            "value": uptime,
        },
        {
            "name": "playify_guilds",
            "help": "Guilds the bot is connected to.",
            "type": "gauge",
            "value": len(getattr(bot, "guilds", None) or []),
        },
        {
            "name": "playify_voice_clients",
            "help": "Active voice connections.",
            "type": "gauge",
            "value": len(getattr(bot, "voice_clients", None) or []),
        },
        {
            "name": "playify_webui_connections",
            "help": "Open Web UI websocket connections.",
            "type": "gauge",
            "value": ws_connections,
        },
        {
            "name": "playify_tracks_played_total",
            "help": "Tracks started since the bot came up.",
            "type": "counter",
            "value": stats.get("tracks_played", 0),
        },
        {
            "name": "playify_warnings_total",
            "help": "Warnings seen since the bot came up.",
            "type": "counter",
            "value": feeding.get("warnings", 0),
        },
        {
            "name": "playify_errors_total",
            "help": "Errors seen since the bot came up.",
            "type": "counter",
            "value": feeding.get("errors", 0),
        },
    ]

    for guild in guilds:
        labels = {"guild_id": str(guild.get("guild_id", ""))}
        samples.append(
            {
                "name": "playify_voice_listeners",
                "help": "People sitting in the bot's voice channel.",
                "type": "gauge",
                "value": guild.get("listeners", 0),
                "labels": labels,
            }
        )
        samples.append(
            {
                "name": "playify_queue_tracks",
                "help": "Tracks waiting in the queue.",
                "type": "gauge",
                "value": guild.get("queue", 0),
                "labels": labels,
            }
        )
        samples.append(
            {
                "name": "playify_playback_state",
                "help": "0 idle, 1 playing, 2 paused, 3 keepalive, 4 reconnecting.",
                "type": "gauge",
                "value": PLAYBACK_STATES.get(str(guild.get("state")), 0),
                "labels": labels,
            }
        )

    return samples
