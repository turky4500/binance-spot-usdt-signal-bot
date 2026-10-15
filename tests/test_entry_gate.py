"""بوابتا الدراسة فوق إشارة الدخول: كفاءة الاتجاه ER + تهدئة ما بعد الوقف."""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HOUR_MS = 3_600_000
BASE_MS = 1_700_000_000_000


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []

    def send_message(self, text: str) -> None:
        self.sent.append(text)


def make_frame(closes: list[float], highs=None, lows=None, start=BASE_MS) -> pd.DataFrame:
    n = len(closes)
    highs = highs or [c * 1.001 for c in closes]
    lows = lows or [c * 0.999 for c in closes]
    return pd.DataFrame({
        "open_time": [start + i * HOUR_MS for i in range(n)],
        "open": closes, "high": highs, "low": lows, "close": closes,
        "volume": [1000.0] * n,
        "close_time": [start + i * HOUR_MS + HOUR_MS - 1 for i in range(n)],
    })


def make_bot(tmp_path: Path):
    from app import SpotSignalBot
    from src.config import AppConfig
    from src.state import StateStore
    from src.strategy import StrategySettings

    bot = SpotSignalBot.__new__(SpotSignalBot)
    bot.data_dir = str(tmp_path)
    bot.telegram = FakeTelegram()
    bot.logger = logging.getLogger("gate-test")
    bot.config = AppConfig(telegram_bot_token="t", telegram_chat_id="c")
    bot.store = StateStore(str(tmp_path / "state.json"))
    bot.state = bot.store.load()
    bot.settings = StrategySettings()
    bot.symbols = ["AAAUSDT"]
    bot.halal_verdicts = {}
    bot.price_ticks = {}
    bot.get_halal_verdict = lambda symbol: "Halal ✓"
    return bot


def open_trade(bot, entry=100.0, stop=98.0, targets=(105.0, 110.0, 115.0)):
    trade = {
        "trade_id": f"AAAUSDT-{BASE_MS}",
        "symbol": "AAAUSDT",
        "entry_price": entry,
        "stop_price": stop,
        "targets": list(targets),
        "hit": [False, False, False],
        "entry_time": BASE_MS + HOUR_MS - 1,
        "entry_bar_open_time": BASE_MS,
        "last_target_check_ms": BASE_MS + HOUR_MS,
        "atr_at_entry": 1.0,
        "trend_length": 89,
        "peak_price": entry,
        "halal_verdict": "Halal ✓",
    }
    bot.state["open_trades"]["AAAUSDT"] = trade
    return trade


# إطارات إشارة: (إشارة شراء على آخر شمعة)
GOOD_CLOSES = [100.0] * 440 + [99.0, 105.0]      # ER ≈ 0.71 → مسموحة
CHOPPY_CLOSES = [100.0] * 428 + [101, 99] * 6 + [99.0, 101.0]  # ER ≈ 0.04 → مرفوضة
GOOD_BAR_OPEN = BASE_MS + 441 * HOUR_MS


# ───────────────────────── وحدة: دالة البوابة
def test_er_passes_on_straight_trend():
    from src.strategy import StrategySettings, entry_er_ok

    closes = [100 + i * 0.5 for i in range(30)]
    ok, er = entry_er_ok(pd.DataFrame({"close": closes}), StrategySettings())
    assert ok and er > 0.9


def test_er_rejects_choppy_market():
    from src.strategy import StrategySettings, entry_er_ok

    ok, er = entry_er_ok(pd.DataFrame({"close": CHOPPY_CLOSES}), StrategySettings())
    assert not ok and er < 0.30


def test_er_allows_when_data_flat_nan():
    from src.strategy import StrategySettings, entry_er_ok

    ok, er = entry_er_ok(pd.DataFrame({"close": [100.0] * 50}), StrategySettings())
    assert ok and np.isnan(er)


def test_er_gate_disabled_when_zero():
    from src.strategy import StrategySettings, entry_er_ok

    ok, _ = entry_er_ok(pd.DataFrame({"close": CHOPPY_CLOSES}), StrategySettings(entry_er_min=0.0))
    assert ok


def test_er_allows_on_short_frame():
    from src.strategy import StrategySettings, entry_er_ok

    ok, _ = entry_er_ok(pd.DataFrame({"close": CHOPPY_CLOSES[:10]}), StrategySettings())
    assert ok


