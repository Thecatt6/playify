"""Voice connection health: dependency checks and zombie-connection self-healing.

Discord voice connections can die silently: the gateway still lists the bot in a
voice channel (so Discord and the Web UI both show it there) while the underlying
:class:`discord.VoiceClient` is already disconnected. In that state audio stops
playing and every command complains about permissions or about not being in the
channel, until a human runs ``/reconnect``.

This module keeps a light watchdog alive that detects exactly that situation and
rebuilds the connection on its own, resuming the current track at the position it
had reached. It also reports whether the voice dependencies are usable, because a
missing ``PyNaCl``/``davey``/``ffmpeg`` produces the same "no audio" symptom.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import re
import shutil
import time
from typing import Any

logger = logging.getLogger("playify.voice")

# The watchdog is intentionally cheap: a couple of attribute reads per guild.
HEAL_INTERVAL = 25
# Never hammer Discord: at most N attempts per guild inside this window.
HEAL_WINDOW = 300
HEAL_MAX_ATTEMPTS = 3

_task: asyncio.Task | None = None
_attempts: dict[int, list[float]] = {}
# Longest we tolerate a call we cannot encrypt for before rebuilding the session.
NO_ENCRYPT_GRACE = 30
_unencrypted_since: dict[Any, float] = {}


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:  # pragma: no cover - broken/partial installs
        return False


def _module_version(distribution: str) -> str | None:
    """Installed version of a distribution, or ``None`` when it is absent."""
    try:
        from importlib.metadata import version

        return version(distribution)
    except Exception:  # pragma: no cover - broken/partial installs
        return None


def _version_tuple(text: str | None) -> tuple[int, ...]:
    """``"2.7.1"`` -> ``(2, 7, 1)``; stops at the first non-numeric chunk."""
    if not text:
        return ()
    parts: list[int] = []
    for chunk in re.split(r"[.\-+]", text):
        match = re.match(r"\d+", chunk)
        if not match:
            break
        parts.append(int(match.group()))
    return tuple(parts)


def dave_status() -> dict[str, Any]:
    """Whether the installed ``davey`` can talk to the installed discord.py.

    ``davey`` 0.2 (2026-11-02) changed how DAVE encryption keys are rotated: the
    key switch now needs ``prepare_transition()`` / ``execute_transition()``,
    which discord.py 2.7.x never calls. The bot keeps playing, the green
    speaking ring lights up, and every listener hears silence because they
    cannot decrypt the audio.
    """
    davey = _module_version("davey")
    discord_py = _module_version("discord.py")
    compatible = True
    reason = None
    if davey and _version_tuple(davey) >= (0, 2):
        if not discord_py or _version_tuple(discord_py) < (2, 8):
            compatible = False
            reason = (
                f"davey {davey} with discord.py {discord_py or '?'} cannot rotate "
                "DAVE keys: listeners cannot decrypt the audio (the bot plays, "
                "nobody hears it). Pin davey below 0.2."
            )
    return {
        "davey": davey,
        "discord_py": discord_py,
        "compatible": compatible,
        "reason": reason,
    }


def dave_runtime_state(voice_client: Any) -> dict[str, Any]:
    """What the *live* DAVE (E2EE voice) session says.

    Everything is read defensively from discord.py 2.7 internals: a bot that is
    not encrypting in an encrypted call is heard by nobody, and that is
    otherwise invisible from the outside.
    """
    state: dict[str, Any] = {
        "protocol": 0,
        "max_protocol": 0,
        "session": False,
        "ready": False,
        "can_encrypt": False,
        "privacy_code": None,
    }
    connection = getattr(voice_client, "_connection", None)
    if connection is None:
        return state

    def read(attribute: str, default: Any = None) -> Any:
        try:
            return getattr(connection, attribute, default)
        except Exception:  # pragma: no cover - discord.py internals
            return default

    state["protocol"] = read("dave_protocol_version", 0) or 0
    state["max_protocol"] = read("max_dave_protocol_version", 0) or 0
    session = read("dave_session", None)
    state["session"] = session is not None
    if session is not None:
        try:
            state["ready"] = bool(getattr(session, "ready", False))
        except Exception:  # pragma: no cover - discord.py internals
            state["ready"] = False
        try:
            code = getattr(session, "voice_privacy_code", None)
            state["privacy_code"] = str(code) if code else None
        except Exception:  # pragma: no cover - discord.py internals
            state["privacy_code"] = None
    try:
        state["can_encrypt"] = bool(read("can_encrypt", False))
    except Exception:  # pragma: no cover - discord.py internals
        state["can_encrypt"] = False
    return state


def voice_dependencies() -> dict[str, Any]:
    """Everything voice playback needs, resolved once."""
    dave = dave_status()
    return {
        "ffmpeg": shutil.which("ffmpeg"),
        "pynacl": _has_module("nacl"),
        "davey": _has_module("davey"),
        "davey_version": dave["davey"],
        "discord_py_version": dave["discord_py"],
        "dave_compatible": dave["compatible"],
        "yt_dlp": _has_module("yt_dlp"),
    }


def voice_readiness_problems() -> list[str]:
    """Human-readable list of what is broken (empty when voice is fine)."""
    deps = voice_dependencies()
    problems = []
    if not deps["ffmpeg"]:
        problems.append(
            "ffmpeg was not found in PATH: no audio can be decoded (install ffmpeg)"
        )
    if not deps["pynacl"]:
        problems.append(
            "PyNaCl is missing: discord.py cannot send voice (pip install PyNaCl)"
        )
    if not deps["davey"]:
        problems.append(
            "davey is missing: voice does not work on E2EE channels (pip install davey)"
        )
    elif not deps.get("dave_compatible", True):
        problems.append(
            f"davey {deps.get('davey_version')} breaks DAVE encryption with discord.py "
            f"{deps.get('discord_py_version') or '?'}: the bot plays but nobody hears it "
            '(pip install "davey>=0.1.6,<0.2")'
        )
    return problems


def log_voice_readiness() -> bool:
    """Log the voice readiness once at startup. Returns ``True`` when usable."""
    problems = voice_readiness_problems()
    deps = voice_dependencies()
    if problems:
        logger.error("=" * 70)
        logger.error("VOICE SUPPORT IS NOT READY — playback will stay silent:")
        for problem in problems:
            logger.error("  • %s", problem)
        logger.error("Fix with: pip install -r requirements.txt && apt install ffmpeg")
        logger.error("Run `python tools/doctor.py` for a full report.")
        logger.error("=" * 70)
        return False

    logger.info(
        "Voice support ready (ffmpeg=%s, PyNaCl=yes, davey=yes, yt-dlp=%s).",
        deps["ffmpeg"],
        "yes" if deps["yt_dlp"] else "no",
    )
    return True


# ---------------------------------------------------------------------------
# Self-healing
# ---------------------------------------------------------------------------


def voice_channel_of(guild: Any) -> Any:
    """Where the gateway says the bot is, even if the voice client is dead."""
    me = getattr(guild, "me", None)
    channel = getattr(getattr(me, "voice", None), "channel", None)
    if channel is not None:
        return channel
    return getattr(getattr(guild, "voice_client", None), "channel", None)


def is_connected(voice_client: Any) -> bool:
    check = getattr(voice_client, "is_connected", None)
    try:
        return bool(check()) if callable(check) else bool(check)
    except Exception:  # pragma: no cover - defensive
        return False


def playback_position(player: Any, now: float | None = None) -> float:
    now = time.time() if now is None else now
    start = float(getattr(player, "start_time", 0) or 0)
    started = getattr(player, "playback_started_at", None)
    if started:
        speed = float(getattr(player, "playback_speed", 1.0) or 1.0)
        start += max(0.0, now - float(started)) * speed
    return max(0.0, start)


def needs_encryption_heal(guild: Any, player: Any) -> bool:
    """Is the bot playing into an encrypted call it cannot encrypt for?

    In that state the bot sends plain audio, every client silently discards it,
    and nothing in the logs complains: exactly the "it plays but nobody hears
    it" report.
    """
    voice_client = getattr(guild, "voice_client", None) or getattr(
        player, "voice_client", None
    )
    if voice_client is None or not is_connected(voice_client):
        return False
    if not (
        getattr(player, "current_info", None)
        or getattr(player, "playback_started_at", None)
    ):
        return False
    state = dave_runtime_state(voice_client)
    return bool(state["protocol"]) and not state["can_encrypt"]


def encryption_heal_due(
    guild_id: Any, guild: Any, player: Any, now: float | None = None
) -> bool:
    """Debounced: only after the problem persisted for ``NO_ENCRYPT_GRACE`` seconds.

    A fresh connection needs a moment to finish the MLS handshake, so healing on
    the first bad sample would flap.
    """
    now = time.time() if now is None else now
    if not needs_encryption_heal(guild, player):
        _unencrypted_since.pop(guild_id, None)
        return False
    since = _unencrypted_since.setdefault(guild_id, now)
    return now - since >= NO_ENCRYPT_GRACE


def _should_heal(guild: Any, state: Any, player: Any) -> bool:
    """Heal only if the bot is supposed to be in voice but the client is dead."""
    if voice_channel_of(guild) is None:
        return False
    if is_connected(getattr(guild, "voice_client", None)):
        return False
    if getattr(player, "is_cleaning", False) or getattr(player, "is_reconnecting", False):
        return False
    return bool(
        getattr(player, "current_info", None)
        or getattr(player, "is_playing_silence", False)
        or getattr(state, "_24_7_mode", False)
    )


async def heal_guild(guild: Any, player: Any, *, reason: str = "dead voice client") -> bool:
    """Rebuild a dead voice connection and resume playback where it stopped."""
    from .playback import play_audio
    from .voice import safe_stop

    channel = voice_channel_of(guild)
    if channel is None:
        return False
    if getattr(player, "is_cleaning", False) or getattr(player, "is_reconnecting", False):
        # Something else (a /reconnect, a track change) is already on it.
        return False

    guild_id = getattr(guild, "id", 0)
    now = time.time()
    recent = [ts for ts in _attempts.get(guild_id, []) if now - ts < HEAL_WINDOW]
    if len(recent) >= HEAL_MAX_ATTEMPTS:
        if len(recent) == HEAL_MAX_ATTEMPTS:
            logger.warning(
                "[%s] Voice connection keeps dying: pausing self-heal for %ss.",
                guild_id,
                HEAL_WINDOW,
            )
            _attempts[guild_id] = recent + [now]
        return False
    _attempts[guild_id] = recent + [now]

    had_track = bool(getattr(player, "current_info", None))
    position = playback_position(player) if had_track else 0.0
    was_playing = bool(getattr(player, "playback_started_at", None))

    logger.warning(
        "[%s] Rebuilding the voice connection (%s) in '%s'%s.",
        guild_id,
        reason,
        getattr(channel, "name", "voice"),
        f", resuming at {position:.1f}s" if had_track else "",
    )

    silence_task = getattr(player, "silence_task", None)
    if silence_task is not None and not silence_task.done():
        silence_task.cancel()
    player.is_playing_silence = False

    player.is_cleaning = True
    try:
        vc = getattr(guild, "voice_client", None)
        if vc is not None:
            if is_connected(vc) and (vc.is_playing() or vc.is_paused()):
                await safe_stop(vc)
            await vc.disconnect(force=True)
    except Exception as exc:  # pragma: no cover - network dependent
        logger.warning("[%s] Voice self-heal: disconnect failed: %s", guild_id, exc)
    finally:
        player.is_cleaning = False

    player.voice_client = None
    try:
        new_vc = await channel.connect(reconnect=True, timeout=20.0, self_deaf=False)
    except Exception as exc:
        logger.error("[%s] Voice self-heal: reconnection failed: %s", guild_id, exc)
        return False

    player.voice_client = new_vc
    logger.info("[%s] Voice connection restored.", guild_id)

    if had_track and (was_playing or position > 0):
        player.current_task = asyncio.create_task(
            play_audio(guild.id, seek_time=position, is_a_loop=True)
        )
    return True


async def _watch(bot_instance: Any, interval: float) -> None:
    from ..core import guild_states

    try:
        await bot_instance.wait_until_ready()
    except Exception:  # pragma: no cover - shutdown races
        return

    while not bot_instance.is_closed():
        try:
            for guild_id, state in list(guild_states.items()):
                guild = bot_instance.get_guild(guild_id)
                if guild is None:
                    continue
                player = getattr(state, "music_player", None)
                if player is None:
                    continue
                if encryption_heal_due(guild_id, guild, player):
                    healed = await heal_guild(
                        guild,
                        player,
                        reason="the DAVE session cannot encrypt, nobody can hear the bot",
                    )
                    if healed:
                        _unencrypted_since.pop(guild_id, None)
                    continue
                if not _should_heal(guild, state, player):
                    continue
                await heal_guild(guild, player)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Voice watchdog error: %s", exc, exc_info=True)
        await asyncio.sleep(interval)


def start_voice_watchdog(
    bot_instance: Any | None = None, interval: float = HEAL_INTERVAL
) -> asyncio.Task | None:
    """Start the background watchdog once (safe to call on every ``on_ready``)."""
    global _task
    if _task is not None and not _task.done():
        return _task
    if bot_instance is None:
        from ..core import bot as bot_instance  # noqa: PLC0415 - lazy on purpose
    target = bot_instance
    try:
        _task = target.loop.create_task(_watch(target, interval))
    except Exception as exc:  # pragma: no cover - no running loop yet
        logger.error("Could not start the voice watchdog: %s", exc)
        _task = None
    return _task


async def stop_voice_watchdog() -> None:
    global _task
    task = _task
    _task = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
