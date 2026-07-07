"""Telethon-backed Telegram client for the Tier-2 bot adapter.

Talks to a music-download bot as a *user* account (bots can't message other
bots, so a user session is required). The user supplies Telegram API creds and a
pre-authorised session string — this module never handles the phone-login/2FA
(set the session up once with Telethon's StringSession, out of band).

Targets the common "send query -> bot replies with the audio file" pattern.
All network I/O; excluded from coverage. Selection logic lives in the adapter.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

Clock = Callable[[], float]
Sleep = Callable[[float], None]
# Fetch messages the bot has sent since our request; returns the raw list.
Collect = Callable[[], list[Any]]


def collect_until_deadline(
    collect: Collect,
    has_audio: Callable[[list[Any]], bool],
    *,
    request_timeout_s: float,
    poll_interval_s: float,
    sleep: Sleep,
    clock: Clock,
) -> list[Any]:
    """Poll ``collect`` until it yields an audio-bearing reply or the deadline passes.

    A silent bot must never wait forever: the loop is bounded by a monotonic
    deadline (``clock`` + ``request_timeout_s``). Raises ``TimeoutError`` when the
    deadline is reached with no audio reply. ``clock``/``sleep`` are injected so
    the deadline is unit-tested without real wall-clock waits.
    """
    deadline = clock() + request_timeout_s
    while True:
        sleep(poll_interval_s)
        messages = collect()
        if has_audio(messages):
            return messages
        if clock() >= deadline:
            raise TimeoutError("telegram bot did not reply within 90s")


class TelegramBotClient:  # pragma: no cover - network I/O
    """Query a music bot and download the audio it returns."""

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session: str,
        bot: str,
        *,
        collect_wait_s: float = 10.0,
        request_timeout_s: float = 90.0,
        clock: Clock = time.monotonic,
        sleep: Sleep = time.sleep,
    ) -> None:
        self.bot = bot
        self.collect_wait_s = collect_wait_s
        self.request_timeout_s = request_timeout_s
        self._clock = clock
        self._sleep = sleep
        from telethon.sessions import StringSession
        from telethon.sync import TelegramClient  # sync facade over the async API

        self._client = TelegramClient(StringSession(session), api_id, api_hash)
        self._client.connect()

    def request(self, query: str) -> list[dict[str, Any]]:
        """Send the query, wait for the bot to reply, return audio-bearing messages.

        Bounded by ``request_timeout_s`` (default 90s): a silent bot raises
        ``TimeoutError`` instead of pinning the worker thread forever.
        """
        from telethon.tl.types import DocumentAttributeAudio

        sent = self._client.send_message(self.bot, query)

        def collect() -> list[Any]:
            return list(self._client.get_messages(self.bot, limit=15, min_id=sent.id))

        def has_audio(messages: list[Any]) -> bool:
            return any(
                getattr(m, "file", None) is not None
                and (m.audio or (getattr(m.file, "mime_type", "") or "").startswith("audio"))
                for m in messages
            )

        messages = collect_until_deadline(
            collect,
            has_audio,
            request_timeout_s=self.request_timeout_s,
            poll_interval_s=self.collect_wait_s,
            sleep=self._sleep,
            clock=self._clock,
        )

        out: list[dict[str, Any]] = []
        for m in messages:
            file = getattr(m, "file", None)
            if file is None or not (m.audio or (file.mime_type or "").startswith("audio")):
                out.append({"text": m.message or ""})
                continue
            duration = 0
            for attr in getattr(m.document, "attributes", []) or []:
                if isinstance(attr, DocumentAttributeAudio):
                    duration = attr.duration or 0
            size = file.size or 0
            out.append(
                {
                    "ref": m,
                    "audio": {
                        "file_name": file.name or "audio",
                        "mime_type": file.mime_type or "",
                        "file_size": size,
                        "duration": duration,
                        "bitrate": (size * 8 // duration) if duration else 0,
                    },
                }
            )
        return out

    def download(self, ref: Any, dest: Path) -> None:
        self._client.download_media(ref, file=str(dest))

    def close(self) -> None:
        self._client.disconnect()
