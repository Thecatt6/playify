"""Server-side proxy for cover art.

The panel could load thumbnails straight from the platform CDNs, but that hands
YouTube/Spotify the IP address and the listening habits of every person in the
channel, and it breaks entirely on networks where those CDNs are blocked. The
bot fetches the image once, keeps it in memory for an hour and serves it from
its own origin.

This is deliberately not an open proxy: only https, only the hosts the music
platforms actually use, only images, and a hard size cap.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import aiohttp

ALLOWED_HOSTS = {
    # YouTube / YouTube Music
    "i.ytimg.com",
    "i1.ytimg.com",
    "i2.ytimg.com",
    "i3.ytimg.com",
    "i4.ytimg.com",
    "i9.ytimg.com",
    "img.youtube.com",
    "yt3.ggpht.com",
    "yt3.googleusercontent.com",
    # Spotify
    "i.scdn.co",
    "mosaic.scdn.co",
    "image-cdn-ak.spotifycdn.com",
    "image-cdn-fa.spotifycdn.com",
    # Deezer
    "e-cdns-images.dzcdn.net",
    "cdn-images.dzcdn.net",
    # SoundCloud / Bandcamp
    "i1.sndcdn.com",
    "f4.bcbits.com",
}

MAX_BYTES = 3 * 1024 * 1024
CACHE_TTL = 3600.0
CACHE_MAX = 128
FETCH_TIMEOUT = 10.0


def allowed_url(raw: str) -> str | None:
    """Return the URL if it may be proxied, otherwise ``None``."""
    if not raw or len(raw) > 2048:
        return None
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if parsed.scheme != "https":
        return None
    if parsed.username or parsed.password:
        return None  # https://i.ytimg.com@evil.example/ tricks
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        return None
    if parsed.port not in (None, 443):
        return None
    if not parsed.path or parsed.path == "/":
        return None
    return parsed.geturl()


class ThumbnailCache:
    """Bounded in-memory cache of downloaded artwork."""

    def __init__(self, ttl: float = CACHE_TTL, max_entries: int = CACHE_MAX, clock=time.time) -> None:
        self.ttl = float(ttl)
        self.max_entries = max(1, int(max_entries))
        self._items: dict[str, tuple[float, bytes, str]] = {}
        self._clock = clock

    def get(self, url: str) -> tuple[bytes, str] | None:
        entry = self._items.get(url)
        if entry is None:
            return None
        expires_at, body, content_type = entry
        if expires_at < self._clock():
            self._items.pop(url, None)
            return None
        return body, content_type

    def put(self, url: str, body: bytes, content_type: str) -> None:
        if len(self._items) >= self.max_entries:
            oldest = min(self._items, key=lambda key: self._items[key][0])
            self._items.pop(oldest, None)
        self._items[url] = (self._clock() + self.ttl, body, content_type)


CACHE = ThumbnailCache()


async def fetch(url: str, *, session: Any = None) -> tuple[bytes, str] | None:
    """Download ``url`` (already validated) and return ``(bytes, content type)``."""
    cached = CACHE.get(url)
    if cached is not None:
        return cached

    own_session = session is None
    try:
        if own_session:
            session = aiohttp.ClientSession()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT)) as resp:
            if resp.status != 200:
                return None
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not content_type.startswith("image/"):
                return None
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    return None
                chunks.append(chunk)
            body = b"".join(chunks)
    except Exception:
        return None
    finally:
        if own_session and session is not None:
            await session.close()

    if not body:
        return None
    CACHE.put(url, body, content_type)
    return body, content_type
