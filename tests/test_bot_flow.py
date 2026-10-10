"""اختبارات دورة الصفقة الكاملة: دخول → أهداف → إغلاق (بيع / وقف / تحقق الأهداف الثلاثة).

تعمل بـ pytest مباشرة:  python -m pytest tests/ -q
"""
from __future__ import annotations

import csv
import logging
import sys
import tempfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HOUR_MS = 3_600_000
MINUTE_MS = 60_000
BASE_MS = 1_700_000_000_000


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []

    def send_message(self, text: str) -> None:
        self.sent.append(text)


def make_bot(tmp_path: Path):
    from app import SpotSignalBot

    bot = SpotSignalBot.__new__(SpotSignalBot)
    bot.data_dir = str(tmp_path)
    bot.telegram = FakeTelegram()
    bot.logger = logging.getLogger("test-flow")
    from src.config import AppConfig

    bot.config = AppConfig(telegram_bot_token="t", telegram_chat_id="c")
    from src.state import StateStore

    bot.store = StateStore(str(tmp_path / "state.json"))
    bot.state = bot.store.load()
    from src.strategy import StrategySettings

    bot.settings = StrategySettings()
    bot.symbols = ["BTCUSDT"]
    bot.halal_verdicts = {}
    bot.get_halal_verdict = lambda symbol: "希尔ًا (Halal)"
    return bot


def open_trade(bot, entry=100.0, stop=98.0, targets=(105.0, 110.0, 115.0), bar_open=BASE_MS):
    trade = {
        "trade_id": f"BTCUSDT-{bar_open}",
        "symbol": "BTCUSDT",
        "entry_price": entry,
        "stop_price": stop,
        "targets": list(targets),
        "hit": [False, False, False],
        "entry_time": bar_open + HOUR_MS - 1,
        "entry_bar_open_time": bar_open,
        "last_target_check_ms": bar_open + HOUR_MS,
        "atr_at_entry": 1.0,
        "trend_length": 10,
        "peak_price": entry,
        "halal_verdict": "希尔ًا (Halal)",
    }
    bot.state["open_trades"][bot.symbols[0]] = trade
    bot.store.save(bot.state)
    return trade


