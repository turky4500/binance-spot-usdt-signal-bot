"""اختبارات دورة إشارة ZigZag++ الكاملة: قاع → دخول → نجاح/خسارة.

تشمل إثبات العزل: لا تُنشأ ملفات state.json أو trades_log.csv أو state_qaa.json.

python -m pytest tests/ -q
"""
from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.zigzag import HOUR_MS, MINUTE_MS  # noqa: E402

BASE_MS = 1_699_999_200_000


class FakeTelegram:
    def __init__(self, fail: bool = False):
        self.sent: list[str] = []
        self.fail = fail

    def send_message(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("telegram 400")
        self.sent.append(text)


class FakeBinance:
    def __init__(self, klines_1h=None, klines_1m=None, server_time=None):
        self.klines_1h = klines_1h or {}
        self.klines_1m = klines_1m if klines_1m is not None else pd.DataFrame()
        self.server_time = server_time

    def get_server_time(self):
        return int(self.server_time)

    def get_klines_for_symbols(self, symbols, interval="1h", limit=300):
        return self.klines_1h

    def get_klines_range(self, symbol, interval, start_time, end_time, limit=1000):
        return self.klines_1m


def make_bot(tmp_path: Path, telegram=None):
    from src.config import AppConfig
    from src.state import StateStore
    from src.zigzag import ZigZagSettings
    from zigzag_bot import ZigZagSignalBot

    bot = ZigZagSignalBot.__new__(ZigZagSignalBot)
    bot.config = AppConfig(telegram_bot_token="t", telegram_chat_id="c")
    bot.telegram = telegram or FakeTelegram()
    bot.logger = logging.getLogger("test-zigzag-flow")
    bot.settings = ZigZagSettings()
    # فلتر EMA200 له اختبارات مستقلة (tests/test_zigzag_ema_batching.py) تحتاج
    # 200 شمعة تاريخية؛ هنا نعطّله ليبقى التركيز على مسار القاع→الدخول→الخروج.
    bot.settings.ema_filter = 0
    bot.interval = "1h"
    bot.interval_ms = HOUR_MS
    bot.data_dir = str(tmp_path)
    bot.halal_dir = str(tmp_path / "halal")
    bot.store = StateStore(str(tmp_path / "state_zigzag.json"))
    bot.state = bot.store.load()
    bot.state.setdefault("last_bottom_bar_time", {})
    bot.state.setdefault("open_signals", {})
    bot.price_ticks = {}
    bot.halal_verdicts = {}
    bot.symbols = []
    bot.last_symbols_refresh = 0.0
    bot.get_halal_verdict = lambda symbol: "✅ مباح"
    return bot


def make_hour_df(rows):
    n = len(rows)
    return pd.DataFrame({
        "open_time": [BASE_MS + i * HOUR_MS for i in range(n)],
        "close_time": [BASE_MS + (i + 1) * HOUR_MS - 1 for i in range(n)],
        "open": [float(r[0]) for r in rows],
        "high": [float(r[1]) for r in rows],
        "low": [float(r[2]) for r in rows],
        "close": [float(r[3]) for r in rows],
    })


def entry_df(next_candle=(100.0, 103.0, 99.0, 102.0)):
    rows = [(100.0, 101.0, 99.0, 100.0) for _ in range(18)]
    rows[15] = (100.0, 101.0, 90.0, 100.0)   # القاع
    rows[16] = next_candle                    # الشمعة التالية
    rows[17] = (102.0, 103.0, 101.0, 102.0)
    return make_hour_df(rows)


def server_time_after_entry():
    return BASE_MS + 18 * HOUR_MS


def make_signal(symbol="BTCUSDT", entry=100.0, target=102.0, stop=90.0, entry_bar=16):
    return {
        "trade_id": f"{symbol}-{BASE_MS + entry_bar * HOUR_MS}",
        "symbol": symbol,
        "entry_price": entry,
        "target_price": target,
        "stop_price": stop,
        "bottom_low": stop,
        "bottom_bar_open_time": BASE_MS + (entry_bar - 1) * HOUR_MS,
        "entry_bar_open_time": BASE_MS + entry_bar * HOUR_MS,
        "entry_time": BASE_MS + (entry_bar + 1) * HOUR_MS - 1,
        "last_check_ms": BASE_MS + (entry_bar + 1) * HOUR_MS,
        "max_price": entry,
        "halal_verdict": "✅ مباح",
    }


def make_1m(rows):
    """rows: (open, high, low, close, close_time)."""
    return pd.DataFrame({
        "open_time": [int(r[4]) - MINUTE_MS + 1 for r in rows],
        "open": [float(r[0]) for r in rows],
        "high": [float(r[1]) for r in rows],
        "low": [float(r[2]) for r in rows],
        "close": [float(r[3]) for r in rows],
        "close_time": [int(r[4]) for r in rows],
    })


# ─────────────────────────────────────────── الدخول
def test_entry_flow_opens_signal(tmp_path):
    bot = make_bot(tmp_path)
    bot.symbols = ["BTCUSDT"]
    df = entry_df()
    bot.process_new_bottoms({"BTCUSDT": df}, server_time_after_entry())

    signal = bot.state["open_signals"]["BTCUSDT"]
    assert signal["entry_price"] == 102.0
    assert signal["stop_price"] == 90.0
    assert abs(signal["target_price"] - 104.04) < 1e-9
    assert bot.state["last_bottom_bar_time"]["BTCUSDT"] == BASE_MS + 15 * HOUR_MS
    assert any("📥 إشارة دخول" in t for t in bot.telegram.sent)

    with (tmp_path / "trades_log_zigzag.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1 and rows[0]["symbol"] == "BTCUSDT"


def test_entry_message_content(tmp_path):
    bot = make_bot(tmp_path)
    bot.symbols = ["BTCUSDT"]
    bot.process_new_bottoms({"BTCUSDT": entry_df()}, server_time_after_entry())
    text = bot.telegram.sent[-1]
    assert "ZigZag++ (ساعة) • فريم 1H" in text
    assert "الدخول: 102" in text
    assert "الهدف: 104.04 (+2%)" in text
    assert "الوقف: 90 — إغلاق تحت قاع الشمعة" in text
    assert "شمعة القاع:" in text and "شمعة الدخول:" in text
    assert "الحكم الشرعي: ✅ مباح" in text


def test_red_next_candle_cancels_bottom(tmp_path):
    bot = make_bot(tmp_path)
    bot.symbols = ["BTCUSDT"]
    bot.process_new_bottoms({"BTCUSDT": entry_df((102.0, 103.0, 99.0, 100.0))}, server_time_after_entry())
    assert bot.state["open_signals"] == {}
    assert bot.state["last_bottom_bar_time"]["BTCUSDT"] == BASE_MS + 15 * HOUR_MS
    assert bot.telegram.sent == []


def test_doji_next_candle_cancels_bottom(tmp_path):
    bot = make_bot(tmp_path)
    bot.symbols = ["BTCUSDT"]
    bot.process_new_bottoms({"BTCUSDT": entry_df((100.0, 101.0, 99.0, 100.02))}, server_time_after_entry())
    assert bot.state["open_signals"] == {}
    assert bot.telegram.sent == []


def test_pending_bottom_waits_for_next_candle(tmp_path):
    """القاع على آخر شمعة مغلقة: لا إشارة حتى تُغلق الشمعة التالية."""
    bot = make_bot(tmp_path)
    bot.symbols = ["BTCUSDT"]
    rows = [(100.0, 101.0, 99.0, 100.0) for _ in range(18)]
    rows[17] = (100.0, 101.0, 90.0, 100.0)   # القاع = آخر شمعة
    df = make_hour_df(rows)
    bot.process_new_bottoms({"BTCUSDT": df}, server_time_after_entry())
    assert bot.state["open_signals"] == {}
    assert "BTCUSDT" not in bot.state["last_bottom_bar_time"]
    assert bot.telegram.sent == []


def test_stale_signal_is_skipped(tmp_path):
    bot = make_bot(tmp_path)
    bot.symbols = ["BTCUSDT"]
    df = entry_df()
    bot.process_new_bottoms({"BTCUSDT": df}, server_time_after_entry() + 5 * HOUR_MS)
    assert bot.state["open_signals"] == {}
    assert bot.telegram.sent == []


def test_one_signal_per_symbol(tmp_path):
    bot = make_bot(tmp_path)
    bot.symbols = ["BTCUSDT"]
    bot.state["open_signals"]["BTCUSDT"] = make_signal()
    bot.process_new_bottoms({"BTCUSDT": entry_df()}, server_time_after_entry())
    assert list(bot.state["open_signals"]) == ["BTCUSDT"]
    assert bot.state["last_bottom_bar_time"]["BTCUSDT"] == BASE_MS + 15 * HOUR_MS
    assert bot.telegram.sent == []


# ─────────────────────────────────────────── النجاح
def test_success_on_target_touch(tmp_path):
    bot = make_bot(tmp_path)
    signal = make_signal()
    bot.state["open_signals"]["BTCUSDT"] = signal
    bot.sync_open_signals_to_journal()
    bot.binance = FakeBinance(klines_1m=make_1m([
        (100.0, 102.5, 99.0, 101.0, BASE_MS + 17 * HOUR_MS + MINUTE_MS - 1),
    ]))
    bot.monitor_open_signals(BASE_MS + 17 * HOUR_MS + 2 * MINUTE_MS, {})

    assert bot.state["open_signals"] == {}
    text = bot.telegram.sent[-1]
    assert "🎯 نجاح — BTCUSDT" in text
    assert "الربح: +2%" in text
    with (tmp_path / "trades_log_zigzag.csv").open(newline="", encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    assert row["outcome"] == "win"
    assert row["exit_reason"] == "target"
    assert float(row["exit_price"]) == 102.0


def test_target_wins_over_hour_close(tmp_path):
    bot = make_bot(tmp_path)
    bot.state["open_signals"]["BTCUSDT"] = make_signal()
    bot.sync_open_signals_to_journal()
    hour_end = BASE_MS + 18 * HOUR_MS - 1
    bot.binance = FakeBinance(klines_1m=make_1m([
        (100.0, 102.5, 88.0, 89.0, hour_end),
    ]))
    bot.monitor_open_signals(hour_end + 1, {})

    assert bot.state["open_signals"] == {}
    assert "🎯 نجاح" in bot.telegram.sent[-1]


# ─────────────────────────────────────────── الخسارة
def test_loss_on_hour_close_below_bottom(tmp_path):
    bot = make_bot(tmp_path)
    bot.state["open_signals"]["BTCUSDT"] = make_signal()
    bot.sync_open_signals_to_journal()
    hour_end = BASE_MS + 18 * HOUR_MS - 1
    bot.binance = FakeBinance(klines_1m=make_1m([
        (100.0, 101.0, 88.0, 89.0, hour_end),
    ]))
    bot.monitor_open_signals(hour_end + 1, {})

    assert bot.state["open_signals"] == {}
    text = bot.telegram.sent[-1]
    assert "🛑 خسارة — BTCUSDT" in text
    assert "سبب الخروج: إغلاق شمعة تحت قاع الإشارة" in text
    with (tmp_path / "trades_log_zigzag.csv").open(newline="", encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    assert row["outcome"] == "loss"
    assert row["exit_reason"] == "close_below_bottom"


def test_intra_hour_dip_does_not_close(tmp_path):
    bot = make_bot(tmp_path)
    bot.state["open_signals"]["BTCUSDT"] = make_signal()
    bot.binance = FakeBinance(klines_1m=make_1m([
        (100.0, 101.0, 88.0, 89.0, BASE_MS + 17 * HOUR_MS + MINUTE_MS - 1),
    ]))
    bot.monitor_open_signals(BASE_MS + 17 * HOUR_MS + 2 * MINUTE_MS, {})
    assert "BTCUSDT" in bot.state["open_signals"]
    assert bot.telegram.sent == []


# ─────────────────────────────────────────── السلامة والعزل
def test_telegram_failure_does_not_lose_signal(tmp_path):
    bot = make_bot(tmp_path, telegram=FakeTelegram(fail=True))
    bot.symbols = ["BTCUSDT"]
    bot.process_new_bottoms({"BTCUSDT": entry_df()}, server_time_after_entry())

    assert "BTCUSDT" in bot.state["open_signals"]
    with (tmp_path / "trades_log_zigzag.csv").open(newline="", encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == 1


def test_isolation_from_other_systems(tmp_path):
    bot = make_bot(tmp_path)
    bot.symbols = ["BTCUSDT"]
    bot.process_new_bottoms({"BTCUSDT": entry_df()}, server_time_after_entry())

    assert (tmp_path / "state_zigzag.json").exists()
    assert (tmp_path / "trades_log_zigzag.csv").exists()
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "trades_log.csv").exists()
    assert not (tmp_path / "state_qaa.json").exists()
    assert not (tmp_path / "trades_log_qaa.csv").exists()