def test_settings_gate_defaults():
    from src.strategy import StrategySettings

    s = StrategySettings()
    assert s.entry_er_min == 0.20  # مضبوط على 4H (2026-10-15)
    assert s.entry_er_len == 14
    assert s.entry_cooldown_hours == 96
    assert "بوابة ER" in s.describe()
    assert "تهدئة" in s.describe()


# ───────────────────────── تكامل: البوابة تمنع الدخول فعليًا
def test_good_signal_still_opens(tmp_path):
    bot = make_bot(tmp_path)
    bot._process_one_closed_hour({"AAAUSDT": make_frame(GOOD_CLOSES)}, GOOD_BAR_OPEN)
    assert "AAAUSDT" in bot.state["open_trades"]


def test_choppy_signal_rejected_with_event(tmp_path):
    bot = make_bot(tmp_path)
    bot._process_one_closed_hour({"AAAUSDT": make_frame(CHOPPY_CLOSES)}, GOOD_BAR_OPEN)
    assert "AAAUSDT" not in bot.state["open_trades"]
    assert any(e.get("type") == "entry_rejected_er" for e in bot.state.get("event_log", []))


def test_cooldown_blocks_new_entry(tmp_path):
    bot = make_bot(tmp_path)
    bot.state["entry_blocked_until"] = {"AAAUSDT": BASE_MS + 999 * HOUR_MS}
    bot._process_one_closed_hour({"AAAUSDT": make_frame(GOOD_CLOSES)}, GOOD_BAR_OPEN)
    assert "AAAUSDT" not in bot.state["open_trades"]
    assert any(e.get("type") == "entry_rejected_cooldown" for e in bot.state.get("event_log", []))


def test_cooldown_expired_allows_entry(tmp_path):
    bot = make_bot(tmp_path)
    bot.state["entry_blocked_until"] = {"AAAUSDT": GOOD_BAR_OPEN - 1}
    bot._process_one_closed_hour({"AAAUSDT": make_frame(GOOD_CLOSES)}, GOOD_BAR_OPEN)
    assert "AAAUSDT" in bot.state["open_trades"]


# ───────────────────────── التهدئة تُضبط عند الوقف فقط
def test_stop_loss_sets_cooldown_until_24h(tmp_path):
    bot = make_bot(tmp_path)
    trade = open_trade(bot)
    exit_ms = BASE_MS + 5 * HOUR_MS
    bot._record_trade_exit(trade, "loss", "stop_loss", exit_ms, 98.0)
    blocked = bot.state["entry_blocked_until"]["AAAUSDT"]
    assert blocked == exit_ms + bot.settings.entry_cooldown_hours * HOUR_MS


def test_signal_down_does_not_set_cooldown(tmp_path):
    bot = make_bot(tmp_path)
    trade = open_trade(bot)
    bot._record_trade_exit(trade, "partial", "signal_down", BASE_MS + 5 * HOUR_MS, 101.0)
    assert "entry_blocked_until" not in bot.state or "AAAUSDT" not in bot.state["entry_blocked_until"]


def test_all_targets_does_not_set_cooldown(tmp_path):
    bot = make_bot(tmp_path)
    trade = open_trade(bot)
    bot._record_trade_exit(trade, "win", "all_targets", BASE_MS + 5 * HOUR_MS, 115.0)
    assert "entry_blocked_until" not in bot.state or "AAAUSDT" not in bot.state["entry_blocked_until"]


# ───────────────────────── عدّاد التقرير اليومي
def test_daily_report_counts_rejected_signals(tmp_path):
    bot = make_bot(tmp_path)
    tz = ZoneInfo(bot.config.timezone_name)
    now_local = datetime(2026, 10, 13, 0, 5, tzinfo=tz)
    now_ms = int(now_local.timestamp() * 1000)
    ev_ms = int(datetime(2026, 10, 12, 12, 0, tzinfo=tz).timestamp() * 1000)
    bot.state["event_log"] = [
        {"type": "entry", "symbol": "AAAUSDT", "time_ms": ev_ms},
        {"type": "entry_rejected_er", "symbol": "AAAUSDT", "time_ms": ev_ms},
        {"type": "entry_rejected_er", "symbol": "BBBUSDT", "time_ms": ev_ms},
        {"type": "entry_rejected_cooldown", "symbol": "CCCUSDT", "time_ms": ev_ms},
    ]
    bot.send_daily_report_if_due(now_ms)
    text = bot.telegram.sent[-1]
    assert "📥 صفقات الدخول: 1" in text
    assert "كفاءة 2" in text
    assert "تهدئة 1" in text
