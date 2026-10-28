"""اختبارات دورة صفقة نظام القاع الكاملة: دخول → تحديث وقف → إغلاق (وقف/مهلة).

تشمل إثبات العزل: لا يُنشأ state.json ولا trades_log.csv (ملفات النظام الآخر).

تعمل بـ pytest مباشرة:  python -m pytest tests/ -q
"""
from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DAY_MS = 86_400_000
MINUTE_MS = 60_000
BASE_MS = 1_700_000_000_000


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []

    def send_message(self, text: str) -> None:
        self.sent.append(text)


class FakeBinance:
    def __init__(self, klines_1m: pd.DataFrame | None = None, prices: dict | None = None):
        self.klines_1m = klines_1m
        self.prices = prices or {}

    def get_klines_range(self, symbol, interval, start_time, end_time, limit=1000):
        return self.klines_1m

    def get_all_prices(self):
        return self.prices


def make_bot(tmp_path: Path):
    from qaa_bot import QaaSignalBot
    from src.config import AppConfig
    from src.qaa_strategy import QaaSettings
    from src.state import StateStore

    bot = QaaSignalBot.__new__(QaaSignalBot)
    bot.config = AppConfig(telegram_bot_token="t", telegram_chat_id="c")
    bot.telegram = FakeTelegram()
    bot.logger = logging.getLogger("test-qaa-flow")
    bot.settings = QaaSettings()
    bot.binance = FakeBinance()  # افتراضي؛ الاختبارات التي تحتاج بيانات تستبدله
    bot.interval = "1d"
    bot.interval_ms = DAY_MS
    bot.data_dir = str(tmp_path)
    bot.halal_dir = str(tmp_path / "halal")
    bot.store = StateStore(str(tmp_path / "state_qaa.json"))
    bot.state = bot.store.load()
    bot.price_ticks = {}
    bot.halal_verdicts = {}
    bot.symbols = []
    bot.last_symbols_refresh = 0.0
    bot.get_halal_verdict = lambda symbol: "✅ مباح"
    return bot


def make_trade(symbol="BTCUSDT", entry=100.0, pivot_stop=95.0, bar_open=BASE_MS - DAY_MS):
    dist = (entry - pivot_stop) / entry * 100.0
    return {
        "trade_id": f"{symbol}-{bar_open}",
        "symbol": symbol,
        "entry_price": entry,
        "stop_price": pivot_stop,
        "pivot_stop": pivot_stop,
        "target_price": entry + 2.0 * (entry - pivot_stop),
        "stop_dist_pct": dist,
        "size_suggest_pct": min(1.0 / dist * 100.0, 100.0),
        "entry_time": bar_open + DAY_MS - 1,
        "entry_bar_open_time": bar_open,
        "last_check_ms": bar_open + DAY_MS,
        "peak_price": entry,
        "last_stop_alert_price": pivot_stop,
        "stop_updates_count": 0,
        "halal_verdict": "✅ مباح",
    }


def make_1m(rows):
    """rows: قائمة (open, high, low, close_time)."""
    return pd.DataFrame({
        "open_time": [BASE_MS] * len(rows),
        "open": [float(r[0]) for r in rows],
        "high": [float(r[1]) for r in rows],
        "low": [float(r[2]) for r in rows],
        "close": [float(r[0]) for r in rows],
        "close_time": [int(r[3]) for r in rows],
    })


def valid_entry_df():
    """250 يومًا صاعدًا + قاع صارم — شروط الدخول كاملة على الشمعة الأخيرة."""
    n = 250
    closes = [100.0 + i for i in range(n)]
    opens = list(closes)
    opens[n - 1] = closes[n - 1] - 10.0
    highs = [max(o, c) + 1.0 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 1.0 for o, c in zip(opens, closes)]
    lows[n - 2] = closes[n - 2] - 60.0
    open_times = [BASE_MS + i * DAY_MS for i in range(n)]
    return pd.DataFrame({
        "open_time": open_times,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "close_time": [t + DAY_MS - 1 for t in open_times],
    })


def test_entry_message_details_and_zero_touch(tmp_path):
    bot = make_bot(tmp_path)
    trade = make_trade()
    bot.send_entry_message(trade)
    text = bot.telegram.sent[-1]
    assert "نظام القاع (يومي) • يومي حصري" in text
    assert "الدخول: 100" in text
    assert "الوقف: 95 (-5%) ← الهدف:" in text
    assert "عرض فقط" in text
    assert "الحجم: " in text and "المخاطرة: 1.0%" in text
    assert "الحكم الشرعي" in text
    # العزل: دفتر النظام منفصل وملفات النظام الآخر غير مُنشأة أبدًا
    assert (tmp_path / "trades_log_qaa.csv").exists()
    assert not (tmp_path / "trades_log.csv").exists()
    assert not (tmp_path / "state.json").exists()


