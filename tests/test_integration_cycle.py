"""اختبار تكامل: دورة شمعة مغلقة كاملة (دخول → أهداف → خروج بيع) ببيانات وهمية."""
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


def test_full_cycle_entry_then_sell(tmp_path):
    bot = build(tmp_path)
    # 442 شمعة: ثابت → هبوط (trend=false) → اندفاع (signal_up) على شمعة الدخول
    closes = [100.0] * 440 + [99.0, 105.0]
    highs = [100.2] * 440 + [99.2, 105.2]
    lows = [99.8] * 440 + [98.8, 104.8]
    entry_bar_open = BASE_MS + 441 * HOUR_MS

    df = make_frame(closes, highs, lows)
    bot._process_one_closed_hour({"AAAUSDT": df}, entry_bar_open)

    # ── الدخول وقع ──
    assert "AAAUSDT" in bot.state["open_trades"], "لم تُفتح الصفقة"
    trade = bot.state["open_trades"]["AAAUSDT"]
    assert trade["entry_price"] == 105.0
    assert trade["stop_price"] < 105.0
    assert len(trade["targets"]) == 3
    assert trade["targets"][0] < trade["targets"][1] < trade["targets"][2]
    assert any("إشارة شراء" in m for m in bot.telegram.sent)

    # ── رحلة صعود: تتحقق الأهداف الثلاثة ولا تُغلق الصفقة ──
    for i, price in enumerate((110.0, 118.0, 126.0, 130.0, 130.0), start=1):
        closes.append(price); highs.append(price + 0.2); lows.append(price - 0.2)
        bot._process_one_closed_hour(
            {"AAAUSDT": make_frame(closes, highs, lows)},
            BASE_MS + (441 + i) * HOUR_MS,
        )
        assert "AAAUSDT" in bot.state["open_trades"], f"أُغلقت الصفقة مبكرًا عند شمعة {i}"
    hit = bot.state["open_trades"]["AAAUSDT"]["hit"]
    assert hit == [True, True, True], "لم تُرصد الأهداف الثلاثة"
    assert sum(1 for m in bot.telegram.sent if "تحقق الهدف" in m) == 3

    # ── شمعة الهبوط: crossunder دون لمس الوقف → إغلاق رابحًا بإشارة البيع ──
    closes.append(106.0); highs.append(106.2); lows.append(105.8)
    bot._process_one_closed_hour(
        {"AAAUSDT": make_frame(closes, highs, lows)},
        BASE_MS + (441 + 6) * HOUR_MS,
    )
    assert "AAAUSDT" not in bot.state["open_trades"], "لم تُغلق الصفقة عند إشارة البيع"
    close_msgs = [m for m in bot.telegram.sent if "انتهت الصفقة" in m]
    assert len(close_msgs) == 1
    assert "رابحة" in close_msgs[0]
    assert "الأهداف المحققة: 3 من 3" in close_msgs[0]
    assert "إشارة بيع" in close_msgs[0]

    # ── الدفتر ──
    import csv
    rows = list(csv.DictReader((tmp_path / "trades_log.csv").open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["outcome"] == "win"
    assert float(rows[0]["net_return_pct"]) > 0
    assert rows[0]["exit_reason"] == "signal_down"
    assert rows[0]["targets_hit_count"] == "3"
    assert rows[0]["hit_target1"] == "1" and rows[0]["hit_target3"] == "1"


def test_entry_blocked_when_open_trade_exists(tmp_path):
    bot = build(tmp_path)
    closes = [100.0] * 440 + [99.0, 105.0, 106.0, 107.0, 108.0, 109.0, 110.0, 111.0, 112.0, 113.0, 114.0, 115.0]
    highs = [c + 0.2 for c in closes]
    lows = [c - 0.2 for c in closes]
    df = make_frame(closes, highs, lows)
    last_open = df.iloc[-1]["open_time"]

    # شمعة الاندفاع: يُفتح
    bot._process_one_closed_hour({"AAAUSDT": df}, BASE_MS + 441 * HOUR_MS)
    assert "AAAUSDT" in bot.state["open_trades"]
    first_trade_id = bot.state["open_trades"]["AAAUSDT"]["trade_id"]

    # شمعة تالية بها إشارة دخول نظرية — لا يُسمح بتداخل صفقة
    bot._process_one_closed_hour({"AAAUSDT": df}, int(last_open))
    assert bot.state["open_trades"]["AAAUSDT"]["trade_id"] == first_trade_id
    assert sum(1 for m in bot.telegram.sent if "إشارة شراء" in m) == 1
