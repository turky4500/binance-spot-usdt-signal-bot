"""اختبارات بوابة الإسكات المؤقت TELEGRAM_MUTE في عميل تيليجرام المشترك."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.telegram_client import TelegramClient, is_muted  # noqa: E402


class FakeResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"ok": True}


def test_default_is_not_muted(monkeypatch):
    monkeypatch.delenv("TELEGRAM_MUTE", raising=False)
    assert is_muted() is False
    client = TelegramClient("token", "chat")
    assert client.muted is False


def test_mute_values(monkeypatch):
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("TELEGRAM_MUTE", value)
        assert is_muted() is True, value
    for value in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("TELEGRAM_MUTE", value)
        assert is_muted() is False, value


def test_muted_client_never_calls_telegram(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MUTE", "1")
    client = TelegramClient("token", "chat")
    calls: list = []
    client.session.post = lambda *a, **k: calls.append(a) or FakeResponse()  # type: ignore[assignment]

    client.send_message("📥 إشارة دخول — BTCUSDT")

    assert calls == []  # لا اتصال شبكي إطلاقًا
    assert client.muted is True


def test_unmuted_client_sends(monkeypatch):
    monkeypatch.delenv("TELEGRAM_MUTE", raising=False)
    client = TelegramClient("token", "chat")
    calls: list = []
    client.session.post = lambda *a, **k: calls.append((a, k)) or FakeResponse()  # type: ignore[assignment]

    client.send_message("نص")

    assert len(calls) == 1
    url = calls[0][0][0]
    assert url.endswith("/sendMessage")
