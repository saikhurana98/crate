"""Deadline behaviour of the Telegram poll loop, tested with a fake clock.

The TelegramBotClient itself is network I/O (excluded from coverage); the
bounded polling logic lives in the pure ``collect_until_deadline`` helper so a
silent bot cannot pin a worker thread forever.
"""

from typing import Any

import pytest

from crate.infrastructure.telegram.telethon_client import collect_until_deadline


def _clock_from(times: list[float]) -> Any:
    it = iter(times)
    return lambda: next(it)


def test_returns_as_soon_as_audio_arrives() -> None:
    slept: list[float] = []
    polls = iter([[], ["audio-msg"]])  # empty first, then audio

    result = collect_until_deadline(
        collect=lambda: next(polls),
        has_audio=bool,
        request_timeout_s=90.0,
        poll_interval_s=10.0,
        sleep=slept.append,
        clock=_clock_from([0.0, 10.0, 20.0]),  # start, then two deadline checks
    )
    assert result == ["audio-msg"]
    assert slept == [10.0, 10.0]  # polled twice


def test_raises_timeouterror_when_bot_stays_silent() -> None:
    slept: list[float] = []
    # deadline = 0 + 90; clock advances 0 -> 30 -> 60 -> 90 (>= deadline)
    clock = _clock_from([0.0, 30.0, 60.0, 90.0])

    with pytest.raises(TimeoutError, match="did not reply within 90s"):
        collect_until_deadline(
            collect=lambda: [],  # never any audio
            has_audio=bool,
            request_timeout_s=90.0,
            poll_interval_s=30.0,
            sleep=slept.append,
            clock=clock,
        )
    assert slept  # it did poll at least once before giving up
