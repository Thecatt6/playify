"""Standalone demo of the Playify Web UI.

Runs the real web layer (same routes, same front-end, same access checks)
against an in-memory fake bot, so you can explore or design the interface
without a Discord token, yt-dlp or ffmpeg.

    python tools/webui_demo.py            # http://localhost:8080

Everything is simulated: a fake queue advances on its own, search returns a
canned catalogue and "playback" is just a timer. This tool is for development
and screenshots only — it must never be exposed publicly, since it logs you in
automatically as a fake listener.
"""

from __future__ import annotations

import asyncio
import enum
import os
import sys
import time
import types
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HOST = os.getenv("WEBUI_DEMO_HOST", "0.0.0.0")
PORT = int(os.getenv("WEBUI_DEMO_PORT", "8080"))
GUILD_ID = 1

class DemoLocale(enum.Enum):
    EN_US = "en-US"
    EN_X_KAWAII = "en-x-kawaii"


CATALOG = [
    ("Midnight City", "M83", 244),
    ("Nightcall", "Kavinsky", 258),
    ("Resonance", "HOME", 212),
    ("Weightless", "Marconi Union", 488),
    ("Says", "Nils Frahm", 508),
    ("Odessa", "Caribou", 255),
    ("La Femme d'Argent", "Air", 427),
    ("Sunset Lover", "Petit Biscuit", 238),
    ("Innerbloom", "RÜFÜS DU SOL", 578),
    ("Tadow", "Masego & FKJ", 301),
    ("Anemone", "The Brian Jonestown Massacre", 322),
    ("Weird Fishes", "Radiohead", 328),
]

PALETTES = [
    ("#7c6cf7", "#5eead4"),
    ("#f472b6", "#7c6cf7"),
    ("#38bdf8", "#6366f1"),
    ("#fbbf24", "#f472b6"),
    ("#34d399", "#0ea5e9"),
    ("#a78bfa", "#f472b6"),
    ("#22d3ee", "#a3e635"),
    ("#f97316", "#ef4444"),
]


def cover(index: int, label: str) -> str:
    start, end = PALETTES[index % len(PALETTES)]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{start}"/><stop offset="1" stop-color="{end}"/>'
        "</linearGradient></defs>"
        '<rect width="300" height="300" fill="url(#g)"/>'
        '<circle cx="150" cy="146" r="58" fill="rgba(12,12,18,0.35)"/>'
        '<circle cx="150" cy="146" r="17" fill="rgba(12,12,18,0.6)"/>'
        '<circle cx="150" cy="146" r="6" fill="rgba(255,255,255,0.65)"/>'
        '<g fill="rgba(255,255,255,0.5)">'
        '<rect x="118" y="232" width="64" height="5" rx="2.5"/>'
        '<rect x="132" y="242" width="36" height="4" rx="2"/>'
        "</g></svg>"
    )
    return "data:image/svg+xml;charset=utf-8," + quote(svg)


def track(index: int, title: str, artist: str, duration: int) -> dict:
    return {
        "url": f"demo://track/{index}",
        "webpage_url": f"demo://track/{index}",
        "title": title,
        "artist": artist,
        "uploader": artist,
        "duration": duration,
        "thumbnail": cover(index, title),
        "is_single": True,
        "source_type": "demo",
        "original_platform": "Demo",
    }


DEMO_TRACKS = [track(i, *entry) for i, entry in enumerate(CATALOG)]


# ---------------------------------------------------------------------------
# Fake Discord / Playify objects
# ---------------------------------------------------------------------------


class DemoAsset:
    def __init__(self, url: str) -> None:
        self.url = url


class DemoMember:
    def __init__(self, member_id: int, name: str, bot: bool = False, avatar: str | None = None) -> None:
        self.id = member_id
        self.name = name
        self.display_name = name
        self.global_name = name
        self.bot = bot
        self.avatar = None
        self.display_avatar = DemoAsset(avatar) if avatar else None
        self.voice = None
        self.guild_permissions = types.SimpleNamespace(manage_guild=False)


