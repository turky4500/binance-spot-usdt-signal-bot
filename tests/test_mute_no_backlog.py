"""إثبات عدم وجود «طابور رسائل» بعد الاستئناف من الإسكات.

الإسكات يحدث لحظة الإرسال (تُسقَط الرسالة ولا تُخزَّن)، والحالة تتقدّم
طبيعيًا — لذلك لا يُعاد إرسال أي شيء قديم عند رفع TELEGRAM_MUTE.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.telegram_client import TelegramClient  # noqa: E402
from test_zigzag_flow import (  # noqa: E402
    entry_df,
    make_bot,
    server_time_after_entry,
)
from test_zigzag_report import ms, seed_day  # noqa: E402


class RecordingResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"ok": True}


def muted_client_with_recorder(monkeypatch) -> tuple[TelegramClient, list]:
    """عميل حقيقي مُسكت مع اعتراض الشبكة لتسجيل أي محاولة إرسال."""
    monkeypatch.setenv("TELEGRAM_MUTE", "1")
    client = TelegramClient("token", "chat")
    calls: list = []
    client.session.post = lambda *a, **k: calls.append(a) or RecordingResponse()  # type: ignore[assignment]
    return client, calls


def test_muted_entry_not_resent_after_unmute(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    client, calls = muted_client_with_recorder(monkeypatch)
    bot.telegram = client
    bot.symbols = ["BTCUSDT"]

    df = entry_df()
    bot.process_new_bottoms({"BTCUSDT": df}, server_time_after_entry())

    # أُسقطت رسالة الدخول، لكن الحالة والدفتر تسجّلا
    assert calls == []
    assert "BTCUSDT" in bot.state["open_signals"]
    assert (tmp_path / "trades_log_zigzag.csv").exists()

    # «استئناف»: نفس العميل بلا إسكات — لا شيء يُعاد إرساله
    client.muted = False
    bot.process_new_bottoms({"BTCUSDT": df}, server_time_after_entry() + 5 * 60_000)
    assert calls == []

    # ولا رسالة «نجاح/خسارة» لصفقة قديمة لم تُعلن أصلًا عند الاستئناف بلا أحداث
    bot.binance = type("FakeBinance", (), {
        "get_klines_range": lambda self, **kw: pd.DataFrame(),
    })()
    bot.monitor_open_signals(server_time_after_entry() + 10 * 60_000, {})
    assert calls == []
    assert "BTCUSDT" in bot.state["open_signals"]


def test_muted_daily_report_not_resent_after_unmute(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    seed_day(bot, tmp_path)
    client, calls = muted_client_with_recorder(monkeypatch)
    bot.telegram = client

    bot.send_daily_report_if_due(ms(2026, 10, 30, 0, 5))
    assert calls == []
    assert bot.state["daily_report"]["last_reported_for_date"] == "2026-10-29"

    # استئناف في اليوم نفسه: التقرير مُسجَّل كمرسل ⇒ لا تكرار
    client.muted = False
    bot.send_daily_report_if_due(ms(2026, 10, 30, 0, 30))
    assert calls == []


def test_many_muted_messages_leave_no_queue(tmp_path, monkeypatch):
    """10 أيام إسكات = صفر رسائل مؤجلة (لا يوجد مخزن أو عدّاد تراكمي)."""
    bot = make_bot(tmp_path)
    client, calls = muted_client_with_recorder(monkeypatch)
    bot.telegram = client

    for i in range(500):
        bot._safe_send(f"رسالة قديمة {i}")
    assert calls == []

    client.muted = False
    bot._safe_send("رسالة جديدة")
    assert len(calls) == 1  # الجديدة فقط
