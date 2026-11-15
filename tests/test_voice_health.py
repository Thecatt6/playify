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


def test_davey_0_2_with_discord_py_2_7_is_reported_as_silent(monkeypatch):
    """davey 0.2 rotates DAVE keys in a way discord.py 2.7 never triggers.

    The symptom is the worst kind: the bot connects, plays, and every listener
    hears silence because the audio cannot be decrypted.
    """
    versions = {"davey": "0.2.0", "discord.py": "2.7.1"}
    monkeypatch.setattr(voice_health, "_module_version", lambda name: versions.get(name))
    monkeypatch.setattr(voice_health, "_has_module", lambda name: True)
    monkeypatch.setattr(
        voice_health, "shutil", types.SimpleNamespace(which=lambda name: "/usr/bin/ffmpeg")
    )

    status = voice_health.dave_status()

    assert status["compatible"] is False
    assert "0.2.0" in status["reason"]
    assert "nobody hears it" in status["reason"]
    problems = voice_health.voice_readiness_problems()
    assert any("davey" in problem and "nobody hears" in problem for problem in problems), problems


def test_davey_0_1_is_compatible(monkeypatch):
    versions = {"davey": "0.1.7", "discord.py": "2.7.1"}
    monkeypatch.setattr(voice_health, "_module_version", lambda name: versions.get(name))

    assert voice_health.dave_status()["compatible"] is True


def test_discord_py_with_transition_support_accepts_davey_0_2(monkeypatch):
    versions = {"davey": "0.2.0", "discord.py": "2.8.0"}
    monkeypatch.setattr(voice_health, "_module_version", lambda name: versions.get(name))

    assert voice_health.dave_status()["compatible"] is True


def test_version_tuple_stops_at_non_numeric_chunks():
    assert voice_health._version_tuple("2.7.1") == (2, 7, 1)
    assert voice_health._version_tuple("0.2.0rc1") == (0, 2, 0)
    assert voice_health._version_tuple(None) == ()


class _FakeDaveSession:
    def __init__(self, ready: bool, code: str | None = "abc123"):
        self.ready = ready
        self.voice_privacy_code = code


class _FakeConnection:
    def __init__(self, protocol=1, session=None, can_encrypt=True):
        self.dave_protocol_version = protocol
        self.max_dave_protocol_version = 1
        self.dave_session = session
        self._can_encrypt = can_encrypt

    @property
    def can_encrypt(self):
        return self._can_encrypt


def test_dave_runtime_state_reports_an_encrypted_call():
    voice_client = types.SimpleNamespace(
        _connection=_FakeConnection(protocol=1, session=_FakeDaveSession(True))
    )

    state = voice_health.dave_runtime_state(voice_client)

    assert state["protocol"] == 1
    assert state["session"] is True and state["ready"] is True
    assert state["can_encrypt"] is True
    assert state["privacy_code"] == "abc123"


def test_dave_runtime_state_flags_plain_audio_in_an_encrypted_call():
    voice_client = types.SimpleNamespace(
        _connection=_FakeConnection(protocol=1, session=_FakeDaveSession(False), can_encrypt=False)
    )

    state = voice_health.dave_runtime_state(voice_client)

    assert state["protocol"] == 1
    assert state["can_encrypt"] is False
    assert state["ready"] is False


def test_dave_runtime_state_survives_broken_internals():
    class Exploding:
        @property
        def can_encrypt(self):
            raise RuntimeError("internals moved")

        dave_protocol_version = 0
        dave_session = None

    state = voice_health.dave_runtime_state(types.SimpleNamespace(_connection=Exploding()))

    assert state["protocol"] == 0
    assert state["can_encrypt"] is False
    assert voice_health.dave_runtime_state(types.SimpleNamespace())["protocol"] == 0


def _playing_player(connected=True, protocol=1, can_encrypt=False):
    voice_client = types.SimpleNamespace(
        is_connected=lambda: connected,
        _connection=_FakeConnection(
            protocol=protocol, session=_FakeDaveSession(can_encrypt), can_encrypt=can_encrypt
        ),
    )
    guild = types.SimpleNamespace(voice_client=voice_client, me=None)
    player = types.SimpleNamespace(
        voice_client=voice_client, current_info={"title": "x"}, playback_started_at=1.0
    )
    return guild, player, voice_client


def test_unencrypted_audio_in_an_encrypted_call_is_detected():
    guild, player, _ = _playing_player(can_encrypt=False)

    assert voice_health.needs_encryption_heal(guild, player) is True


def test_encrypted_playback_is_not_healed():
    guild, player, _ = _playing_player(can_encrypt=True)

    assert voice_health.needs_encryption_heal(guild, player) is False


def test_a_plain_call_is_not_healed():
    guild, player, _ = _playing_player(protocol=0, can_encrypt=False)

    assert voice_health.needs_encryption_heal(guild, player) is False


def test_nothing_playing_is_not_healed():
    guild, player, _ = _playing_player(can_encrypt=False)
    player.current_info = None
    player.playback_started_at = None

    assert voice_health.needs_encryption_heal(guild, player) is False


def test_encryption_heal_waits_for_the_grace_period(monkeypatch):
    monkeypatch.setattr(voice_health, "_unencrypted_since", {})
    guild, player, _ = _playing_player(can_encrypt=False)

    assert voice_health.encryption_heal_due(7, guild, player, now=100.0) is False
    assert voice_health.encryption_heal_due(7, guild, player, now=110.0) is False
    assert (
        voice_health.encryption_heal_due(
            7, guild, player, now=100.0 + voice_health.NO_ENCRYPT_GRACE
        )
        is True
    )


def test_encryption_state_is_forgotten_once_audio_encrypts_again(monkeypatch):
    monkeypatch.setattr(voice_health, "_unencrypted_since", {})
    guild, player, voice_client = _playing_player(can_encrypt=False)
    voice_health.encryption_heal_due(9, guild, player, now=100.0)
    assert 9 in voice_health._unencrypted_since

    voice_client._connection._can_encrypt = True
    assert voice_health.encryption_heal_due(9, guild, player, now=200.0) is False
    assert 9 not in voice_health._unencrypted_since
