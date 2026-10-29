"""اختبارات التقرير اليومي لنظام ZigZag++ (00:00 بتوقيت الرياض).

python -m pytest tests/ -q
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.zigzag_journal import append_zigzag_entry  # noqa: E402
from test_zigzag_flow import FakeTelegram, make_bot  # noqa: E402

RIYADH = ZoneInfo("Asia/Riyadh")


def ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=RIYADH).timestamp() * 1000)


def seed_journal(data_dir: Path, symbol: str, exit_ms: int, net: float) -> None:
    append_zigzag_entry(str(data_dir), {
        "trade_id": f"{symbol}-{exit_ms}",
        "symbol": symbol,
        "strategy": "ZigZag++ (ساعة)",
        "entry_price": 100.0,
        "target_price": 102.0,
        "stop_price": 95.0,
        "exit_time_ms": exit_ms,
        "exit_price": 101.0,
        "net_return_pct": net,
        "outcome": "win" if net > 0 else "loss",
    })


def seed_day(bot, data_dir: Path) -> None:
    bot.state["event_log"] = [
        {"type": "entry", "symbol": "AAAUSDT", "time_ms": ms(2026, 10, 29, 10)},
        {"type": "entry", "symbol": "BBBUSDT", "time_ms": ms(2026, 10, 29, 11)},
        {"type": "entry", "symbol": "OLDUSDT", "time_ms": ms(2026, 10, 28, 11)},  # يوم آخر
        {"type": "exit_win", "symbol": "AAAUSDT", "time_ms": ms(2026, 10, 29, 12)},
        {"type": "exit_loss", "symbol": "BBBUSDT", "time_ms": ms(2026, 10, 29, 13)},
    ]
    bot.state["open_signals"]["CCCUSDT"] = {"symbol": "CCCUSDT"}
    seed_journal(data_dir, "AAAUSDT", ms(2026, 10, 29, 12), 1.9)
    seed_journal(data_dir, "BBBUSDT", ms(2026, 10, 29, 13), -3.5)


def test_daily_report_content(tmp_path):
    bot = make_bot(tmp_path)
    seed_day(bot, tmp_path)

    bot.send_daily_report_if_due(ms(2026, 10, 30, 0, 5))

    text = bot.telegram.sent[-1]
    assert "📊 التقرير اليومي — ZigZag++ (ساعة)" in text
    assert "2026-10-29" in text
    assert "إشارات دخلت: 2" in text
    assert "نجاح: 1 • 🛑 خسارة: 1" in text
    assert "نسبة النجاح: 50.0%" in text
    assert "متوسط النتيجة (بعد العمولة): -0.8%" in text
    assert "مفتوحة حاليًا: 1" in text
    assert "أفضل عملة: AAAUSDT +1.9%" in text
    assert "أسوأ عملة: BBBUSDT -3.5%" in text
    assert bot.state["daily_report"]["last_reported_for_date"] == "2026-10-29"


def test_daily_report_once_per_day(tmp_path):
    bot = make_bot(tmp_path)
    seed_day(bot, tmp_path)

    bot.send_daily_report_if_due(ms(2026, 10, 30, 0, 5))
    first = len(bot.telegram.sent)
    bot.send_daily_report_if_due(ms(2026, 10, 30, 0, 55))
    assert len(bot.telegram.sent) == first

    # اليوم التالي: تقرير جديد (بلا صفقات) بمرة واحدة فقط
    bot.send_daily_report_if_due(ms(2026, 10, 31, 0, 5))
    assert len(bot.telegram.sent) == first + 1
    assert "إشارات دخلت: 0" in bot.telegram.sent[-1]
    assert "لا توجد صفقات مغلقة اليوم" in bot.telegram.sent[-1]


def test_daily_report_not_sent_outside_midnight(tmp_path):
    bot = make_bot(tmp_path)
    seed_day(bot, tmp_path)

    for hour in (1, 12, 23):
        bot.send_daily_report_if_due(ms(2026, 10, 30, hour, 0))

    assert bot.telegram.sent == []
    assert bot.state.get("daily_report", {}).get("last_reported_for_date", "") == ""


def test_daily_report_survives_telegram_failure(tmp_path):
    bot = make_bot(tmp_path, telegram=FakeTelegram(fail=True))
    seed_day(bot, tmp_path)

    bot.send_daily_report_if_due(ms(2026, 10, 30, 0, 5))

    # فشل الإرسال لا يمنع تسجيل التقرير ولا يرفع استثناء
    assert bot.state["daily_report"]["last_reported_for_date"] == "2026-10-29"
