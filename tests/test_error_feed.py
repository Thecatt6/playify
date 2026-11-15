"""Tests for the ring buffer that feeds the diagnostics card."""

from __future__ import annotations

import logging

import pytest

from src.playify.services.error_feed import ErrorFeed, ErrorFeedHandler, install


def _record(level: int, message: str, name: str = "playify.test") -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_only_warnings_and_errors_are_kept():
    feed = ErrorFeed()
    feed.record_from_log(_record(logging.INFO, "starting up"))
    feed.record_from_log(_record(logging.DEBUG, "details"))
    feed.record_from_log(_record(logging.WARNING, "spotify token expired"))
    feed.record_from_log(_record(logging.ERROR, "playback died"))

    snapshot = feed.snapshot()
    assert snapshot["total"] == 2
    assert snapshot["warnings"] == 1
    assert snapshot["errors"] == 1
    assert [item["level"] for item in snapshot["recent"]] == ["warning", "error"]


def test_the_buffer_is_bounded_and_keeps_the_newest():
    feed = ErrorFeed(capacity=3)
    for index in range(10):
        feed.record("warning", "playify", f"problem {index}")

    snapshot = feed.snapshot(limit=10)
    assert snapshot["total"] == 10  # the counters keep counting
    assert [item["message"] for item in snapshot["recent"]] == [
        "problem 7",
        "problem 8",
        "problem 9",
    ]


def test_long_messages_are_truncated():
    feed = ErrorFeed()
    feed.record("error", "playify", "x" * 5000)
    assert len(feed.snapshot()["recent"][0]["message"]) == 300


def test_snapshot_limits_the_recent_list():
    feed = ErrorFeed()
    for index in range(10):
        feed.record("warning", "playify", f"w{index}")
    assert len(feed.snapshot(limit=2)["recent"]) == 2


def test_clear_resets_counters_and_entries():
    feed = ErrorFeed()
    feed.record("error", "playify", "boom")
    feed.clear()
    snapshot = feed.snapshot()
    assert snapshot == {"total": 0, "warnings": 0, "errors": 0, "recent": []}


def test_the_handler_writes_into_the_feed():
    feed = ErrorFeed()
    logger = logging.getLogger("playify.test.handler")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    handler = ErrorFeedHandler(feed)
    logger.addHandler(handler)
    try:
        logger.info("ignored")
        logger.warning("kept %s", "with args")
    finally:
        logger.removeHandler(handler)
    assert [item["message"] for item in feed.snapshot()["recent"]] == ["kept with args"]


def test_install_is_idempotent():
    feed = ErrorFeed()
    root = logging.getLogger()
    before = len(root.handlers)
    try:
        install(feed)
        install(feed)
        assert len(root.handlers) == before + 1
    finally:
        for handler in list(root.handlers):
            if isinstance(handler, ErrorFeedHandler) and handler.feed is feed:
                root.removeHandler(handler)


def test_a_broken_format_string_does_not_raise():
    feed = ErrorFeed()
    record = _record(logging.ERROR, "value is %s")
    record.args = ()  # missing argument on purpose
    feed.record_from_log(record)  # must not raise
    assert feed.snapshot()["errors"] == 1


@pytest.mark.parametrize("level", ["warning", "error"])
def test_entries_carry_a_timestamp_and_the_logger_name(level):
    feed = ErrorFeed(clock=lambda: 1234.5)
    feed.record(level, "playify.voice", "something happened")
    entry = feed.snapshot()["recent"][0]
    assert entry == {
        "time": 1234.5,
        "level": level,
        "logger": "playify.voice",
        "message": "something happened",
    }