class DemoGuild:
    def __init__(self, guild_id: int, name: str) -> None:
        self.id = guild_id
        self.name = name
        self.icon = DemoAsset(cover(2, ""))
        self._members: dict[int, DemoMember] = {}

    def get_member(self, member_id: int):
        return self._members.get(member_id)


class DemoChannel:
    def __init__(self, channel_id: int, name: str, guild: DemoGuild, members: list[DemoMember]) -> None:
        self.id = channel_id
        self.name = name
        self.guild = guild
        self.members = members


class DemoVoiceClient:
    def __init__(self, channel: DemoChannel, player: "DemoPlayer") -> None:
        self.channel = channel
        self.player = player
        self.connected = True
        self._playing = False
        self._paused = False
        self.source = None

    def is_connected(self) -> bool:
        return self.connected

    def is_playing(self) -> bool:
        return self._playing and not self._paused

    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    async def disconnect(self, force: bool = False) -> None:
        self.connected = False
        self._playing = False
        self._paused = False

    async def connect(self) -> "DemoVoiceClient":
        self.connected = True
        return self


class DemoPlayer:
    def __init__(self, voice_client: DemoVoiceClient | None = None) -> None:
        self.voice_client = voice_client
        self.queue: asyncio.Queue = asyncio.Queue()
        self.history: list = []
        self.radio_playlist: list = []
        self.current_info = None
        self.current_url = None
        self.loop_current = False
        self.autoplay_enabled = False
        self.start_time = 0.0
        self.playback_started_at = None
        self.playback_speed = 1.0
        self.volume = 0.7
        self.is_current_live = False
        self.is_playing_silence = False
        self.is_paused_by_leave = False
        self.silence_task = None
        self.current_task = None
        self.lyrics_task = None
        self.manual_stop = False
        self.seek_info = None
        self.is_seeking = False
        self.is_reconnecting = False
        self.text_channel = None
        self.queue_lock = asyncio.Lock()


class DemoState:
    def __init__(self, player: DemoPlayer) -> None:
        self.music_player = player
        self._24_7_mode = False
        self.server_filters: set[str] = set()
        self.default_volume = 0.7
        self.locale = DemoLocale.EN_US

    def reset_player(self) -> DemoPlayer:
        self.music_player = DemoPlayer(self.music_player.voice_client)
        self.music_player.volume = self.default_volume
        return self.music_player


class DemoBot:
    def __init__(self, user: DemoMember, voice_client: DemoVoiceClient, guild: DemoGuild) -> None:
        self.user = user
        self.voice_clients = [voice_client]
        self.guilds = [guild]


# ---------------------------------------------------------------------------
# Build the world
# ---------------------------------------------------------------------------

guild = DemoGuild(GUILD_ID, "After Hours")
bot_user = DemoMember(999, "Playify", bot=True)
listener = DemoMember(1, "Alessandro", avatar=cover(0, ""))
giulia = DemoMember(2, "Giulia", avatar=cover(1, ""))
marco = DemoMember(3, "Marco", avatar=cover(3, ""))
sofia = DemoMember(4, "Sofia", avatar=cover(4, ""))
for member in (bot_user, listener, giulia, marco, sofia):
    guild._members[member.id] = member

channel = DemoChannel(7, "wine & vinyl", guild, [listener, giulia, marco, sofia, bot_user])
player = DemoPlayer()
voice_client = DemoVoiceClient(channel, player)
player.voice_client = voice_client
state = DemoState(player)
bot = DemoBot(bot_user, voice_client, guild)
listener.voice = types.SimpleNamespace(channel=channel)
listener.guild_permissions = types.SimpleNamespace(manage_guild=True)  # shows the server settings
giulia.voice = types.SimpleNamespace(channel=channel)


def _start_track(info: dict) -> None:
    player.current_info = info
    player.current_url = info.get("webpage_url") or info.get("url")
    player.start_time = 0.0
    player.playback_started_at = time.time()
    voice_client._playing = True
    voice_client._paused = False
    player.is_current_live = False


