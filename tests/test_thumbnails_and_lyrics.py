"""Tests for the artwork proxy and the lyrics cache."""

from __future__ import annotations

import pytest

from src.playify.webui import lyrics_service, thumbnails


# ---------------------------------------------------------------------------
# Artwork proxy allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://i.ytimg.com/vi/abc/hqdefault.jpg",
        "https://i.scdn.co/image/ab67616d0000b273",
        "https://e-cdns-images.dzcdn.net/images/cover/123/500x500.jpg",
        "https://i1.sndcdn.com/artworks-000.jpg",
    ],
)
def test_known_artwork_hosts_are_allowed(url):
    assert thumbnails.allowed_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://i.ytimg.com/vi/abc/hqdefault.jpg",  # not https
        "https://example.com/cover.jpg",  # random host
        "https://i.ytimg.com.evil.example/x.jpg",  # suffix trick
        "https://i.ytimg.com@evil.example/x.jpg",  # userinfo trick
        "https://i.ytimg.com:8443/x.jpg",  # odd port
        "https://i.ytimg.com/",  # no path
        "file:///etc/passwd",
        "javascript:alert(1)",
        "",
        "https://i.ytimg.com/" + "x" * 3000,
    ],
)
def test_everything_else_is_refused(url):
    assert thumbnails.allowed_url(url) is None


async def test_fetch_rejects_non_images(monkeypatch):
    class Response:
        status = 200
        headers = {"Content-Type": "text/html"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Session:
        def get(self, url, timeout=None):
            return Response()

    assert await thumbnails.fetch("https://i.scdn.co/x", session=Session()) is None


async def test_fetch_caches_the_bytes(monkeypatch):
    calls = {"n": 0}

    class Content:
        async def iter_chunked(self, size):
            yield b"\x89PNG"

    class Response:
        status = 200
        headers = {"Content-Type": "image/png"}
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Session:
        def get(self, url, timeout=None):
            calls["n"] += 1
            return Response()

    thumbnails.CACHE._items.clear()
    first = await thumbnails.fetch("https://i.scdn.co/cache-me", session=Session())
    second = await thumbnails.fetch("https://i.scdn.co/cache-me", session=Session())
    assert first == (b"\x89PNG", "image/png")
    assert second == first
    assert calls["n"] == 1


async def test_fetch_gives_up_on_huge_files(monkeypatch):
    class Content:
        async def iter_chunked(self, size):
            for _ in range(80):
                yield b"x" * (64 * 1024)

    class Response:
        status = 200
        headers = {"Content-Type": "image/jpeg"}
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Session:
        def get(self, url, timeout=None):
            return Response()

    assert await thumbnails.fetch("https://i.scdn.co/huge", session=Session()) is None


def test_the_cache_expires_entries():
    clock = {"now": 1000.0}
    cache = thumbnails.ThumbnailCache(ttl=10, clock=lambda: clock["now"])
    cache.put("u", b"x", "image/png")
    assert cache.get("u") == (b"x", "image/png")
    clock["now"] += 11
    assert cache.get("u") is None


def test_the_cache_is_bounded():
    cache = thumbnails.ThumbnailCache(ttl=1000, max_entries=2, clock=lambda: 0.0)
    for name in ("a", "b", "c"):
        cache.put(name, b"x", "image/png")
    assert len(cache._items) <= 2
    assert cache.get("c") is not None


# ---------------------------------------------------------------------------
# Lyrics cache
# ---------------------------------------------------------------------------


def test_lyrics_cache_round_trip():
    cache = lyrics_service.LyricsCache(ttl=60, clock=lambda: 100.0)
    assert cache.get("Midnight City", "M83") is None
    cache.put("Midnight City", "M83", {"synced": True, "lines": [], "plain": "x"})
    assert cache.get("midnight  city", " m83 ")["plain"] == "x"


def test_lyrics_cache_expires():
    clock = {"now": 0.0}
    cache = lyrics_service.LyricsCache(ttl=5, clock=lambda: clock["now"])
    cache.put("a", "b", {"plain": "x"})
    clock["now"] = 6
    assert cache.get("a", "b") is None


def test_lyrics_cache_drops_the_oldest_entry():
    cache = lyrics_service.LyricsCache(ttl=100, max_entries=2, clock=lambda: 0.0)
    for name in ("one", "two", "three"):
        cache.put(name, "artist", {"plain": name})
    assert cache.get("three", "artist") is not None
    assert len(cache._items) == 2


async def test_lookup_returns_an_empty_payload_without_a_title():
    payload = await lyrics_service.lookup("", "")
    assert payload["found"] is False and payload["plain"] == ""


async def test_lookup_parses_lrc_and_serves_the_cache(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch(query, needs_synced=False):
        calls["n"] += 1
        if needs_synced:
            return "[00:05.00] hello\n[00:09.00] world"
        return None

    monkeypatch.setattr(lyrics_service, "_default_fetcher", fake_fetch)
    lyrics_service.CACHE.clear()

    first = await lyrics_service.lookup("Song", "Artist")
    assert first["synced"] is True
    assert [line["text"] for line in first["lines"]] == ["hello", "world"]
    assert first["cached"] is False

    second = await lyrics_service.lookup("Song", "Artist")
    assert second["cached"] is True
    assert calls["n"] == 1


async def test_a_lookup_failure_is_not_fatal(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("LRCLIB is down")

    monkeypatch.setattr(lyrics_service, "_default_fetcher", boom)
    lyrics_service.CACHE.clear()
    payload = await lyrics_service.lookup("Unknown", "Nobody")
    assert payload["found"] is False
    assert payload["lines"] == []
