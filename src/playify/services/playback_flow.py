"""What should happen when a track ends?

The ``after`` callback of :mod:`discord.player` used to be a wall of nested
conditionals inside ``play_audio``, which made the rules impossible to test and
easy to break. The precedence is subtle and every branch has bitten us at least
once:

* an empty voice channel wins over everything else (the bot must stay silent,
  not skip to the next song);
* a manual stop (skip/stop from the panel or a command) replays immediately,
  bypassing the 24/7 logic;
* a gone voice client means nothing will ever play again, so the progress clock
  must be stopped;
* while reconnecting we simply wait for the watchdog;
* a pending seek restarts the same track at the new offset;
* loop mode repeats the track;
* otherwise we move on to the next queue item.

Keeping the decision pure means it can be covered by fast tests, while the
callback itself only dispatches the resulting action.
"""

from __future__ import annotations

AFTER_PAUSED_BY_LEAVE = "paused_by_leave"
AFTER_REPLAY = "replay"
AFTER_DISCONNECTED = "disconnected"
AFTER_RECONNECTING = "reconnecting"
AFTER_SEEK = "seek"
AFTER_LOOP = "loop"
AFTER_ADVANCE = "advance"

ACTIONS = (
    AFTER_PAUSED_BY_LEAVE,
    AFTER_REPLAY,
    AFTER_DISCONNECTED,
    AFTER_RECONNECTING,
    AFTER_SEEK,
    AFTER_LOOP,
    AFTER_ADVANCE,
)


def resolve_after_playing(
    *,
    paused_by_leave: bool = False,
    manual_stop: bool = False,
    voice_connected: bool = True,
    reconnecting: bool = False,
    seek_info: float | None = None,
    loop_current: bool = False,
) -> str:
    """Return the action the playback loop should take.

    The order of the checks is the specification: first match wins.
    """
    if paused_by_leave:
        return AFTER_PAUSED_BY_LEAVE
    if manual_stop:
        return AFTER_REPLAY
    if not voice_connected:
        return AFTER_DISCONNECTED
    if reconnecting:
        return AFTER_RECONNECTING
    if seek_info is not None:
        return AFTER_SEEK
    if loop_current:
        return AFTER_LOOP
    return AFTER_ADVANCE