async def _advance(force: bool = False) -> None:
    """Move to the next queue item (simulation of the real after_playing chain)."""
    if not force and player.loop_current and player.current_info:
        _start_track(dict(player.current_info))
        return
    if player.current_info:
        player.history.append(dict(player.current_info))
    if player.queue.empty():
        if state._24_7_mode:
            for item in DEMO_TRACKS:
                await player.queue.put(dict(item))
            player.current_info = None
        else:
            player.current_info = None
            player.current_url = None
            player.start_time = 0.0
            player.playback_started_at = None
            voice_client._playing = False
            voice_client._paused = False
            return
    if player.queue.empty():
        return
    _start_track(dict(player.queue.get_nowait()))


async def _engine() -> None:
    while True:
        await asyncio.sleep(0.4)
        if not voice_client.connected:
            continue
        duration = float((player.current_info or {}).get("duration") or 0)
        if (
            voice_client.is_playing()
            and duration
            and player.playback_started_at
            and time.time() - player.playback_started_at >= duration
        ):
            await _advance()
        elif (
            voice_client.connected
            and not player.current_info
            and not player.queue.empty()
            and not voice_client._paused
        ):
            await _advance()


# ---------------------------------------------------------------------------
# Stubs for the bot runtime (imported lazily by the web layer)
# ---------------------------------------------------------------------------


def _module(name: str, **attributes) -> None:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module


async def _noop_async(*_args, **_kwargs) -> None:
    return None


async def _none_async(*_args, **_kwargs):
    return None


async def demo_play_audio(guild_id: int, seek_time: float = 0, is_a_loop: bool = False, **kwargs) -> None:
    if player.current_info is None:
        await _advance()
    elif seek_time:
        player.start_time = float(seek_time)
        player.playback_started_at = time.time()
        voice_client._playing = True
        voice_client._paused = False
    else:
        voice_client._playing = True


async def demo_safe_stop(vc) -> None:
    if player.seek_info is not None:
        player.start_time = float(player.seek_info)
        player.seek_info = None
        player.is_seeking = False
        player.playback_started_at = time.time()
        voice_client._playing = True
        voice_client._paused = False
        return

    was_manual = player.manual_stop
    voice_client._playing = False
    voice_client._paused = False

    async def _after() -> None:
        await asyncio.sleep(0.05)
        if was_manual:
            player.manual_stop = False
            await _advance(force=True)
        elif player.loop_current:
            await _advance(force=True)
        elif voice_client.connected and not voice_client._paused:
            await _advance()

    asyncio.get_running_loop().create_task(_after())


class DemoLazySearchItem:
    def __init__(self, query_dict, requester=None, original_platform="Demo") -> None:
        self.query_dict = query_dict or {}
        self.requester = requester
        self.original_platform = original_platform
        self.resolved_info = None
        self.title = self.query_dict.get("name", "Unknown")
        self.artist = self.query_dict.get("artist", "")
        self.duration = 0
        self.thumbnail = None
        self.url = "#"
        self.webpage_url = "#"


def demo_search(query: str):
    """Fake yt-dlp entry for the search / play handlers."""
    lowered = query.lower()
    matches = [item for item in DEMO_TRACKS if lowered in item["title"].lower() or lowered in item["artist"].lower()]
    if not matches:
        matches = DEMO_TRACKS[:6]
    if "ytsearch" in lowered or "scsearch" in lowered:
        matches = DEMO_TRACKS[:6]
    return {"entries": matches}


async def demo_fetch_info(query: str, ydl_opts_override=None):
    await asyncio.sleep(0.15)
    if "demo://track/" in query:
        try:
            index = int(query.split("demo://track/")[1].split()[0])
            return {"entries": [DEMO_TRACKS[index % len(DEMO_TRACKS)]]}
        except (IndexError, ValueError):
            pass
    if query.startswith(("http://", "https://")):
        return DEMO_TRACKS[0]
    term = query
    for prefix in ("ytsearch", "scsearch"):
        if term.startswith(prefix):
            term = term.split(":", 1)[1] if ":" in term else term
            break
    lowered = term.strip().lower()
    matches = [
        item
        for item in DEMO_TRACKS
        if lowered and (lowered in item["title"].lower() or lowered in item["artist"].lower())
    ]
    return {"entries": matches or DEMO_TRACKS[:6]}


