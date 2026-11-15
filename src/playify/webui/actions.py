"""Player actions triggered from the Web UI.

Every handler receives the already-authorised :class:`VoiceSession` of the
visitor, so access control happens *before* anything here runs.

The Playify modules are imported lazily inside the functions: this keeps the web
package importable on its own (no discord.py, no yt-dlp) and avoids circular
imports with ``src.playify.core``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from .snapshot import playback_position, track_payload

logger = logging.getLogger("playify.webui")

MAX_QUERY_LENGTH = 500
SEARCH_CACHE_TTL = 90.0
_search_cache: dict[tuple[int, str], tuple[float, list[dict[str, Any]]]] = {}


class ActionError(Exception):
    """A user-facing error raised by an action handler."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _state(guild_id: int) -> Any:
    from ..core import get_guild_state

    return get_guild_state(guild_id)


def _player(guild_id: int) -> Any:
    return _state(guild_id).music_player


def _is_connected(vc: Any) -> bool:
    if vc is None:
        return False
    check = getattr(vc, "is_connected", None)
    if callable(check):
        try:
            return bool(check())
        except Exception:  # pragma: no cover - defensive
            return False
    return True


def _voice_client(session: Any, player: Any) -> Any:
    for candidate in (getattr(session, "voice_client", None), getattr(player, "voice_client", None)):
        if _is_connected(candidate):
            return candidate
    raise ActionError("Playify is not connected to a voice channel.", status=409)


def _require_playback(vc: Any) -> None:
    is_playing = bool(getattr(vc, "is_playing", lambda: False)())
    is_paused = bool(getattr(vc, "is_paused", lambda: False)())
    if not (is_playing or is_paused):
        raise ActionError("Nothing is playing right now.", status=409)


