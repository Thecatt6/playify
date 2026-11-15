"""Tests for the Prometheus endpoint and the counters behind it."""

from __future__ import annotations

from src.playify.services import stats
from src.playify.webui import metrics


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_a_gauge_is_rendered_with_help_and_type():
    text = metrics.render_prometheus(
        [{"name": "playify_guilds", "help": "Guilds.", "type": "gauge", "value": 2}]
    )
    assert text.splitlines() == [
        "# HELP playify_guilds Guilds.",
        "# TYPE playify_guilds gauge",
        "playify_guilds 2",
    ]


def test_labels_are_sorted_and_escaped():
    text = metrics.render_prometheus(
        [
            {
                "name": "playify_queue_tracks",
                "value": 3,
                "labels": {"zone": 'a"b', "guild_id": "1137104646637359100"},
            }
        ]
    )
    assert 'guild_id="1137104646637359100",zone="a\\"b"' in text


def test_help_is_printed_once_per_metric():
    text = metrics.render_prometheus(
        [
            {"name": "m", "help": "Same help.", "value": 1, "labels": {"a": "1"}},
            {"name": "m", "help": "Same help.", "value": 2, "labels": {"a": "2"}},
        ]
    )
    assert text.count("# HELP m") == 1
    assert text.count("# TYPE m") == 1
    assert text.count("m{") == 2


def test_floats_are_shortened_but_keep_precision():
    text = metrics.render_prometheus(
        [
            {"name": "a", "value": 12.0},
            {"name": "b", "value": 12.3456},
            {"name": "c", "value": float("nan")},
            {"name": "d", "value": float("inf")},
            {"name": "e", "value": "not a number"},
        ]
    )
    assert "a 12" in text
    assert "b 12.346" in text
    assert "c 0" in text and "d 0" in text and "e 0" in text


def test_booleans_become_zero_and_one():
    text = metrics.render_prometheus([{"name": "flag", "value": True}])
    assert "flag 1" in text


def test_a_sample_without_a_name_is_skipped():
    assert metrics.render_prometheus([{"value": 1}]) == ""


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


class _Bot:
    def __init__(self, guilds=1, clients=1):
        self.guilds = [object()] * guilds
        self.voice_clients = [object()] * clients


class _Ctx:
    started_at = 1000.0


def test_collect_reports_the_aggregate_numbers(monkeypatch):
    monkeypatch.setattr(metrics.time, "time", lambda: 1060.0)
    samples = metrics.collect(
        bot=_Bot(guilds=2, clients=1),
        ctx=_Ctx(),
        ws_connections=3,
        feeding={"warnings": 4, "errors": 1},
        stats={"tracks_played": 9},
        guilds=[{"guild_id": "1", "listeners": 2, "queue": 5, "state": "playing"}],
    )
    by_name = {sample["name"]: sample for sample in samples if "labels" not in sample}
    assert by_name["playify_uptime_seconds"]["value"] == 60.0
    assert by_name["playify_guilds"]["value"] == 2
    assert by_name["playify_voice_clients"]["value"] == 1
    assert by_name["playify_webui_connections"]["value"] == 3
    assert by_name["playify_tracks_played_total"]["value"] == 9
    assert by_name["playify_warnings_total"]["value"] == 4
    assert by_name["playify_errors_total"]["value"] == 1

    labelled = [sample for sample in samples if sample.get("labels")]
    states = [s for s in labelled if s["name"] == "playify_playback_state"]
    assert states[0]["value"] == metrics.PLAYBACK_STATES["playing"]

    text = metrics.render_prometheus(samples)
    assert 'playify_playback_state{guild_id="1"} 1' in text
    assert 'playify_queue_tracks{guild_id="1"} 5' in text


def test_no_guilds_still_renders_the_global_metrics():
    samples = metrics.collect(
        bot=_Bot(guilds=0, clients=0),
        ctx=_Ctx(),
        ws_connections=0,
        feeding={"warnings": 0, "errors": 0},
        stats={"tracks_played": 0},
    )
    text = metrics.render_prometheus(samples)
    assert "playify_guilds 0" in text
    assert "playify_playback_state" not in text


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


def test_counters_start_at_zero_and_add_up():
    stats.reset()
    assert stats.get("tracks_played") == 0
    stats.inc("tracks_played")
    stats.inc("tracks_played", 2)
    assert stats.snapshot()["tracks_played"] == 3
    stats.reset()
    assert stats.get("tracks_played") == 0