def test_stop_update_message_content(tmp_path):
    bot = make_bot(tmp_path)
    trade = make_trade()
    bot.send_stop_update_message(trade, 95.0, 104.5, BASE_MS + DAY_MS, current_price=108.0)
    text = bot.telegram.sent[-1]
    assert "📈 وقف متحرك — BTCUSDT" in text
    assert "الدخول: 100 | السعر الآن: 108 (+8%)" in text
    assert "الوقف: 95 ← 104.5 (+10%) | ربح مضمون: +4.5%" in text
    assert "المسافة: 3.3% فوق الوقف الجديد | تحديث رقم: 1" in text
    assert "⬆️ ارفع وقفك إلى 104.5 على منصتك" in text
    assert "الحكم الشرعي" in text

    # بلا سعر (مسار احتياطي) — السطران يُomitان ولا تنكسر الرسالة
    bot.telegram.sent.clear()
    bot.send_stop_update_message(trade, 95.0, 104.5, BASE_MS + DAY_MS)
    no_price = bot.telegram.sent[-1]
    assert "السعر الآن" not in no_price
    assert "المسافة" not in no_price
    assert "تحديث رقم: 1" in no_price


def test_intrabar_trailing_alert_gated_by_step(tmp_path):
    """القيم الصغيرة تُسجَّل صامتة؛ الرسائل فقط عند تجاوز عتبة 1%."""
    bot = make_bot(tmp_path)
    trade = make_trade()
    bot.state["open_trades"]["BTCUSDT"] = trade
    rows = [
        (100.0, 110.0, 99.0, BASE_MS + MINUTE_MS),        # قمة 110 → لا رفع بعد
        (110.0, 120.0, 109.0, BASE_MS + 2 * MINUTE_MS),   # رفع إلى 101.2 ≥ 95×1.01 → رسالة
        (120.0, 120.5, 119.0, BASE_MS + 3 * MINUTE_MS),   # رفع إلى 110.4 ≥ 102.2 → رسالة
        (120.5, 121.0, 120.0, BASE_MS + 4 * MINUTE_MS),   # رفع إلى 110.86 < 111.5 → صامت
    ]
    bot.binance = FakeBinance(make_1m(rows))
    bot.monitor_open_trades_intrabar(BASE_MS + 5 * MINUTE_MS)

    alerts = [t for t in bot.telegram.sent if "وقف متحرك" in t]
    assert len(alerts) == 2
    live = bot.state["open_trades"]["BTCUSDT"]
    assert abs(live["stop_price"] - 110.86) < 1e-6      # القيمة تُحدَّث دائمًا
    assert live["stop_updates_count"] == 2
    assert abs(live["last_stop_alert_price"] - 110.4) < 1e-6
    assert abs(live["peak_price"] - 121.0) < 1e-9


def test_intrabar_stop_hit_closes_trade(tmp_path):
    bot = make_bot(tmp_path)
    trade = make_trade()
    bot.state["open_trades"]["BTCUSDT"] = trade
    rows = [
        (100.0, 105.0, 96.0, BASE_MS + MINUTE_MS),   # قمة 105 → رفع غدًا إلى 96.6
        (100.0, 101.0, 94.5, BASE_MS + 2 * MINUTE_MS),  # لمس 96.6 → إغلاق
    ]
    bot.binance = FakeBinance(make_1m(rows))
    bot.monitor_open_trades_intrabar(BASE_MS + 5 * MINUTE_MS)

    assert "BTCUSDT" not in bot.state["open_trades"]
    close_msgs = [t for t in bot.telegram.sent if "إغلاق" in t]
    assert len(close_msgs) == 1
    text = close_msgs[0]
    assert "لمس الوقف المتحرك" in text       # الوقف رُفع فوق القاع (96.6 > 95)
    assert "← الخروج: 96.6" in text
    assert "إغلاق خاسر" in text and "الصافي: -3.60%" in text
    # يوم الخروج مسجّل — يمنع إعادة الدخول في نفس اليوم
    assert bot.state["last_exit_bar_time"]["BTCUSDT"] == (BASE_MS + 2 * MINUTE_MS) // DAY_MS * DAY_MS