def _install_stubs() -> None:
    import json as _json
    import re as _re
    from urllib.parse import parse_qs as _parse_qs, urlparse as _urlparse

    _module(
        "src.playify.core",
        re=_re,
        os=os,
        sys=sys,
        json=_json,
        time=time,
        urlparse=_urlparse,
        parse_qs=_parse_qs,
        yt_dlp=types.SimpleNamespace(YoutubeDL=object, utils=types.SimpleNamespace(DownloadError=Exception)),
        Locale=DemoLocale,
        get_guild_state=lambda guild_id: state,
        get_player=lambda guild_id: state.music_player,
        save_all_states=_noop_async,
        IS_PUBLIC_VERSION=False,
        AUDIO_FILTERS={
            "slowed": "asetrate=44100*0.8",
            "spedup": "asetrate=44100*1.2",
            "nightcore": "asetrate=44100*1.25,atempo=1.0",
            "reverb": "aecho=0.8:0.9:40|50|60:0.4|0.3|0.2",
            "8d": "apulsator=hz=0.08",
            "muffled": "lowpass=f=500",
            "bassboost": "bass=g=10",
            "earrape": "acrusher=level_in=8:level_out=18:bits=8:mode=log:aa=1",
        },
        logger=__import__("logging").getLogger("playify.demo"),
    )
    _module("src.playify.services.playback", play_audio=demo_play_audio)
    _module(
        "src.playify.services.voice",
        safe_stop=demo_safe_stop,
        clear_audio_cache=lambda guild_id: None,
        fetch_video_info_with_retry=demo_fetch_info,
        play_silence_loop=_noop_async,
    )
    _module(
        "src.playify.helpers.common",
        safe_stop=demo_safe_stop,
        get_speed_multiplier_from_filters=lambda active: (
            1.25 if "nightcore" in active else 0.8 if "slowed" in active else 1.2 if "spedup" in active else 1.0
        ),
        get_track_display_info=lambda track, guild_id=None: {"title": (track or {}).get("title", "Unknown")},
        format_duration=lambda seconds: f"{int(seconds or 0) // 60}:{int(seconds or 0) % 60:02d}",
    )
    _module("src.playify.models.lazy_search", LazySearchItem=DemoLazySearchItem)
    _module(
        "src.playify.services.platforms",
        process_spotify_url=_none_async,
        process_deezer_url=_none_async,
        process_apple_music_url=_none_async,
        process_tidal_url=_none_async,
        process_amazon_music_url=_none_async,
    )
    _module("src.playify.ui.controller", update_controller=_noop_async)



def _seed_queue() -> None:
    for info in DEMO_TRACKS[:7]:
        player.queue.put_nowait(dict(info))
    _start_track(dict(player.queue.get_nowait()))


async def main() -> None:
    _install_stubs()
    _seed_queue()

    from aiohttp import web

    from src.playify.webui.config import WebUIConfig
    from src.playify.webui.context import WebUIContext
    from src.playify.webui.oauth import DiscordOAuth
    from src.playify.webui.security import TokenSigner
    from src.playify.webui.server import create_app

    config = WebUIConfig(
        enabled=True,
        host=HOST,
        port=PORT,
        base_url="",  # derived from the request
        client_id="",
        client_secret="",
        session_secret="demo-session-secret",
        dev_bypass_user_id=listener.id,
        allow_any_origin=True,
    )
    context = WebUIContext(
        bot=bot,
        config=config,
        signer=TokenSigner(config.session_secret),
        oauth=DiscordOAuth("", ""),
    )

    runner = web.AppRunner(create_app(context), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()

    print("=" * 66)
    print("  Playify Web UI — DEMO MODE (fake bot, auto-login, no Discord)")
    print(f"  http://localhost:{PORT}")
    print("=" * 66)

    tasks = [asyncio.create_task(_engine())]
    try:
        await asyncio.Event().wait()
    finally:
        for task in tasks:
            task.cancel()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDemo stopped.")
