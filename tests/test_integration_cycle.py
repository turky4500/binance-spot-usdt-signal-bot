"""اختبار تكامل: دورة شمعة مغلقة كاملة (دخول → أهداف → إغلاق بالإشارة أو بتحقق الثلاثة)."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

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


def build(tmp_path: Path):
    from app import SpotSignalBot
    from src.config import AppConfig
    from src.state import StateStore
    from src.strategy import StrategySettings

    bot = SpotSignalBot.__new__(SpotSignalBot)
    bot.data_dir = str(tmp_path)
    bot.telegram = FakeTelegram()
    bot.logger = logging.getLogger("smoke")
    bot.config = AppConfig(telegram_bot_token="t", telegram_chat_id="c")
    bot.store = StateStore(str(tmp_path / "state.json"))
    bot.state = bot.store.load()
    bot.settings = StrategySettings()
    bot.symbols = ["AAAUSDT"]
    bot.halal_verdicts = {}
    bot.get_halal_verdict = lambda symbol: "Halal ✓"
    return bot


def test_full_cycle_entry_then_all_targets_win(tmp_path):
    bot = build(tmp_path)
    # 442 شمعة: ثابت → هبوط (trend=false) → اندفاع (signal_up) على شمعة الدخول
    closes = [100.0] * 440 + [99.0, 105.0]
    highs = [100.2] * 440 + [99.2, 105.2]
    lows = [99.8] * 440 + [98.8, 104.8]
    entry_bar_open = BASE_MS + 441 * HOUR_MS

    bot._process_one_closed_hour({"AAAUSDT": make_frame(closes, highs, lows)}, entry_bar_open)

    # ── الدخول وقع ──
    assert "AAAUSDT" in bot.state["open_trades"], "لم تُفتح الصفقة"
    trade = bot.state["open_trades"]["AAAUSDT"]
    assert trade["entry_price"] == 105.0
    assert trade["stop_price"] < 105.0
    t1, t2, t3 = trade["targets"]
    assert t1 < t2 < t3
    assert any("إشارة شراء" in m for m in bot.telegram.sent)

    def push(i, high, low, close):
        closes.append(close)
        highs.append(high)
        lows.append(low)
        bot._process_one_closed_hour(
            {"AAAUSDT": make_frame(closes, highs, lows)},
            BASE_MS + (441 + i) * HOUR_MS,
        )

    # ── شمعة تلمس الهدف الأول فقط: تبقى مفتوحة ──
    push(1, t1 + (t2 - t1) / 2, 101.0, 105.5)
    assert "AAAUSDT" in bot.state["open_trades"], "أُغلقت مبكرًا عند الهدف الأول"
    assert bot.state["open_trades"]["AAAUSDT"]["hit"] == [True, False, False]

    # ── شمعة تلمس الهدف الثاني: تبقى مفتوحة ──
    push(2, t2 + (t3 - t2) / 2, 101.0, 106.0)
    assert "AAAUSDT" in bot.state["open_trades"], "أُغلقت مبكرًا عند الهدف الثاني"
    assert bot.state["open_trades"]["AAAUSDT"]["hit"] == [True, True, False]

    # ── شمعة تلمس الهدف الثالث: إغلاق تلقائي كصفقة ناجحة ──
    push(3, t3 + 0.5, 101.0, 107.0)
    assert "AAAUSDT" not in bot.state["open_trades"], "لم تُغلق الصفقة عند تحقق الهدف الثالث"

    # ── الرسائل: ثلاث تحقق ثم إغلاق واحد ──
    assert sum(1 for m in bot.telegram.sent if "🎯 تحقق الهدف" in m) == 3
    close_msgs = [m for m in bot.telegram.sent if "انتهت الصفقة" in m]
    assert len(close_msgs) == 1
    assert "تحققت الأهداف الثلاثة" in close_msgs[0]
    assert "صفقة ناجحة" in close_msgs[0]
    assert "الأهداف المحققة: 3 من 3" in close_msgs[0]
    assert "مدة التحقق" in close_msgs[0]
    assert "النسبة الفعلية" in close_msgs[0] and "الصافية بعد العمولة" in close_msgs[0]

    # ── الدفتر ──
    import csv
    rows = list(csv.DictReader((tmp_path / "trades_log.csv").open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["outcome"] == "win"
    assert float(rows[0]["net_return_pct"]) > 0
    assert rows[0]["exit_reason"] == "all_targets"
    assert rows[0]["targets_hit_count"] == "3"
    assert rows[0]["hit_target1"] == "1" and rows[0]["hit_target3"] == "1"


def test_signal_down_closes_trade_without_targets(tmp_path):
    """مسار البيع يبقى سائدًا: منصة دون الهدف الأول ثم شمعة هبوط تقطع sma_low."""
    bot = build(tmp_path)
    closes = [100.0] * 440 + [99.0, 105.0]
    highs = [100.2] * 440 + [99.2, 105.2]
    lows = [99.8] * 440 + [98.8, 104.8]
    entry_bar_open = BASE_MS + 441 * HOUR_MS
    bot._process_one_closed_hour({"AAAUSDT": make_frame(closes, highs, lows)}, entry_bar_open)
    assert "AAAUSDT" in bot.state["open_trades"]
    trade = bot.state["open_trades"]["AAAUSDT"]
    t1 = trade["targets"][0]

    def push(i, high, low, close):
        closes.append(close)
        highs.append(high)
        lows.append(low)
        bot._process_one_closed_hour(
            {"AAAUSDT": make_frame(closes, highs, lows)},
            BASE_MS + (441 + i) * HOUR_MS,
        )

    # 12 شمعة منصة دون الهدف الأول: ترفع sma_low دون لمس الأهداف أو الوقف
    for i in range(1, 13):
        push(i, t1 - 0.2, t1 - 0.7, t1 - 0.3)
    assert "AAAUSDT" in bot.state["open_trades"]
    assert bot.state["open_trades"]["AAAUSDT"]["hit"] == [False, False, False]

    # شمعة هبوط: close < sma_low مع low فوق الوقف → إغلاق بإشارة البيع
    push(13, t1 - 1.5, t1 - 2.0, t1 - 1.7)
    assert "AAAUSDT" not in bot.state["open_trades"], "لم تُغلق الصفقة عند إشارة البيع"

    close_msgs = [m for m in bot.telegram.sent if "انتهت الصفقة" in m]
    assert len(close_msgs) == 1
    assert "إشارة بيع" in close_msgs[0]
    assert "الأهداف المحققة: 0 من 3" in close_msgs[0]

    import csv
    rows = list(csv.DictReader((tmp_path / "trades_log.csv").open(encoding="utf-8")))
    assert rows[0]["exit_reason"] == "signal_down"
    assert rows[0]["targets_hit_count"] == "0"


def test_entry_blocked_when_open_trade_exists(tmp_path):
    bot = build(tmp_path)
    closes = [100.0] * 440 + [99.0, 105.0]
    highs = [c + 0.2 for c in closes]
    lows = [c - 0.2 for c in closes]
    df = make_frame(closes, highs, lows)

    # شمعة الاندفاع: يُفتح
    bot._process_one_closed_hour({"AAAUSDT": df}, BASE_MS + 441 * HOUR_MS)
    assert "AAAUSDT" in bot.state["open_trades"]
    first_trade_id = bot.state["open_trades"]["AAAUSDT"]["trade_id"]

    # منصة أسعار دون الهدف الأول حتى تبقى الصفقة مفتوحة
    ceiling = bot.state["open_trades"]["AAAUSDT"]["targets"][0] - 0.5

    # شموع تالية — لا يُسمح بتداخل صفقة جديدة
    for i in range(1, 4):
        closes.append(ceiling - 0.1)
        highs.append(ceiling)
        lows.append(ceiling - 0.4)
        bot._process_one_closed_hour(
            {"AAAUSDT": make_frame(closes, highs, lows)},
            BASE_MS + (441 + i) * HOUR_MS,
        )
        assert bot.state["open_trades"]["AAAUSDT"]["trade_id"] == first_trade_id, "تداخل صفقة جديدة"
    assert sum(1 for m in bot.telegram.sent if "إشارة شراء" in m) == 1