def test_timeout_exit_on_closed_day(tmp_path):
    bot = make_bot(tmp_path)
    trade = make_trade()
    bot.state["open_trades"]["BTCUSDT"] = trade
    last_closed = int(trade["entry_bar_open_time"]) + 40 * DAY_MS
    df = pd.DataFrame({
        "open_time": [last_closed],
        "open": [101.0], "high": [110.0], "low": [100.0], "close": [108.0],
        "close_time": [last_closed + DAY_MS - 1],
    })
    bot._process_one_closed_day({"BTCUSDT": df}, last_closed)

    assert "BTCUSDT" not in bot.state["open_trades"]
    close_msgs = [t for t in bot.telegram.sent if "إغلاق" in t]
    assert len(close_msgs) == 1
    text = close_msgs[0]
    assert "انتهت المهلة (40 يومًا)" in text
    assert "الصافي: +7.80%" in text   # 108/100 - 0.2% عمولة
    assert bot.state["last_exit_bar_time"]["BTCUSDT"] == last_closed


def test_exit_updates_journal_row_after_real_entry(tmp_path):
    """دخول حقيقي (صف مسجّل في الدفتر) ثم إغلاق لاحقًا — يُحدَّث الصف بكل الحقول
    (peak_gain_pct وغيره) دون انفجار DictWriter. Regression: أوقفت الدورة كاملة في الإنتاج."""
    bot = make_bot(tmp_path)
    bot.symbols = ["BTCUSDT"]
    df = valid_entry_df()
    last_closed = int(df.iloc[-1]["open_time"])
    bot._process_one_closed_day({"BTCUSDT": df}, last_closed)
    trade = bot.state["open_trades"]["BTCUSDT"]
    # شمعة الدخول: إغلاق 349 وقمة 350 → أول رفع للوقف: 350 × 0.92 = 322
    entry_time = int(trade["entry_time"])
    rows = [(340.0, 345.0, 320.0, entry_time + 2 * MINUTE_MS)]  # لمس 322 → إغلاق عند الوقف
    bot.binance = FakeBinance(make_1m(rows))
    bot.monitor_open_trades_intrabar(entry_time + 5 * MINUTE_MS)

    assert "BTCUSDT" not in bot.state["open_trades"]
    with (tmp_path / "trades_log_qaa.csv").open(newline="", encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    assert row["exit_reason"] != "" and row["exit_price"] != ""
    assert row["peak_gain_pct"] != ""  # العمود الجديد كان يُسقط الكتابة كلها


def test_entry_flow_opens_trade(tmp_path):
    bot = make_bot(tmp_path)
    bot.symbols = ["BTCUSDT"]
    df = valid_entry_df()
    last_closed = int(df.iloc[-1]["open_time"])
    bot._process_one_closed_day({"BTCUSDT": df}, last_closed)

    live = bot.state["open_trades"]["BTCUSDT"]
    assert live["entry_price"] == 349.0
    assert live["pivot_stop"] == 288.0
    assert live["target_price"] == 471.0
    assert bot.state["last_entry_bar_time"]["BTCUSDT"] == last_closed
    assert any("إشارة دخول" in t for t in bot.telegram.sent)
    with (tmp_path / "trades_log_qaa.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1 and rows[0]["symbol"] == "BTCUSDT"


def test_entry_blocked_on_exit_day(tmp_path):
    bot = make_bot(tmp_path)
    bot.symbols = ["BTCUSDT"]
    df = valid_entry_df()
    last_closed = int(df.iloc[-1]["open_time"])
    bot.state["last_exit_bar_time"]["BTCUSDT"] = last_closed  # خرج في هذا اليوم نفسه
    bot._process_one_closed_day({"BTCUSDT": df}, last_closed)

    assert "BTCUSDT" not in bot.state["open_trades"]
    assert bot.telegram.sent == []
    assert not (tmp_path / "trades_log_qaa.csv").exists()
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "trades_log.csv").exists()


def test_entry_flow_survives_telegram_failure(tmp_path):
    """فشل تيليجرام (HTTP400) لا يقتل الدورة: الصفقة تُحفظ في الحالة والدفتر."""
    class BoomTelegram:
        def send_message(self, text: str) -> None:
            raise RuntimeError("telegram 400")

    bot = make_bot(tmp_path)
    bot.telegram = BoomTelegram()
    bot.symbols = ["BTCUSDT"]
    df = valid_entry_df()
    last_closed = int(df.iloc[-1]["open_time"])
    bot._process_one_closed_day({"BTCUSDT": df}, last_closed)  # لا يُسقَط استثناء

    assert "BTCUSDT" in bot.state["open_trades"]
    with (tmp_path / "trades_log_qaa.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1 and rows[0]["symbol"] == "BTCUSDT"
