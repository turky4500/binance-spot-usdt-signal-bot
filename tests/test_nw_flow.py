"""اختبارات دورة NW كاملة: دخول ⇒ تكرار ⇒ خروج، بلا شبكة ولا تيليجرام حقيقي."""

from __future__ import annotations

import numpy as np
import pandas as pd

from nw_bot import NWBot
from src.nw_journal import read_trades
from src.nw_state import NWStateStore

BUY_ARGS = (20, 1.01)
SELL_ARGS = (50, 2.76)
HOUR = 3_600_000


def make_df(kind="buy", base_time=0, flat=False, n=500):
    k = np.arange(n)
    if flat:
        c = np.full(n, 100.0)
    else:
        period, phase = BUY_ARGS if kind == "buy" else SELL_ARGS
        c = 100 + 10 * np.sin(2 * np.pi * k / period + phase)
    o = c.copy()
    if kind == "buy" and not flat:
        o[-1] = c[-1] - 1.0                  # شمعة التأكيد خضراء
    t = np.arange(n, dtype=np.int64) * HOUR + base_time
    return pd.DataFrame({
        "open_time": t,
        "open": o,
        "high": np.maximum(o, c) + 0.5,
        "low": np.minimum(o, c) - 0.5,
        "close": c,
        "volume": np.ones(n),
        "close_time": t + HOUR - 1,
    })


class FakeClient:
    def __init__(self, frame, now_ms):
        self.frame = frame
        self.now_ms = now_ms

    def get_server_time(self):
        return self.now_ms

    def get_spot_usdt_symbols(self):
        return ["BTCUSDT"]

    def get_klines_for_symbols(self, symbols, interval, limit):
        assert interval == "1h"
        return {s: self.frame for s in symbols}


class FakeTelegram:
    def __init__(self):
        self.messages: list[str] = []

    def send_message(self, text: str) -> None:
        self.messages.append(text)


def build(tmp_path, frame, now_ms):
    tg = FakeTelegram()
    store = NWStateStore(str(tmp_path / "state.json"))
    bot = NWBot(
        client=FakeClient(frame, now_ms),
        telegram=tg,
        state_store=store,
        state_path=str(tmp_path / "state.json"),
        trade_log=str(tmp_path / "trades.csv"),
        show_repaint_warning=True,
    )
    return bot, tg, store


def now_for(base_time, n=500):
    return (n - 1) * HOUR + base_time + HOUR


def test_full_flow_entry_repeat_exit(tmp_path):
    frame1 = make_df("buy", base_time=0)
    bot, tg, store = build(tmp_path, frame1, now_for(0))
    s1 = bot.run_once(universe=["BTCUSDT"])
    assert s1["entry"] == 1 and s1["repeat"] == 0 and s1["exit"] == 0
    assert len(tg.messages) == 1 and "توصية دخول" in tg.messages[0]
    assert "شمعة التأكيد خضراء" in tg.messages[0]
    assert "الحكم الشرعي" in tg.messages[0]
    assert store.load()["symbols"]["BTCUSDT"]["position"] is not None

    # نفس التسمية في دورة لاحقة ⇒ لا تكرار رسالة
    s2 = bot.run_once(universe=["BTCUSDT"])
    assert s2["skipped_same_label"] >= 1 and len(tg.messages) == 1

    # علامة خضراء جديدة على شمعة لاحقة ⇒ «إشارة دخول ثانية»
    frame2 = make_df("buy", base_time=HOUR)
    bot.client.frame, bot.client.now_ms = frame2, now_for(HOUR)
    s3 = bot.run_once(universe=["BTCUSDT"])
    assert s3["repeat"] == 1 and s3["entry"] == 0
    assert "إشارة دخول ثانية" in tg.messages[-1]
    assert store.load()["symbols"]["BTCUSDT"]["entry_count"] == 2

    # علامة حمراء ▼ ⇒ خروج + صفقة في الدفتر
    frame3 = make_df("sell", base_time=2 * HOUR)
    bot.client.frame, bot.client.now_ms = frame3, now_for(2 * HOUR)
    s4 = bot.run_once(universe=["BTCUSDT"])
    assert s4["exit"] == 1
    last = tg.messages[-1]
    assert "إشارة خروج" in last and "العلامة الحمراء" in last
    assert "سعر الدخول" in last and "سعر الخروج" in last
    assert ("ربح ✅" in last) or ("خسارة 🛑" in last) or ("تعادل ⚖️" in last)
    state = store.load()
    assert state["symbols"]["BTCUSDT"]["position"] is None
    trades = read_trades(str(tmp_path / "trades.csv"))
    assert len(trades) == 1 and trades[0]["exit_reason"] == "red_label"
    assert trades[0]["entries_count"] == "2"


def test_reappearing_same_label_also_counts_as_second_entry(tmp_path):
    frame = make_df("buy", base_time=0)
    bot, tg, store = build(tmp_path, frame, now_for(0))
    bot.run_once(universe=["BTCUSDT"])
    assert len(tg.messages) == 1

    # دورة بلا تسميات ⇒ تختفي العلامة من prev_buy_bars
    flat = make_df("flat", base_time=0, flat=True)
    bot.client.frame, bot.client.now_ms = flat, now_for(0)
    bot.run_once(universe=["BTCUSDT"])
    assert len(tg.messages) == 1

    # تعود التسمية نفسها (إعادة رسم) ⇒ رسالة تكرار، لا رسالة مكررة صامتة
    bot.client.frame, bot.client.now_ms = frame, now_for(0)
    s = bot.run_once(universe=["BTCUSDT"])
    assert s["repeat"] == 1
    assert "إشارة دخول ثانية" in tg.messages[-1]
    assert len(tg.messages) == 2


def test_ruling_is_taken_from_verdicts_and_shown_in_message(tmp_path):
    """عملة «غير مباح» تُرسل إشارتها ويظهر حكمها الفعلي (لا تُستبعد)."""
    frame = make_df("buy", base_time=0)
    bot, tg, _ = build(tmp_path, frame, now_for(0))
    bot.verdicts = {"BTC": 1}                    # ⛔ غير مباح
    bot._load_verdicts = lambda: bot.verdicts   # يمنع إعادة التحميل من الملفات
    s = bot.run_once(universe=["BTCUSDT"])
    assert s["entry"] == 1
    assert "الحكم الشرعي: ⛔ غير مباح" in tg.messages[0]


def test_no_exit_when_no_open_recommendation(tmp_path):
    frame = make_df("sell", base_time=0)
    bot, tg, store = build(tmp_path, frame, now_for(0))
    s = bot.run_once(universe=["BTCUSDT"])
    assert s["exit"] == 0 and tg.messages == []


def test_no_signal_when_confirmation_candle_is_red(tmp_path):
    frame = make_df("buy", base_time=0)
    frame.loc[frame.index[-1], "open"] = frame["close"].iloc[-1] + 1.0
    bot, tg, _ = build(tmp_path, frame, now_for(0))
    s = bot.run_once(universe=["BTCUSDT"])
    assert s["entry"] == 0 and tg.messages == []