def _log_task_error(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("Web UI background task failed: %s", error, exc_info=error)


def _schedule(coro) -> None:
    try:
        task = asyncio.ensure_future(coro)
    except RuntimeError:  # pragma: no cover - no running loop
        return
    task.add_done_callback(_log_task_error)


def _refresh_controller(ctx: Any, guild_id: int) -> None:
    """Keep the persistent Discord controller message in sync with the Web UI."""
    try:
        # Errors are reported to the controller channel, so make sure the player
        # knows which text channel to talk to even if the Web UI acted first.
        state = _state(guild_id)
        player = state.music_player
        if getattr(player, "text_channel", None) is None and state.controller_channel_id:
            channel = ctx.bot.get_channel(state.controller_channel_id)
            if channel is not None:
                player.text_channel = channel

        from ..ui.controller import update_controller
    except Exception:  # pragma: no cover - the UI package is always available in prod
        return
    _schedule(update_controller(ctx.bot, guild_id))


def _persist_state() -> None:
    try:
        from ..core import save_all_states
    except Exception:  # pragma: no cover
        return
    _schedule(save_all_states())


async def _insert_items(player: Any, items: list[Any], *, front: bool) -> None:
    lock = getattr(player, "queue_lock", None)

    async def _apply() -> None:
        existing: list[Any] = []
        while not player.queue.empty():
            existing.append(player.queue.get_nowait())
        ordered = list(items) + existing if front else existing + list(items)
        new_queue: asyncio.Queue = asyncio.Queue()
        for item in ordered:
            await new_queue.put(item)
        player.queue = new_queue

    if lock is not None:
        async with lock:
            await _apply()
    else:  # pragma: no cover - the real player always has a lock
        await _apply()


async def _start_playback_if_idle(ctx: Any, session: Any, player: Any, vc: Any) -> None:
    from ..services.playback import play_audio
    from ..services.voice import safe_stop

    silence_task = getattr(player, "silence_task", None)
    if getattr(player, "is_playing_silence", False) or (
        silence_task is not None and not silence_task.done()
    ):
        if silence_task is not None and not silence_task.done():
            silence_task.cancel()
        if vc.is_playing():
            await safe_stop(vc)
        player.is_playing_silence = False
        await asyncio.sleep(0.1)

    if not vc.is_playing() and not vc.is_paused():
        player.current_task = asyncio.create_task(play_audio(session.guild_id))


def _thumb(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, (list, tuple)):
        for entry in value:
            if isinstance(entry, dict) and entry.get("url"):
                return str(entry["url"])
    return None


def _video_item(video: dict[str, Any], member: Any) -> dict[str, Any]:
    return {
        "url": video.get("webpage_url") or video.get("url"),
        "title": video.get("original_title") or video.get("title") or "Unknown title",
        "artist": video.get("original_artist") or video.get("artist") or video.get("uploader"),
        "webpage_url": video.get("webpage_url") or video.get("url"),
        "thumbnail": _thumb(video.get("thumbnail")) or _thumb(video.get("thumbnails")),
        "duration": video.get("duration") or 0,
        "is_single": True,
        "requester": member,
        "original_platform": video.get("original_platform"),
        "original_url": video.get("original_url"),
        "original_title": video.get("original_title"),
        "original_artist": video.get("original_artist"),
        "source_type": video.get("source_type"),
    }


# ---------------------------------------------------------------------------
# Query resolution (URLs, playlists and plain-text searches)
# ---------------------------------------------------------------------------


class _FollowupShim:
    """Captures messages a platform processor *would* have sent on Discord."""

    def __init__(self, notices: list[str]) -> None:
        self._notices = notices

    async def send(self, *args, **kwargs) -> None:
        embed = kwargs.get("embed")
        text = None
        if embed is not None:
            text = getattr(embed, "description", None) or getattr(embed, "title", None)
        if text:
            self._notices.append(str(text))
        content = kwargs.get("content")
        if content:
            self._notices.append(str(content))


class _InteractionShim:
    """Minimal stand-in for ``discord.Interaction`` used by platform scrapers."""

    def __init__(self, session: Any, notices: list[str]) -> None:
        channel = getattr(getattr(session, "voice_client", None), "channel", None)
        self.guild = getattr(channel, "guild", None)
        self.guild_id = session.guild_id
        self.user = getattr(session, "member", None)
        self.followup = _FollowupShim(notices)

    def is_expired(self) -> bool:
        return False

    class response:  # noqa: N801 - mirrors the discord.py API surface
        @staticmethod
        def is_done() -> bool:
            return True


def _platform_for(query: str) -> tuple[Callable | None, str]:
    import re

    from ..services import platforms

    mapping = (
        (r"^(https?://)?(open\.spotify\.com)/.+$", platforms.process_spotify_url, "Spotify"),
        (
            r"^(https?://)?((www\.)?deezer\.com/(?:[a-z]{2}/)?(track|playlist|album|artist)/.+|(link\.deezer\.com)/s/.+)$",
            platforms.process_deezer_url,
            "Deezer",
        ),
        (r"^(https?://)?(music\.apple\.com)/.+$", platforms.process_apple_music_url, "Apple Music"),
        (r"^(https?://)?(www\.)?tidal\.com/.+$", platforms.process_tidal_url, "Tidal"),
        (
            r"^(https?://)?(music\.amazon\.(fr|com|co\.uk|de|es|it|jp))/.+$",
            platforms.process_amazon_music_url,
            "Amazon Music",
        ),
    )
    for pattern, processor, name in mapping:
        if re.match(pattern, query):
            return processor, name
    return None, ""


async def _youtube_search(ctx: Any, query: str, opts: dict | None = None) -> dict:
    from ..core import IS_PUBLIC_VERSION
    from ..helpers.url_utils import sanitize_query
    from ..services.voice import fetch_video_info_with_retry

    prefix = "scsearch:" if IS_PUBLIC_VERSION else "ytsearch:"
    return await fetch_video_info_with_retry(
        f"{prefix}{sanitize_query(query)}",
        ydl_opts_override=opts or {"noplaylist": True},
    )


async def _resolve_query(ctx: Any, session: Any, query: str) -> tuple[list[Any], list[str]]:
    """Turn a URL or a text query into one or more queue items."""
    import re

    from ..core import IS_PUBLIC_VERSION
    from ..helpers.url_utils import sanitize_query
    from ..models.lazy_search import LazySearchItem
    from ..services.voice import fetch_video_info_with_retry

    notices: list[str] = []
    member = getattr(session, "member", None)
    cleaned = sanitize_query(query)

    if IS_PUBLIC_VERSION and re.search(r"youtube\.com|youtu\.be|music\.youtube\.com", cleaned):
        raise ActionError(
            "YouTube links are disabled on the public build of Playify. "
            "Search for the title instead."
        )

    # --- 1. Platforms that must be converted into searchable tracks ---
    processor, platform_name = _platform_for(cleaned)
    if processor is not None:
        tracks = await processor(cleaned, _InteractionShim(session, notices))
        if not tracks:
            raise ActionError(f"No tracks were found for that {platform_name} link.")

        if len(tracks) == 1:
            name, artist = tracks[0]
            info = await _youtube_search(ctx, f"{name} {artist}".strip())
            if not info or not info.get("entries"):
                raise ActionError(f"No audio stream was found for '{name}'.")
            video = info["entries"][0]
            video["original_platform"] = platform_name
            video["original_url"] = cleaned
            video["original_title"] = name
            video["original_artist"] = artist
            return [_video_item(video, member)], notices

        items = [
            LazySearchItem(
                query_dict={"name": name, "artist": artist},
                requester=member,
                original_platform=platform_name,
            )
            for name, artist in tracks
        ]
        notices.append(f"Queued {len(items)} tracks from {platform_name}.")
        return items, notices

    # --- 2. Direct links (YouTube, SoundCloud, Bandcamp, audio files…) ---
    direct_platform = re.match(
        r"^(https?://)?((www|m)\.)?(youtube\.com|youtu\.be|music\.youtube\.com|soundcloud\.com|twitch\.tv)|([^\.]+)\.bandcamp\.com/.+$",
        cleaned,
    )
    direct_file = re.match(
        r"^(https?://).+\.(mp3|wav|ogg|m4a|mp4|webm|flac)(\?.+)?$", cleaned, re.IGNORECASE
    )
    if direct_platform or direct_file:
        info = await fetch_video_info_with_retry(
            cleaned, ydl_opts_override={"extract_flat": True, "noplaylist": False}
        )
        entries = list(info.get("entries") or []) if isinstance(info, dict) else []
        if len(entries) > 1:
            items = [
                {
                    "url": entry.get("webpage_url") or entry.get("url"),
                    "title": entry.get("title") or "Loading…",
                    "artist": entry.get("uploader") or "",
                    "thumbnail": _thumb(entry.get("thumbnail")),
                    "duration": entry.get("duration") or 0,
                    "requester": member,
                }
                for entry in entries
                if entry.get("url") or entry.get("webpage_url")
            ]
            notices.append(f"Queued {len(items)} tracks from the playlist.")
            return items, notices

        # A single video / track: re-fetch without ``extract_flat`` so the UI
        # gets a real duration and artwork.
        entry = None
        if entries:
            entry = entries[0]
        elif isinstance(info, dict) and (info.get("webpage_url") or info.get("url")):
            entry = info

        single_url = (entry or {}).get("webpage_url") or (entry or {}).get("url")
        if single_url:
            try:
                full = await fetch_video_info_with_retry(
                    single_url, ydl_opts_override={"noplaylist": True}
                )
            except Exception as exc:  # pragma: no cover - network dependent
                logger.info("Full metadata fetch failed for %s: %s", single_url, exc)
                full = entry
            return [_video_item(full or entry, member)], notices
        raise ActionError("That link could not be read.")

    # --- 3. Plain-text search ---
    info = await _youtube_search(ctx, cleaned)
    entries = list(info.get("entries") or []) if isinstance(info, dict) else []
    if not entries:
        raise ActionError("No results found for that search.")
    return [_video_item(entries[0], member)], notices


# ---------------------------------------------------------------------------
# Search endpoint
# ---------------------------------------------------------------------------


async def search(ctx: Any, session: Any, query: str, limit: int = 8) -> list[dict[str, Any]]:
    from ..core import IS_PUBLIC_VERSION
    from ..helpers.url_utils import sanitize_query
    from ..services.voice import fetch_video_info_with_retry

    query = sanitize_query(query or "")
    if len(query) < 2:
        return []
    if len(query) > MAX_QUERY_LENGTH:
        query = query[:MAX_QUERY_LENGTH]

    limit = max(1, min(int(limit), 12))
    cache_key = (int(session.guild_id), query.lower())
    cached = _search_cache.get(cache_key)
    if cached and time.time() - cached[0] < SEARCH_CACHE_TTL:
        return cached[1][:limit]

    prefix = "scsearch" if IS_PUBLIC_VERSION else "ytsearch"
    info = await fetch_video_info_with_retry(
        f"{prefix}{limit}:{query}",
        ydl_opts_override={
            "extract_flat": True,
            "noplaylist": True,
            "socket_timeout": 10,
        },
    )

    results = []
    for entry in (info.get("entries") or [])[:limit]:
        url = entry.get("webpage_url") or entry.get("url")
        if not url:
            continue
        results.append(
            {
                "title": entry.get("title") or "Unknown title",
                "artist": entry.get("uploader") or entry.get("channel") or "",
                "duration": entry.get("duration") or 0,
                "thumbnail": _thumb(entry.get("thumbnail")),
                "url": url,
                "platform": "SoundCloud" if IS_PUBLIC_VERSION else "YouTube",
            }
        )

    if len(_search_cache) > 256:
        _search_cache.clear()
    _search_cache[cache_key] = (time.time(), results)
    return results


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


async def action_play(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    query = str(data.get("query") or "").strip()
    if not query:
        raise ActionError("Type a song name or paste a link.")
    if len(query) > MAX_QUERY_LENGTH:
        raise ActionError("That request is too long.")

    position = str(data.get("position") or "end").lower()
    front = position in {"next", "playnext"}

    player = _player(session.guild_id)
    vc = _voice_client(session, player)

    items, notices = await _resolve_query(ctx, session, query)
    if not items:
        raise ActionError("No track could be found for that request.")

    await _insert_items(player, items, front=front)
    await _start_playback_if_idle(ctx, session, player, vc)
    _refresh_controller(ctx, session.guild_id)

    first = track_payload(items[0]) or {}
    title = first.get("title") or "track"
    if len(items) == 1:
        message = f"Added “{title}”" + (" up next" if front else " to the queue")
    else:
        message = f"Added {len(items)} tracks to the queue"
    return {"ok": True, "message": message, "added": len(items), "notices": notices[:4]}


async def action_pause(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    player = _player(session.guild_id)
    vc = _voice_client(session, player)
    if not vc.is_playing():
        raise ActionError("Nothing is playing right now.", status=409)
    if player.playback_started_at:
        elapsed = time.time() - player.playback_started_at
        player.start_time += elapsed * player.playback_speed
        player.playback_started_at = None
    vc.pause()
    _refresh_controller(ctx, session.guild_id)
    return {"ok": True, "message": "Paused"}


async def action_resume(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    player = _player(session.guild_id)
    vc = _voice_client(session, player)
    if not vc.is_paused():
        raise ActionError("Playback is not paused.", status=409)
    if player.playback_started_at is None:
        player.playback_started_at = time.time()
    vc.resume()
    _refresh_controller(ctx, session.guild_id)
    return {"ok": True, "message": "Resumed"}


async def action_toggle(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    player = _player(session.guild_id)
    vc = _voice_client(session, player)
    if vc.is_paused():
        return await action_resume(ctx, session, data)
    return await action_pause(ctx, session, data)


async def action_skip(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    from ..services.voice import safe_stop

    player = _player(session.guild_id)
    vc = _voice_client(session, player)
    _require_playback(vc)

    if player.lyrics_task and not player.lyrics_task.done():
        player.lyrics_task.cancel()

    player.manual_stop = True
    await safe_stop(vc)
    _refresh_controller(ctx, session.guild_id)
    return {"ok": True, "message": "Skipped"}


async def action_previous(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    from ..services.voice import safe_stop

    player = _player(session.guild_id)
    vc = _voice_client(session, player)
    _require_playback(vc)

    if len(player.history) < 2:
        raise ActionError("There is no previous track in the history.", status=409)

    current = player.history.pop()
    previous = player.history.pop()

    new_queue: asyncio.Queue = asyncio.Queue()
    await new_queue.put(previous)
    await new_queue.put(current)
    for item in list(player.queue._queue):
        await new_queue.put(item)
    player.queue = new_queue

    player.manual_stop = True
    await safe_stop(vc)
    _refresh_controller(ctx, session.guild_id)
    return {"ok": True, "message": "Playing the previous track"}


async def action_stop(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    from ..helpers.common import safe_stop as _safe_stop
    from ..services.voice import clear_audio_cache

    state = _state(session.guild_id)
    player = state.music_player
    vc = _voice_client(session, player)

    if player.lyrics_task and not player.lyrics_task.done():
        player.lyrics_task.cancel()

    await _safe_stop(vc)
    if player.current_task and not player.current_task.done():
        player.current_task.cancel()
    await vc.disconnect()

    state._24_7_mode = False
    clear_audio_cache(session.guild_id)
    state.reset_player()
    _refresh_controller(ctx, session.guild_id)
    return {"ok": True, "message": "Playback stopped and the bot left the channel"}


async def action_seek(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    from ..services.voice import safe_stop

    player = _player(session.guild_id)
    vc = _voice_client(session, player)
    _require_playback(vc)

    if getattr(player, "is_current_live", False):
        raise ActionError("You cannot seek inside a live stream.", status=409)

    try:
        target = float(data.get("position"))
    except (TypeError, ValueError):
        raise ActionError("Invalid seek position.") from None

    duration = float((player.current_info or {}).get("duration") or 0)
    target = max(0.0, target)
    if duration:
        target = min(target, max(0.0, duration - 1))

    was_paused = vc.is_paused()
    player.start_time = target
    player.playback_started_at = time.time()
    player.seek_info = target
    player.is_seeking = True
    await safe_stop(vc)

    if was_paused:
        for _ in range(60):
            await asyncio.sleep(0.05)
            if _is_connected(vc) and vc.is_playing():
                vc.pause()
                player.playback_started_at = None
                break

    _refresh_controller(ctx, session.guild_id)
    return {"ok": True, "message": "Playback position updated"}


async def action_volume(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    player = _player(session.guild_id)
    vc = getattr(player, "voice_client", None)
    try:
        level = int(round(float(data.get("volume"))))
    except (TypeError, ValueError):
        raise ActionError("Invalid volume.") from None
    level = max(0, min(level, 200))

    player.volume = level / 100.0
    source = getattr(vc, "source", None)
    volume_attr = getattr(source, "volume", None)
    if volume_attr is not None:
        try:
            source.volume = player.volume
        except Exception:  # pragma: no cover - defensive
            pass

    _refresh_controller(ctx, session.guild_id)
    return {"ok": True, "message": f"Volume set to {level}%"}


async def action_loop(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    player = _player(session.guild_id)
    if "enabled" in data:
        player.loop_current = bool(data["enabled"])
    else:
        player.loop_current = not player.loop_current
    _refresh_controller(ctx, session.guild_id)
    return {
        "ok": True,
        "message": "Loop enabled" if player.loop_current else "Loop disabled",
    }


async def action_autoplay(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    player = _player(session.guild_id)
    if "enabled" in data:
        player.autoplay_enabled = bool(data["enabled"])
    else:
        player.autoplay_enabled = not player.autoplay_enabled
    _refresh_controller(ctx, session.guild_id)
    return {
        "ok": True,
        "message": "Autoplay enabled" if player.autoplay_enabled else "Autoplay disabled",
    }


async def action_shuffle(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    import random

    player = _player(session.guild_id)
    if player.queue.empty():
        raise ActionError("The queue is empty.", status=409)

    items = list(player.queue._queue)
    random.shuffle(items)
    new_queue: asyncio.Queue = asyncio.Queue()
    for item in items:
        await new_queue.put(item)
    player.queue = new_queue
    _refresh_controller(ctx, session.guild_id)
    return {"ok": True, "message": "Queue shuffled"}


async def action_filter(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    from ..core import AUDIO_FILTERS
    from ..helpers.common import get_speed_multiplier_from_filters
    from ..services.voice import safe_stop

    state = _state(session.guild_id)
    player = state.music_player

    name = str(data.get("filter") or "").lower()
    if name in {"none", ""}:
        new_filters: set[str] = set()
    elif name not in AUDIO_FILTERS:
        raise ActionError(f"Unknown filter '{name}'.")
    else:
        new_filters = set(state.server_filters)
        enabled = data.get("enabled")
        if enabled is None:
            enabled = name not in new_filters
        if enabled:
            new_filters.add(name)
        else:
            new_filters.discard(name)

    state.server_filters = new_filters

    vc = getattr(player, "voice_client", None)
    if vc is not None and _is_connected(vc) and (vc.is_playing() or vc.is_paused()):
        old_speed = getattr(player, "playback_speed", 1.0) or 1.0
        elapsed = 0.0
        if player.playback_started_at:
            elapsed = (
                time.time() - player.playback_started_at
            ) * old_speed + player.start_time
        player.playback_speed = get_speed_multiplier_from_filters(new_filters)
        player.is_seeking = True
        player.seek_info = elapsed
        await safe_stop(vc)

    _refresh_controller(ctx, session.guild_id)
    label = ", ".join(sorted(new_filters)) or "none"
    return {"ok": True, "message": f"Filters: {label}"}


async def action_mode_247(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    state = _state(session.guild_id)
    player = state.music_player

    if "enabled" in data:
        enabled = bool(data["enabled"])
    else:
        enabled = not state._24_7_mode
    mode = "auto" if str(data.get("mode") or "").lower() == "auto" else "normal"

    if not enabled:
        state._24_7_mode = False
        player.autoplay_enabled = False
        player.loop_current = False
        player.radio_playlist.clear()
        if player.silence_task and not player.silence_task.done():
            player.silence_task.cancel()
        _persist_state()
        _refresh_controller(ctx, session.guild_id)
        return {"ok": True, "message": "24/7 mode disabled"}

    vc = _voice_client(session, player)
    if not player.radio_playlist:
        if player.current_info:
            player.radio_playlist.append(
                {
                    "url": player.current_url,
                    "title": player.current_info.get("title", "Unknown title"),
                    "webpage_url": player.current_info.get("webpage_url"),
                    "is_single": False,
                    "source_type": player.current_info.get("source_type"),
                }
            )
        player.radio_playlist.extend(list(player.queue._queue))

    state._24_7_mode = True
    player.loop_current = False
    player.autoplay_enabled = mode == "auto"
    _persist_state()

    if not vc.is_playing() and not vc.is_paused() and player.radio_playlist:
        await _start_playback_if_idle(ctx, session, player, vc)

    _refresh_controller(ctx, session.guild_id)
    message = (
        "24/7 auto-radio enabled"
        if mode == "auto"
        else "24/7 mode enabled (queue on repeat)"
    )
    return {"ok": True, "message": message}


def _require_manager(session: Any) -> None:
    if not getattr(session, "is_manager", False):
        raise ActionError("Only server managers can change this setting.", status=403)


async def action_default_volume(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    _require_manager(session)
    try:
        level = int(round(float(data.get("volume"))))
    except (TypeError, ValueError):
        raise ActionError("Invalid volume.") from None
    level = max(0, min(level, 200))

    state = _state(session.guild_id)
    player = state.music_player
    state.default_volume = level / 100.0
    player.volume = state.default_volume
    source = getattr(getattr(player, "voice_client", None), "source", None)
    if getattr(source, "volume", None) is not None:
        try:
            source.volume = state.default_volume
        except Exception:  # pragma: no cover - defensive
            pass

    _persist_state()
    _refresh_controller(ctx, session.guild_id)
    return {"ok": True, "message": f"Default volume set to {level}%"}


async def action_kawaii(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    _require_manager(session)

    from ..core import Locale

    state = _state(session.guild_id)
    if "enabled" in data:
        enabled = bool(data["enabled"])
    else:
        enabled = state.locale != Locale.EN_X_KAWAII
    state.locale = Locale.EN_X_KAWAII if enabled else Locale.EN_US
    _persist_state()
    _refresh_controller(ctx, session.guild_id)
    return {"ok": True, "message": "Kawaii mode enabled ( ˘ ³˘)♥" if enabled else "Kawaii mode disabled"}


async def action_remove(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    player = _player(session.guild_id)
    try:
        index = int(data.get("index"))
    except (TypeError, ValueError):
        raise ActionError("Invalid queue index.") from None

    items = list(player.queue._queue)
    if not 0 <= index < len(items):
        raise ActionError("That track is no longer in the queue.", status=404)

    removed = items.pop(index)
    new_queue: asyncio.Queue = asyncio.Queue()
    for item in items:
        await new_queue.put(item)
    player.queue = new_queue
    _refresh_controller(ctx, session.guild_id)

    title = (track_payload(removed) or {}).get("title", "track")
    return {"ok": True, "message": f"Removed “{title}”"}


async def action_clear_queue(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    player = _player(session.guild_id)
    while not player.queue.empty():
        player.queue.get_nowait()
    player.radio_playlist.clear()
    _refresh_controller(ctx, session.guild_id)
    return {"ok": True, "message": "Queue cleared"}


async def action_jump(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    from ..services.voice import safe_stop

    player = _player(session.guild_id)
    vc = _voice_client(session, player)

    try:
        index = int(data.get("index"))
    except (TypeError, ValueError):
        raise ActionError("Invalid queue index.") from None

    items = list(player.queue._queue)
    if not 0 <= index < len(items):
        raise ActionError("That track is no longer in the queue.", status=404)

    player.history.extend(items[:index])
    new_queue: asyncio.Queue = asyncio.Queue()
    for item in items[index:]:
        await new_queue.put(item)
    player.queue = new_queue

    player.manual_stop = True
    if vc.is_playing() or vc.is_paused():
        await safe_stop(vc)
    else:
        await _start_playback_if_idle(ctx, session, player, vc)

    _refresh_controller(ctx, session.guild_id)
    title = (track_payload(items[index]) or {}).get("title", "track")
    return {"ok": True, "message": f"Jumping to “{title}”"}


async def action_reconnect(ctx: Any, session: Any, data: dict[str, Any]) -> dict[str, Any]:
    from ..services.playback import play_audio
    from ..services.voice import safe_stop

    player = _player(session.guild_id)
    vc = _voice_client(session, player)
    channel = getattr(vc, "channel", None)
    if channel is None:
        raise ActionError("The voice channel could not be resolved.", status=409)

    timestamp = playback_position(player) if player.current_info else 0.0
    player.is_reconnecting = True
    try:
        if vc.is_playing():
            await safe_stop(vc)
        await vc.disconnect(force=True)
        await asyncio.sleep(0.75)
        new_vc = await channel.connect()
        player.voice_client = new_vc
        if player.current_info:
            player.current_task = asyncio.create_task(
                play_audio(session.guild_id, seek_time=timestamp, is_a_loop=True)
            )
    except ActionError:
        raise
    except Exception as exc:
        logger.error("Web UI reconnect failed: %s", exc)
        raise ActionError("Reconnecting to the voice channel failed.", status=500) from exc
    finally:
        player.is_reconnecting = False

    _refresh_controller(ctx, session.guild_id)
    return {"ok": True, "message": "Voice connection refreshed"}


_HANDLERS: dict[str, Callable[..., Any]] = {
    "play": action_play,
    "pause": action_pause,
    "resume": action_resume,
    "toggle": action_toggle,
    "skip": action_skip,
    "previous": action_previous,
    "stop": action_stop,
    "seek": action_seek,
    "volume": action_volume,
    "loop": action_loop,
    "autoplay": action_autoplay,
    "shuffle": action_shuffle,
    "filter": action_filter,
    "mode24_7": action_mode_247,
    "remove": action_remove,
    "clear_queue": action_clear_queue,
    "jump": action_jump,
    "reconnect": action_reconnect,
    "default_volume": action_default_volume,
    "kawaii": action_kawaii,
}

ACTIONS = tuple(_HANDLERS)


async def dispatch(ctx: Any, action: str, data: dict[str, Any], session: Any) -> dict[str, Any]:
    handler = _HANDLERS.get(str(action or "").strip().lower())
    if handler is None:
        raise ActionError(f"Unknown action '{action}'.", status=404)
    result = await handler(ctx, session, data or {})
    if not isinstance(result, dict):
        result = {"ok": True}
    result.setdefault("ok", True)
    return result