def test_entry_message_contains_targets_and_stop(tmp_path):
    bot = make_bot(tmp_path)
    trade = open_trade(bot)
    bot.send_entry_message(trade)
    text = bot.telegram.sent[0]
    assert "BTCUSDT" in text
    assert "1H" in text
    assert "الهدف 1" in text and "الهدف 2" in text and "الهدف 3" in text
    assert "وقف الخسارة" in text
    assert "الحكم الشرعي" in text
    # دفتر الصفقات يحوي صفقة مفتوحة
    rows = list(csv.DictReader((tmp_path / "trades_log.csv").open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"


def test_target_hit_message_does_not_close(tmp_path):
    bot = make_bot(tmp_path)
    trade = open_trade(bot)
    bot.send_entry_message(trade)
    trade["hit"][0] = True
    bot.send_target_hit_message(trade, 1, 105.0, trade["entry_time"] + 2 * HOUR_MS)
    assert len(bot.telegram.sent) == 2
    assert "تحقق الهدف 1 من 3" in bot.telegram.sent[1]
    assert "الأهداف المحققة: 1 من 3" in bot.telegram.sent[1]
    # الصفقة باقية
    assert bot.symbols[0] in bot.state["open_trades"]


def test_close_win_reports_targets_hit(tmp_path):
    bot = make_bot(tmp_path)
    trade = open_trade(bot)
    bot.send_entry_message(trade)
    trade["hit"] = [True, True, False]
    bot.send_close_message(trade, 110.0, trade["entry_time"] + 10 * HOUR_MS, "signal_down", "إشارة بيع")
    text = bot.telegram.sent[1]
    assert "انتهت الصفقة" in text and "رابحة" in text
    assert "الأهداف المحققة: 2 من 3" in text
    assert "سبب الإغلاق" in text
    # السجل محدَّث بالنتيجة
    rows = list(csv.DictReader((tmp_path / "trades_log.csv").open(encoding="utf-8")))
    assert rows[0]["outcome"] == "win"
    assert rows[0]["targets_hit_count"] == "2"
    assert float(rows[0]["net_return_pct"]) > 0


def test_close_loss_reports_negative(tmp_path):
    bot = make_bot(tmp_path)
    trade = open_trade(bot, entry=100.0, stop=99.0, targets=(105.0, 110.0, 115.0))
    bot.send_entry_message(trade)
    bot.send_close_message(trade, 97.0, trade["entry_time"] + 3 * HOUR_MS, "stop_loss", "لمس وقف الخسارة")
    text = bot.telegram.sent[1]
    assert "خاسرة" in text
    rows = list(csv.DictReader((tmp_path / "trades_log.csv").open(encoding="utf-8")))
    assert rows[0]["outcome"] == "loss"
    assert float(rows[0]["net_return_pct"]) < 0


def test_close_with_target_hit_is_neutral_not_loss(tmp_path):
    """صفقة تحققت أهدافًا ثم أُغلقت بنتيجة سالبة → محايدة (partial) لا خاسرة."""
    bot = make_bot(tmp_path)
    trade = open_trade(bot)
    bot.send_entry_message(trade)
    trade["hit"] = [True, True, False]
    # إغلاق بإشارة البيع عند سعر سالب رغم تحقق هدفين
    bot.send_close_message(trade, 99.0, trade["entry_time"] + 8 * HOUR_MS, "signal_down", "إشارة بيع")

    text = bot.telegram.sent[1]
    assert "انتهت الصفقة — حققت هدفًا فأكثر" in text
    assert "انتهت الصفقة — خاسرة" not in text
    assert "محايدة" in text
    assert "الأهداف المحققة: 2 من 3" in text

    rows = list(csv.DictReader((tmp_path / "trades_log.csv").open(encoding="utf-8")))
    assert rows[0]["outcome"] == "partial"
    assert rows[0]["exit_reason"] == "signal_down"
    assert float(rows[0]["net_return_pct"]) < 0
    # الحدث محايد وليس خسارة
    assert bot.state["event_log"][-1]["type"] == "exit_neutral"


def test_close_without_targets_negative_is_loss(tmp_path):
    """صفقة بلا أهداف محققة ونتيجة سالبة → تبقى خاسرة كما كانت."""
    bot = make_bot(tmp_path)
    trade = open_trade(bot)
    bot.send_entry_message(trade)
    trade["hit"] = [False, False, False]
    bot.send_close_message(trade, 99.0, trade["entry_time"] + 4 * HOUR_MS, "signal_down", "إشارة بيع")
    text = bot.telegram.sent[1]
    assert "انتهت الصفقة — خاسرة" in text
    rows = list(csv.DictReader((tmp_path / "trades_log.csv").open(encoding="utf-8")))
    assert rows[0]["outcome"] == "loss"
    assert bot.state["event_log"][-1]["type"] == "exit_loss"


def test_intrabar_stop_closes_trade(tmp_path):
    bot = make_bot(tmp_path)
    trade = open_trade(bot, entry=100.0, stop=99.0)

    class FakeFrame(pd.DataFrame):
        pass

    # شمعة 1m تلمس الوقف
    one_min = pd.DataFrame({
        "open_time": [trade["entry_time"] + 1],
        "open": [100.0], "high": [100.5], "low": [98.9], "close": [99.2], "volume": [10.0],
        "close_time": [trade["entry_time"] + MINUTE_MS],
    })

    class FakeBinance:
        def get_klines_range(self, symbol, interval, start_time, end_time, limit=1000):
            return one_min

    bot.binance = FakeBinance()
    bot.monitor_open_trades_intrabar(trade["entry_time"] + 5 * MINUTE_MS)
    assert bot.symbols[0] not in bot.state["open_trades"]
    assert any("انتهت الصفقة" in m for m in bot.telegram.sent)


def test_intrabar_target_sends_message_and_keeps_trade(tmp_path):
    bot = make_bot(tmp_path)
    trade = open_trade(bot, entry=100.0, stop=98.0, targets=(103.0, 110.0, 115.0))
    one_min = pd.DataFrame({
        "open_time": [trade["entry_time"] + 1],
        "open": [100.0], "high": [103.5], "low": [100.0], "close": [103.2], "volume": [10.0],
        "close_time": [trade["entry_time"] + MINUTE_MS],
    })

    class FakeBinance:
        def get_klines_range(self, symbol, interval, start_time, end_time, limit=1000):
            return one_min

    bot.binance = FakeBinance()
    bot.monitor_open_trades_intrabar(trade["entry_time"] + 5 * MINUTE_MS)
    assert bot.symbols[0] in bot.state["open_trades"]
    assert any("تحقق الهدف 1 من 3" in m for m in bot.telegram.sent)
    assert bot.state["open_trades"]["BTCUSDT"]["hit"][0] is True


def test_intrabar_third_target_closes_trade_as_win(tmp_path):
    bot = make_bot(tmp_path)
    trade = open_trade(bot, entry=100.0, stop=98.0, targets=(103.0, 106.0, 110.0))
    bot.send_entry_message(trade)

    # شمعة 1m تلمس الأهداف الثلاثة مع بقاء الوقف بعيدًا
    one_min = pd.DataFrame({
        "open_time": [trade["entry_time"] + 1],
        "open": [100.0], "high": [110.5], "low": [99.5], "close": [110.2], "volume": [10.0],
        "close_time": [trade["entry_time"] + MINUTE_MS],
    })

    class FakeBinance:
        def get_klines_range(self, symbol, interval, start_time, end_time, limit=1000):
            return one_min

    bot.binance = FakeBinance()
    bot.monitor_open_trades_intrabar(trade["entry_time"] + 5 * MINUTE_MS)

    # الصفقة أُغلقت فور تحقق الهدف الثالث
    assert bot.symbols[0] not in bot.state["open_trades"]
    # ثلاث رسائل تحقق ثم رسالة إغلاق واحدة
    assert sum(1 for m in bot.telegram.sent if "🎯 تحقق الهدف" in m) == 3
    close_msgs = [m for m in bot.telegram.sent if "تحققت الأهداف الثلاثة" in m]
    assert len(close_msgs) == 1
    text = close_msgs[0]
    assert "صفقة ناجحة" in text
    assert "الأهداف المحققة: 3 من 3" in text
    assert "مدة التحقق" in text
    assert "النسبة الفعلية" in text and "الصافية بعد العمولة" in text
    assert "الهدف 1:" in text and "الهدف 2:" in text and "الهدف 3:" in text
    assert "الحكم الشرعي" in text
    # الدفتر: ناجحة بـ all_targets وثلاثة أهداف محققة
    rows = list(csv.DictReader((tmp_path / "trades_log.csv").open(encoding="utf-8")))
    assert rows[-1]["outcome"] == "win"
    assert rows[-1]["exit_reason"] == "all_targets"
    assert rows[-1]["targets_hit_count"] == "3"
    assert float(rows[-1]["net_return_pct"]) > 0


def test_intrabar_second_target_keeps_trade_open(tmp_path):
    bot = make_bot(tmp_path)
    trade = open_trade(bot, entry=100.0, stop=98.0, targets=(103.0, 106.0, 110.0))
    bot.send_entry_message(trade)

    # شمعة تلمس الهدفين الأول والثاني فقط
    one_min = pd.DataFrame({
        "open_time": [trade["entry_time"] + 1],
        "open": [100.0], "high": [106.4], "low": [99.5], "close": [106.0], "volume": [10.0],
        "close_time": [trade["entry_time"] + MINUTE_MS],
    })

    class FakeBinance:
        def get_klines_range(self, symbol, interval, start_time, end_time, limit=1000):
            return one_min

    bot.binance = FakeBinance()
    bot.monitor_open_trades_intrabar(trade["entry_time"] + 5 * MINUTE_MS)

    # باقية مفتوحة بلا رسالة إغلاق
    assert bot.symbols[0] in bot.state["open_trades"]
    assert not any("انتهت الصفقة" in m for m in bot.telegram.sent)
    assert bot.state["open_trades"]["BTCUSDT"]["hit"] == [True, True, False]
