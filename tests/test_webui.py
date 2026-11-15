"""Tests for the optional Playify Web UI.

They run against fake bot objects, so no Discord connection (and no real music
player) is needed. Run with::

    pytest -q
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from src.playify.webui import actions, server
from src.playify.webui.config import WebUIConfig
from src.playify.webui.context import WebUIContext
from src.playify.webui.oauth import DiscordOAuth
from src.playify.webui.security import TokenSigner, build_session_payload
from src.playify.webui.snapshot import build_snapshot

GUILD_ID = 42
USER_ID = 7


# ---------------------------------------------------------------------------
# Fake Discord / Playify objects
# ---------------------------------------------------------------------------


class Member:
    def __init__(self, member_id: int, name: str, bot: bool = False) -> None:
        self.id = member_id
        self.name = name
        self.display_name = name
        self.global_name = name
        self.bot = bot
        self.avatar = None
        self.display_avatar = None
        self.voice = None


class Guild:
    def __init__(self, guild_id: int, name: str) -> None:
        self.id = guild_id
        self.name = name
        self.icon = None
        self.voice_states: dict[int, object] = {}
        self._members: dict[int, Member] = {}

    def add_member(self, member: Member) -> Member:
        self._members[member.id] = member
        return member

    def get_member(self, member_id: int):
        return self._members.get(member_id)


class Channel:
    def __init__(self, channel_id: int, name: str, guild: Guild, members: list[Member]) -> None:
        self.id = channel_id
        self.name = name
        self.guild = guild
        self.members = members


class VoiceClient:
    def __init__(self, channel: Channel) -> None:
        self.channel = channel
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
        self._playing = False
        self._paused = True

    def resume(self) -> None:
        self._playing = True
        self._paused = False


class Player:
    def __init__(self, voice_client: VoiceClient) -> None:
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
        self.volume = 1.0
        self.is_current_live = False
        self.is_playing_silence = False
        self.is_paused_by_leave = False
        self.silence_task = None
        self.current_task = None
        self.lyrics_task = None
        self.manual_stop = False
        self.queue_lock = asyncio.Lock()
        self.text_channel = None

    def reset_player(self):
        self.__init__(self.voice_client)
        return self


class State:
    def __init__(self, player: Player) -> None:
        self.music_player = player
        self._24_7_mode = False
        self.server_filters: set[str] = set()
        self.default_volume = 1.0


class Bot:
    def __init__(self, user: Member, voice_client: VoiceClient, guild: Guild) -> None:
        self.user = user
        self.voice_clients = [voice_client]
        self.guilds = [guild]


class World:
    def __init__(self) -> None:
        self.guild = Guild(GUILD_ID, "Test Server")
        self.listener = self.guild.add_member(Member(USER_ID, "Listener"))
        self.stranger = self.guild.add_member(Member(99, "Stranger"))
        self.bot_user = self.guild.add_member(Member(999, "Playify", bot=True))
        self.channel = Channel(5, "Lounge", self.guild, [self.listener, self.bot_user])
        self.vc = VoiceClient(self.channel)
        self.player = Player(self.vc)
        self.state = State(self.player)
        self.bot = Bot(self.bot_user, self.vc, self.guild)
        self.listener.voice = type("Voice", (), {"channel": self.channel})()


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def config() -> WebUIConfig:
    return WebUIConfig(
        enabled=True,
        host="127.0.0.1",
        port=0,
        base_url="http://localhost:8080",
        client_id="client-id",
        client_secret="client-secret",
        session_secret="unit-test-secret",
    )


@pytest_asyncio.fixture
async def app_bundle(world: World, config: WebUIConfig, monkeypatch):
    context = WebUIContext(
        bot=world.bot,
        config=config,
        signer=TokenSigner(config.session_secret),
        oauth=DiscordOAuth(config.client_id, config.client_secret),
    )
    monkeypatch.setattr(server, "_guild_state", lambda guild_id: world.state)
    monkeypatch.setattr(actions, "_state", lambda guild_id: world.state)
    server._rate_limits.clear()

    client = TestClient(TestServer(server.create_app(context)))
    await client.start_server()
    yield type("Bundle", (), {"client": client, "ctx": context, "world": world})
    await client.close()


def auth_headers(ctx: WebUIContext, user: dict | None = None) -> dict[str, str]:
    payload = build_session_payload(
        user or {"id": str(USER_ID), "username": "listener", "global_name": "Listener"},
        ctx.config.session_ttl,
    )
    return {"Cookie": f"playify_webui={ctx.signer.dumps(payload)}"}


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


async def test_public_config_is_open(app_bundle):
    response = await app_bundle.client.get("/api/config")
    assert response.status == 200
    body = await response.json()
    assert body["oauth"] is True


async def test_requires_authentication(app_bundle):
    response = await app_bundle.client.get("/api/me")
    assert response.status == 401
    body = await response.json()
    assert body["error"] == "unauthenticated"


async def test_me_lists_voice_sessions(app_bundle):
    response = await app_bundle.client.get("/api/me", headers=auth_headers(app_bundle.ctx))
    assert response.status == 200
    body = await response.json()
    assert len(body["sessions"]) == 1
    session = body["sessions"][0]
    assert session["guild_id"] == str(GUILD_ID)
    assert session["channel_name"] == "Lounge"
    assert session["listener_count"] == 1


async def test_people_outside_the_voice_channel_are_denied(app_bundle):
    headers = auth_headers(
        app_bundle.ctx, {"id": "99", "username": "stranger", "global_name": "Stranger"}
    )
    response = await app_bundle.client.get(
        f"/api/state?guild_id={GUILD_ID}", headers=headers
    )
    assert response.status == 403
    body = await response.json()
    assert body["error"] == "not_in_channel"
    assert "Lounge" in body["message"]

    me = await app_bundle.client.get("/api/me", headers=headers)
    assert me.status == 200
    assert (await me.json())["sessions"] == []


async def test_forged_cookie_is_rejected(app_bundle):
    payload = build_session_payload({"id": str(USER_ID), "username": "x"}, 600)
    forged = TokenSigner("another-secret").dumps(payload)
    response = await app_bundle.client.get(
        "/api/me", headers={"Cookie": f"playify_webui={forged}"}
    )
    assert response.status == 401


async def test_state_contains_player_snapshot(app_bundle):
    world = app_bundle.world
    world.player.current_info = {
        "title": "Nightcall",
        "artist": "Kavinsky",
        "duration": 258,
        "thumbnail": "https://example.test/art.jpg",
    }
    world.player.start_time = 30
    world.player.playback_started_at = time.time()
    world.player.volume = 0.55
    world.vc._playing = True
    world.player.queue.put_nowait({"title": "Next", "url": "https://example.test/next"})

    response = await app_bundle.client.get(
        f"/api/state?guild_id={GUILD_ID}", headers=auth_headers(app_bundle.ctx)
    )
    assert response.status == 200
    snapshot = (await response.json())["state"]
    assert snapshot["playback"]["state"] == "playing"
    assert snapshot["playback"]["current"]["title"] == "Nightcall"
    assert snapshot["playback"]["position"] >= 30
    assert snapshot["queue"]["count"] == 1
    assert snapshot["settings"]["volume"] == 55


async def test_voice_state_fallback_when_member_cache_misses(app_bundle):
    """Large guilds may have an incomplete member cache; voice states save us."""
    world = app_bundle.world
    world.channel.members = []
    world.guild.voice_states = {USER_ID: SimpleNamespace(channel=world.channel)}

    response = await app_bundle.client.get(
        f"/api/state?guild_id={GUILD_ID}", headers=auth_headers(app_bundle.ctx)
    )
    assert response.status == 200
    snapshot = (await response.json())["state"]
    # The listener is still recognised through the voice-state map.
    assert snapshot["session"]["listener_count"] == 1


async def test_invalid_guild_and_unknown_action(app_bundle):
    response = await app_bundle.client.get("/api/state?guild_id=nope", headers=auth_headers(app_bundle.ctx))
    assert response.status == 400

    server._rate_limits.clear()
    response = await app_bundle.client.post(
        "/api/action",
        json={"guild_id": GUILD_ID, "action": "make_coffee"},
        headers=auth_headers(app_bundle.ctx),
    )
    assert response.status == 404


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


async def test_callback_rejects_missing_state(app_bundle):
    response = await app_bundle.client.get(
        "/auth/callback?code=abc&state=wrong",
        headers={"Cookie": "playify_webui_oauth=expected"},
        allow_redirects=False,
    )
    assert response.status == 400


async def test_login_redirects_to_discord(app_bundle):
    response = await app_bundle.client.get("/auth/login", allow_redirects=False)
    assert response.status == 302
    location = response.headers["Location"]
    assert location.startswith("https://discord.com/oauth2/authorize?")
    assert "redirect_uri=http://localhost:8080/auth/callback" in location
    assert "scope=identify" in location


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


async def test_action_requires_same_origin_when_origin_present(app_bundle):
    response = await app_bundle.client.post(
        "/api/action",
        json={"guild_id": GUILD_ID, "action": "pause"},
        headers={**auth_headers(app_bundle.ctx), "Origin": "https://evil.example"},
    )
    assert response.status == 403


async def test_toggle_pause_updates_player(app_bundle):
    world = app_bundle.world
    world.player.current_info = {"title": "Song", "duration": 100}
    world.player.start_time = 10.0
    world.player.playback_started_at = time.time() - 4
    world.vc._playing = True

    response = await app_bundle.client.post(
        "/api/action",
        json={"guild_id": GUILD_ID, "action": "toggle"},
        headers=auth_headers(app_bundle.ctx),
    )
    assert response.status == 200
    assert world.vc.is_paused()
    assert world.player.playback_started_at is None
    assert 13.5 <= world.player.start_time <= 15.0

    server._rate_limits.clear()
    response = await app_bundle.client.post(
        "/api/action",
        json={"guild_id": GUILD_ID, "action": "toggle"},
        headers=auth_headers(app_bundle.ctx),
    )
    assert response.status == 200
    assert not world.vc.is_paused()
    assert world.player.playback_started_at is not None


async def test_volume_is_clamped(app_bundle):
    world = app_bundle.world
    response = await app_bundle.client.post(
        "/api/action",
        json={"guild_id": GUILD_ID, "action": "volume", "data": {"volume": 900}},
        headers=auth_headers(app_bundle.ctx),
    )
    assert response.status == 200
    assert world.player.volume == 2.0


async def test_remove_from_queue(app_bundle):
    world = app_bundle.world
    world.player.queue.put_nowait({"title": "A", "url": "a"})
    world.player.queue.put_nowait({"title": "B", "url": "b"})

    response = await app_bundle.client.post(
        "/api/action",
        json={"guild_id": GUILD_ID, "action": "remove", "data": {"index": 0}},
        headers=auth_headers(app_bundle.ctx),
    )
    assert response.status == 200
    assert [item["title"] for item in world.player.queue._queue] == ["B"]


async def test_server_settings_require_the_manage_server_permission(app_bundle, monkeypatch):
    world = app_bundle.world

    response = await app_bundle.client.post(
        "/api/action",
        json={"guild_id": GUILD_ID, "action": "default_volume", "data": {"volume": 80}},
        headers=auth_headers(app_bundle.ctx),
    )
    assert response.status == 403
    assert world.state.default_volume == 1.0

    server._rate_limits.clear()
    world.listener.guild_permissions = SimpleNamespace(manage_guild=True)
    response = await app_bundle.client.post(
        "/api/action",
        json={"guild_id": GUILD_ID, "action": "default_volume", "data": {"volume": 80}},
        headers=auth_headers(app_bundle.ctx),
    )
    assert response.status == 200
    assert world.state.default_volume == 0.8

    # ``kawaii`` needs ``Locale`` from the bot core, which is not installed here.
    import enum
    import sys

    class FakeLocale(enum.Enum):
        EN_US = "en-US"
        EN_X_KAWAII = "en-x-kawaii"

    monkeypatch.setitem(sys.modules, "src.playify.core", SimpleNamespace(Locale=FakeLocale))
    world.state.locale = FakeLocale.EN_US

    server._rate_limits.clear()
    response = await app_bundle.client.post(
        "/api/action",
        json={"guild_id": GUILD_ID, "action": "kawaii", "data": {"enabled": True}},
        headers=auth_headers(app_bundle.ctx),
    )
    assert response.status == 200
    assert world.state.locale is FakeLocale.EN_X_KAWAII


async def test_websocket_requires_voice_membership(app_bundle):
    """Denied sockets now close with 4003 + a reason instead of a 403 handshake."""
    headers = auth_headers(
        app_bundle.ctx, {"id": "99", "username": "stranger", "global_name": "Stranger"}
    )
    ws = await app_bundle.client.ws_connect(f"/ws?guild_id={GUILD_ID}", headers=headers)
    message = await asyncio.wait_for(ws.receive(), 3)
    assert message.type.name == "CLOSE"
    assert ws.close_code == 4003
    assert "Lounge" in (message.extra or "")
    await ws.close()


async def test_websocket_streams_state_and_accepts_actions(app_bundle):
    world = app_bundle.world
    world.player.current_info = {"title": "Song", "duration": 100}
    world.player.start_time = 5.0
    world.player.playback_started_at = time.time()
    world.vc._playing = True

    ws = await app_bundle.client.ws_connect(
        f"/ws?guild_id={GUILD_ID}", headers=auth_headers(app_bundle.ctx)
    )
    try:
        hello = await asyncio.wait_for(ws.receive_json(), 3)
        assert hello["type"] == "hello"

        snapshot = await asyncio.wait_for(ws.receive_json(), 3)
        assert snapshot["type"] == "state"
        assert snapshot["data"]["playback"]["current"]["title"] == "Song"

        await ws.send_json({"type": "action", "request_id": 1, "action": "pause"})
        while True:
            message = await asyncio.wait_for(ws.receive_json(), 3)
            if message.get("type") == "result":
                break
        assert message["ok"] is True
        assert world.vc.is_paused()
    finally:
        await ws.close()


async def test_websocket_revokes_access_when_listener_leaves(app_bundle):
    world = app_bundle.world
    ws = await app_bundle.client.ws_connect(
        f"/ws?guild_id={GUILD_ID}", headers=auth_headers(app_bundle.ctx)
    )
    try:
        hello = await asyncio.wait_for(ws.receive_json(), 3)
        assert hello["type"] == "hello"

        # The listener leaves the voice channel.
        world.channel.members.remove(world.listener)
        world.listener.voice.channel = None

        while True:
            message = await asyncio.wait_for(ws.receive_json(), 5)
            if message.get("type") == "revoked":
                break
        assert "voice channel" in message["message"]
    finally:
        await ws.close()


async def test_bot_missing_from_voice_is_reported_as_such(app_bundle):
    """No voice connection at all must not look like "join the channel"."""
    world = app_bundle.world
    world.bot.voice_clients = []

    response = await app_bundle.client.get(
        f"/api/state?guild_id={GUILD_ID}", headers=auth_headers(app_bundle.ctx)
    )
    assert response.status == 403
    body = await response.json()
    assert body["error"] == "bot_not_in_voice"
    assert "not connected" in body["message"].lower()


async def test_membership_survives_an_empty_member_cache(app_bundle):
    """Voice states are authoritative even when guild.get_member() fails."""
    import types

    world = app_bundle.world
    world.channel.members = []
    world.guild._members = {}
    world.listener.voice = None
    world.guild.voice_states = {
        USER_ID: types.SimpleNamespace(
            channel=world.channel, member=None, user=world.listener
        )
    }

    response = await app_bundle.client.get(
        f"/api/state?guild_id={GUILD_ID}", headers=auth_headers(app_bundle.ctx)
    )
    assert response.status == 200
    body = await response.json()
    assert body["state"]["session"]["listener_count"] == 1


async def test_19_digit_guild_ids_are_handled_exactly(app_bundle):
    """Discord snowflakes are bigger than a JS Number: keep them as strings."""
    world = app_bundle.world
    snowflake = 1137104646637359100
    world.guild.id = snowflake
    world.state.guild_id = snowflake

    response = await app_bundle.client.get(
        f"/api/state?guild_id={snowflake}", headers=auth_headers(app_bundle.ctx)
    )
    assert response.status == 200
    session = (await response.json())["state"]["session"]
    assert session["guild_id"] == str(snowflake)


async def test_unknown_guild_falls_back_to_the_only_session(app_bundle):
    """A stale/truncated id must not lock the only eligible listener out."""
    response = await app_bundle.client.get(
        "/api/state?guild_id=1137104646637359104",  # rounded by the browser
        headers=auth_headers(app_bundle.ctx),
    )
    assert response.status == 200
    session = (await response.json())["state"]["session"]
    assert session["guild_id"] == str(GUILD_ID)


async def test_diagnostics_explain_the_access_state(app_bundle):
    response = await app_bundle.client.get(
        f"/api/diagnostics?guild_id={GUILD_ID}", headers=auth_headers(app_bundle.ctx)
    )
    assert response.status == 200
    body = await response.json()
    assert body["requested_guild_known"] is True
    assert body["bot_voice_channels"][0]["channel_name"] == "Lounge"
    assert body["guild_voice_states"][0]["bot_is_in"] == "Lounge"
    assert body["sessions"][0]["guild_id"] == str(GUILD_ID)


async def test_bot_playing_in_another_server_is_explained(app_bundle):
    """Being connected elsewhere must say *where*, not just "another server"."""
    world = app_bundle.world
    other_guild = Guild(7, "Other Server")
    other_channel = Channel(8, "lofi", other_guild, [])
    world.bot.voice_clients = [VoiceClient(other_channel)]

    response = await app_bundle.client.get(
        f"/api/state?guild_id={GUILD_ID}", headers=auth_headers(app_bundle.ctx)
    )
    assert response.status == 403
    message = (await response.json())["message"]
    assert "Other Server" in message
    assert "#lofi" in message
    assert "/play" in message


async def test_access_works_with_only_voice_state_user_ids(app_bundle):
    """The real bot has no members intent: only user ids are available.

    This is the exact production case that used to answer "you are not in the
    channel" while the listener was sitting in it.
    """
    import types

    world = app_bundle.world
    world.channel.members = []
    world.guild._members = {}
    world.listener.voice = None
    world.guild.voice_states = {
        USER_ID: types.SimpleNamespace(channel=world.channel, member=None, user=None)
    }

    response = await app_bundle.client.get(
        f"/api/state?guild_id={GUILD_ID}", headers=auth_headers(app_bundle.ctx)
    )
    assert response.status == 200
    session = (await response.json())["state"]["session"]
    assert session["channel_name"] == "Lounge"
    assert session["listener_count"] == 1
    assert session["listeners"][0]["name"] in {"Listener", "listener"}


async def test_zombie_voice_client_still_grants_access(app_bundle):
    """A dead voice client must not hide the bot from the people in the channel."""
    import types

    world = app_bundle.world
    world.vc.connected = False
    world.guild.me = types.SimpleNamespace(
        voice=types.SimpleNamespace(channel=world.channel)
    )

    response = await app_bundle.client.get(
        f"/api/state?guild_id={GUILD_ID}", headers=auth_headers(app_bundle.ctx)
    )
    assert response.status == 200
    session = (await response.json())["state"]["session"]
    assert session["connected"] is False
    assert session["channel_name"] == "Lounge"


async def test_suggestions_are_proxied_and_access_checked(app_bundle, monkeypatch):
    async def fake_suggest(query, limit=6):
        return ["daft punk", "daft punk one more time"]

    monkeypatch.setattr(actions, "suggest", fake_suggest)

    response = await app_bundle.client.get(
        f"/api/suggest?guild_id={GUILD_ID}&q=daft",
        headers=auth_headers(app_bundle.ctx),
    )
    assert response.status == 200
    assert (await response.json())["suggestions"][0] == "daft punk"

    stranger = auth_headers(
        app_bundle.ctx, {"id": "99", "username": "stranger", "global_name": "Stranger"}
    )
    denied = await app_bundle.client.get(
        f"/api/suggest?guild_id={GUILD_ID}&q=daft", headers=stranger
    )
    assert denied.status == 403


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def test_position_interpolation_uses_playback_speed(world: World):
    player = world.player
    player.current_info = {"title": "Song", "duration": 300}
    player.start_time = 20.0
    player.playback_speed = 1.2
    player.playback_started_at = 1000.0
    world.vc._playing = True

    session = type("Session", (), {"public": lambda self: {}, "voice_client": world.vc})()
    snapshot = build_snapshot(world.state, session, now=1010.0)
    assert snapshot["playback"]["position"] == pytest.approx(32.0)


def test_keepalive_silence_reports_idle(world: World):
    world.player.is_playing_silence = True
    world.vc._playing = True
    session = type("Session", (), {"public": lambda self: {}, "voice_client": world.vc})()
    snapshot = build_snapshot(world.state, session)
    assert snapshot["playback"]["state"] == "keepalive"
    assert snapshot["playback"]["current"] is None


# ---------------------------------------------------------------------------
# Voice health watchdog
# ---------------------------------------------------------------------------


def test_playback_position_matches_the_player_clock(world: World):
    from src.playify.services.voice_health import playback_position

    world.player.start_time = 12.0
    world.player.playback_started_at = 1000.0
    world.player.playback_speed = 0.5
    assert playback_position(world.player, now=1010.0) == pytest.approx(17.0)


def test_voice_channel_prefers_the_gateway_state(world: World):
    import types

    from src.playify.services.voice_health import voice_channel_of

    other = types.SimpleNamespace(id=99, name="Elsewhere")
    world.guild.me = types.SimpleNamespace(voice=types.SimpleNamespace(channel=other))
    assert voice_channel_of(world.guild) is other


def test_watchdog_only_heals_missing_connections(world: World):
    import types

    from src.playify.services.voice_health import _should_heal

    world.player.current_info = {"title": "Song"}
    world.guild.me = types.SimpleNamespace(
        voice=types.SimpleNamespace(channel=world.channel)
    )
    world.guild.voice_client = world.vc

    # healthy client: nothing to do
    assert _should_heal(world.guild, world.state, world.player) is False

    # dead client with a track in progress: heal
    world.vc.connected = False
    assert _should_heal(world.guild, world.state, world.player) is True

    # bot not in voice at all (kicked): leave it alone
    world.guild.me = None
    world.guild.voice_client = None
    assert _should_heal(world.guild, world.state, world.player) is False
