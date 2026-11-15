"""Tests for the voice watchdog (zombie connections + resuming playback)."""

from __future__ import annotations

import asyncio
import sys
import time
import types

import pytest

from src.playify.services import voice_health


class FakeChannel:
    def __init__(self, channel_id: int = 5, name: str = "Lounge") -> None:
        self.id = channel_id
        self.name = name
        self.connect_calls: list[dict] = []

    async def connect(self, **kwargs):
        self.connect_calls.append(kwargs)
        return FakeVoiceClient(self)


class FakeVoiceClient:
    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel
        self.disconnected = False

    def is_connected(self) -> bool:
        return not self.disconnected

    def is_playing(self) -> bool:
        return False

    def is_paused(self) -> bool:
        return False

    async def disconnect(self, force: bool = False) -> None:
        self.disconnected = True


class FakeGuild:
    def __init__(self, channel: FakeChannel | None) -> None:
        self.id = 42
        self.voice_client = None
        self._channel = channel

    @property
    def me(self):
        if self._channel is None:
            return None
        return types.SimpleNamespace(
            voice=types.SimpleNamespace(channel=self._channel)
        )


class FakePlayer:
    def __init__(self) -> None:
        self.voice_client = None
        self.current_info = {"title": "Midnight City", "duration": 244}
        self.start_time = 10.0
        self.playback_started_at = None
        self.playback_speed = 1.0
        self.is_playing_silence = False
        self.is_cleaning = False
        self.is_reconnecting = False
        self.silence_task = None
        self.current_task = None


@pytest.fixture(autouse=True)
def clean_attempts(monkeypatch):
    """Isolate the module-level attempt bookkeeping between tests."""
    monkeypatch.setattr(voice_health, "_attempts", {})


@pytest.fixture
def audio_calls(monkeypatch):
    calls: list[tuple] = []

    playback = types.ModuleType("src.playify.services.playback")

    async def play_audio(guild_id, seek_time=0, is_a_loop=False, **kwargs):
        calls.append(("play_audio", guild_id, seek_time))

    playback.play_audio = play_audio

    voice = types.ModuleType("src.playify.services.voice")

    async def safe_stop(vc):
        calls.append(("safe_stop", vc))

    voice.safe_stop = safe_stop

    monkeypatch.setitem(sys.modules, "src.playify.services.playback", playback)
    monkeypatch.setitem(sys.modules, "src.playify.services.voice", voice)
    return calls


def test_heal_reconnects_and_resumes_where_it_stopped(audio_calls):
    async def scenario():
        channel = FakeChannel()
        guild = FakeGuild(channel)
        player = FakePlayer()
        player.playback_started_at = time.time() - 5.0  # 5s into the track

        healed = await voice_health.heal_guild(guild, player, reason="test")
        await asyncio.sleep(0)  # let the resume task run
        return channel, guild, player, healed

    channel, guild, player, healed = asyncio.run(scenario())

    assert healed is True
    assert len(channel.connect_calls) == 1
    assert channel.connect_calls[0]["reconnect"] is True
    assert player.voice_client is not None
    assert player.voice_client.is_connected()

    assert audio_calls, "playback should be resumed after the reconnect"
    action, guild_id, seek_time = audio_calls[0]
    assert action == "play_audio"
    assert guild_id == 42
    assert seek_time == pytest.approx(15.0, abs=0.5)


def test_heal_gives_up_after_repeated_failures(audio_calls):
    channel = FakeChannel()
    guild = FakeGuild(channel)
    player = FakePlayer()
    limit = voice_health.HEAL_MAX_ATTEMPTS

    async def scenario():
        return [
            await voice_health.heal_guild(guild, player, reason="loop")
            for _ in range(limit + 1)
        ]

    results = asyncio.run(scenario())

    assert results[:limit] == [True] * limit
    assert results[-1] is False
    assert len(channel.connect_calls) == limit


def test_heal_skips_when_the_bot_is_not_in_a_channel(audio_calls):
    guild = FakeGuild(None)
    player = FakePlayer()

    assert asyncio.run(voice_health.heal_guild(guild, player)) is False
    assert player.voice_client is None


def test_heal_never_fights_an_in_progress_reconnect(audio_calls):
    channel = FakeChannel()
    guild = FakeGuild(channel)
    player = FakePlayer()
    player.is_reconnecting = True

    assert asyncio.run(voice_health.heal_guild(guild, player)) is False
    assert channel.connect_calls == []


def test_watchdog_requires_a_track_or_247_mode():
    channel = FakeChannel()
    guild = FakeGuild(channel)
    state = types.SimpleNamespace(_24_7_mode=False)

    player = FakePlayer()
    player.current_info = None
    assert voice_health._should_heal(guild, state, player) is False

    player.current_info = {"title": "Song"}
    assert voice_health._should_heal(guild, state, player) is True

    player.current_info = None
    state._24_7_mode = True
    assert voice_health._should_heal(guild, state, player) is True


def test_readiness_reports_missing_dependencies(monkeypatch):
    monkeypatch.setattr(voice_health, "shutil", types.SimpleNamespace(which=lambda name: None))
    monkeypatch.setattr(voice_health, "_has_module", lambda name: False)

    problems = voice_health.voice_readiness_problems()

    assert any("ffmpeg" in problem for problem in problems)
    assert any("PyNaCl" in problem for problem in problems)
    assert any("davey" in problem for problem in problems)
    assert voice_health.log_voice_readiness() is False
