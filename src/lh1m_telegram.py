"""Deadline-aware transport for the isolated L/H system only.

The existing Telegram client retries an explicit HTTP 429, which is safe to
retry but may consume enough time for a signal to become stale. Check *after*
its pacing/backoff and immediately before each HTTP attempt. An accepted
request can still arrive late due to network latency; that cannot be undone.
"""
from __future__ import annotations

import time
from typing import Callable

from .telegram_client import TelegramClient


class SignalDeadlineExpired(RuntimeError):
    """Definitely no request is about to be made; any prior attempt got 429."""


class SignalTelegramClient(TelegramClient):
    def __init__(self, token: str, chat_id: str, *, clock: Callable[[], int] | None = None,
                 timeout: int = 20) -> None:
        super().__init__(token, chat_id, timeout=timeout)
        self._deadline_ms: int | None = None
        self._clock_ms = clock or (lambda: int(time.time() * 1000))

    def _wait_turn(self) -> None:
        super()._wait_turn()
        if self._deadline_ms is not None and self._clock_ms() > self._deadline_ms:
            raise SignalDeadlineExpired("signal deadline elapsed before Telegram request")

    def send_before(self, text: str, deadline_ms: int) -> None:
        if self._deadline_ms is not None:
            raise RuntimeError("nested Telegram delivery is unsupported")
        self._deadline_ms = int(deadline_ms)
        try:
            # All retries occur inside send_message and each calls _wait_turn.
            super().send_message(text)
        finally:
            self._deadline_ms = None
