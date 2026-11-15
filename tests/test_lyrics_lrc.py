"""Tests for the LRC parser that powers the lyrics view."""

from __future__ import annotations

from src.playify.services.lyrics_lrc import (
    current_index,
    is_lrc,
    parse_lrc,
    strip_lrc,
)

LRC = """\
[ar:M83]
[ti:Midnight City]
[00:12.34] Waiting in a car
[00:16.80][01:02.10] Waiting for a ride
[00:20.00]
[1:05.50] The chorus comes back
"""


def test_metadata_tags_never_become_lyrics():
    lines = parse_lrc(LRC)
    assert all(line["text"] not in {"M83", "Midnight City"} for line in lines)


def test_timestamps_are_converted_to_seconds():
    lines = parse_lrc(LRC)
    assert lines[0] == {"time": 12.34, "text": "Waiting in a car"}
    assert lines[-1]["time"] == 65.5  # [1:05.50] is one minute five seconds


def test_a_repeated_chorus_yields_one_entry_per_timestamp():
    lines = parse_lrc(LRC)
    ride = [line for line in lines if line["text"] == "Waiting for a ride"]
    assert [line["time"] for line in ride] == [16.8, 62.1]


def test_entries_are_sorted_even_when_the_file_is_not():
    text = "[00:30.00] second\n[00:10.00] first\n"
    assert [line["text"] for line in parse_lrc(text)] == ["first", "second"]


def test_empty_timestamped_lines_are_dropped():
    assert all(line["text"] for line in parse_lrc(LRC))


def test_plain_text_is_not_mistaken_for_lrc():
    assert is_lrc(LRC)
    assert not is_lrc("Just a plain lyric\nwithout timestamps")


def test_strip_lrc_returns_readable_lyrics_without_duplicates():
    plain = strip_lrc(LRC)
    assert plain.splitlines() == [
        "Waiting in a car",
        "Waiting for a ride",
        "The chorus comes back",
    ]


def test_the_current_line_follows_the_position():
    lines = parse_lrc(LRC)
    assert current_index(lines, 0) == -1
    assert current_index(lines, 12.33) == -1
    assert lines[current_index(lines, 12.34)]["text"] == "Waiting in a car"
    assert lines[current_index(lines, 17.0)]["text"] == "Waiting for a ride"
    assert lines[current_index(lines, 999)]["text"] == "The chorus comes back"


def test_the_index_ignores_broken_times():
    assert current_index([{"time": "nope"}, {"time": 5}], 10) == 1


def test_parsing_an_empty_document_is_safe():
    assert parse_lrc("") == []
    assert strip_lrc("") == ""
