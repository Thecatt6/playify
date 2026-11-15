"""Fetch and cache lyrics for the track that is playing.

LRCLIB is free and needs no key, but it is somebody else's server: one lookup
per track is reasonable, one lookup per player tick is not. This module keeps a
small TTL cache keyed by "title artist", returns the parsed lines for the
synced view and the plain text for the fallback view.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..services import lyrics_lrc

CACHE_TTL = 60 * 60.0  # an hour is plenty for a listening session
CACHE_MAX = 64


class LyricsCache:
    """Bounded TTL cache of lyrics lookups."""

    def __init__(self, ttl: float = CACHE_TTL, max_entries: int = CACHE_MAX, clock=time.time) -> None:
        self.ttl = float(ttl)
        self.max_entries = max(1, int(max_entries))
        self._items: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        self._clock = clock

    def _key(self, title: str, artist: str) -> str:
        # Collapse whitespace: "midnight  city" and "Midnight City" are the same
        # song, and a track title can pick up double spaces on the way in.
        clean = lambda value: " ".join(str(value or "").split()).lower()
        return f"{clean(title)}|{clean(artist)}"

    def get(self, title: str, artist: str) -> dict[str, Any] | None:
        key = self._key(title, artist)
        entry = self._items.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if expires_at < self._clock():
            self._items.pop(key, None)
            return None
        return payload

    def put(self, title: str, artist: str, payload: dict[str, Any]) -> None:
        key = self._key(title, artist)
        if len(self._items) >= self.max_entries:
            # Drop the entry closest to expiry; good enough for a cache this size.
            oldest = min(self._items, key=lambda item: self._items[item][0])
            self._items.pop(oldest, None)
        self._items[key] = (self._clock() + self.ttl, payload)

    def clear(self) -> None:
        self._items.clear()

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock


CACHE = LyricsCache()


def _payload(raw: str, *, cached: bool) -> dict[str, Any]:
    if lyrics_lrc.is_lrc(raw):
        lines = lyrics_lrc.parse_lrc(raw)
        plain = lyrics_lrc.strip_lrc(raw) if len(lines) < 2 else "\n".join(
            line["text"] for line in lines
        )
        return {"synced": True, "lines": lines, "plain": plain, "cached": cached}
    return {"synced": False, "lines": [], "plain": raw.strip(), "cached": cached}


async def _default_fetcher(query: str, needs_synced: bool = False) -> str | None:
    """Ask LRCLIB (imported lazily: the bot modules pull in discord.py)."""
    from ..services.lyrics import fetch_lrclib

    return await fetch_lrclib(query, needs_synced=needs_synced)


async def lookup(title: str, artist: str, fetch=None) -> dict[str, Any]:
    """Return lyrics for one track, from the cache when possible.

    Never raises: a lyrics service being down must not break the panel.
    """
    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title:
        return {"synced": False, "lines": [], "plain": "", "cached": False, "found": False}

    cached = CACHE.get(title, artist)
    if cached is not None:
        return {**cached, "cached": True}

    query = f"{title} {artist}".strip()
    fetcher = fetch or _default_fetcher
    try:
        async with CACHE.lock:
            raw = await fetcher(query, needs_synced=True)
            if not raw:
                raw = await fetcher(query, needs_synced=False)
    except Exception:
        return {"synced": False, "lines": [], "plain": "", "cached": False, "found": False}

    if not raw:
        # Cache the miss too: otherwise a track without lyrics hits LRCLIB on
        # every single request.
        payload = {"synced": False, "lines": [], "plain": "", "cached": False, "found": False}
        CACHE.put(title, artist, payload)
        return payload

    payload = {**_payload(raw, cached=False), "found": True}
    CACHE.put(title, artist, payload)
    return payload
