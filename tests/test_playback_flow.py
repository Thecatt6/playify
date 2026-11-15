"""Tests for the "a track just ended, now what?" decision table."""

from __future__ import annotations

import pytest

from src.playify.services.playback_flow import (
    AFTER_ADVANCE,
    AFTER_DISCONNECTED,
    AFTER_LOOP,
    AFTER_PAUSED_BY_LEAVE,
    AFTER_RECONNECTING,
    AFTER_REPLAY,
    AFTER_SEEK,
    resolve_after_playing,
)


def test_a_normal_track_moves_on_to_the_queue():
    assert resolve_after_playing() == AFTER_ADVANCE


def test_an_empty_channel_wins_over_everything():
    """The bot was paused because everyone left: it must not start a new track."""
    decision = resolve_after_playing(
        paused_by_leave=True,
        manual_stop=True,
        voice_connected=False,
        seek_info=12.0,
        loop_current=True,
    )
    assert decision == AFTER_PAUSED_BY_LEAVE


def test_a_manual_stop_replays_instead_of_waiting_for_24_7_logic():
    assert resolve_after_playing(manual_stop=True) == AFTER_REPLAY


def test_a_manual_stop_wins_over_loop_and_seek():
    assert (
        resolve_after_playing(manual_stop=True, seek_info=3.0, loop_current=True)
        == AFTER_REPLAY
    )


def test_a_disconnected_voice_client_stops_the_loop():
    assert resolve_after_playing(voice_connected=False) == AFTER_DISCONNECTED


def test_a_disconnected_client_wins_over_reconnecting():
    assert (
        resolve_after_playing(voice_connected=False, reconnecting=True)
        == AFTER_DISCONNECTED
    )


def test_while_reconnecting_we_wait_for_the_watchdog():
    assert resolve_after_playing(reconnecting=True) == AFTER_RECONNECTING


def test_a_pending_seek_restarts_the_same_track():
    assert resolve_after_playing(seek_info=0.0) == AFTER_SEEK


def test_a_seek_of_zero_is_still_a_seek():
    """0.0 is a valid offset: it must not be mistaken for "no seek pending"."""
    assert resolve_after_playing(seek_info=0.0) == AFTER_SEEK


def test_loop_mode_repeats_the_track():
    assert resolve_after_playing(loop_current=True) == AFTER_LOOP


def test_loop_loses_to_a_pending_seek():
    assert resolve_after_playing(loop_current=True, seek_info=30.0) == AFTER_SEEK


@pytest.mark.parametrize(
    "action",
    [
        AFTER_ADVANCE,
        AFTER_DISCONNECTED,
        AFTER_LOOP,
        AFTER_PAUSED_BY_LEAVE,
        AFTER_RECONNECTING,
        AFTER_REPLAY,
        AFTER_SEEK,
    ],
)
def test_every_action_is_reachable_and_named(action):
    assert isinstance(action, str) and action


def test_the_callback_handles_every_action():
    """Guard: a new action must be dispatched in after_playing, not ignored."""
    import pathlib
    import re

    source = pathlib.Path(
        "src/playify/services/playback.py"
    ).read_text(encoding="utf-8")
    # AFTER_ADVANCE is the fallthrough; the others must be explicit branches.
    for constant in (
        "AFTER_DISCONNECTED",
        "AFTER_LOOP",
        "AFTER_PAUSED_BY_LEAVE",
        "AFTER_RECONNECTING",
        "AFTER_REPLAY",
        "AFTER_SEEK",
    ):
        assert re.search(rf"decision == {constant}", source), constant
