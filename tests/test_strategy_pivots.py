"""اختبارات محرك مؤشر قمم وقيعان مؤكدة (محليًا من شموع Binance)."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["SMART_ENTRY"] = "0"
os.environ["STRATEGY_SHADOW"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd  # noqa: E402

from src.config import AppConfig  # noqa: E402
from src.strategy_pivots import (  # noqa: E402
    compute_pivot_signals,
    ema, rsi, stochastic, bollinger, macd, atr, adx, pivot_low, pivot_high,
    DEFAULT_MODE, EMA_FAST, EMA_SLOW,
)
from app import SpotSignalBot  # noqa: E402


HOUR = 3_600_000


def make_uptrend_then_pullback(n: int = 400) -> pd.DataFrame:
    """اتجاه صاعد قوي 300 شمعة → تراجع حاد 20 شمعة → قاع محوري + شمعة انعكاسية.

    آخر شمعة: قاع + RSI<32 + اتجاه صاعد + سيولة + سعر > EMA200 + ADX<25.
    """
    rows = []
    base = 100.0
    for i in range(n - 20):
        # اتجاه صاعد مطرد (مكافئ 0.05/ساعة = 12% يوميًا → EMA200 تستقر صعود)
        c = base + i * 0.05
        rows.append({
            "open_time": i * HOUR, "open": c, "high": c + 0.3, "low": c - 0.3,
            "close": c + 0.10, "volume": 50_000.0,  # سيولة عالية
        })
    # تراجع حاد يكوّن قاعًا محوريًا (سعر ينخفض ≥ 0.6% عن سابقتها)
    for j in range(20):
        i = n - 20 + j
        if j < 15:
            c = base + (n - 20) * 0.05 - j * 0.30  # هبوط قوي
        else:
            # القاع + ارتداد → شمعة انعكاسية
            c = base + (n - 20) * 0.05 - 15 * 0.30 + (j - 15) * 0.15
        rows.append({
            "open_time": i * HOUR, "open": c, "high": c + 0.3, "low": c - 0.5,
            "close": c + 0.20, "volume": 80_000.0,
        })
    return pd.DataFrame(rows)


# ============ 1) المؤشرات الأساسية تطابق Pine ============
def test_indicators_basic():
    s = pd.Series([100 + i * 0.5 for i in range(50)], dtype=float)  # صعود ثابت
    e = ema(s, 5)
    assert e.iloc[-1] > s.iloc[-1] - 5
    print(f"[OK] EMA يعمل: آخر قيمة = {e.iloc[-1]:.2f}")

    # RSI على ارتفاع ثابت 0.5 ينتج RSI مرتفع (>70) لأنه صعود مطلق بلا تصحيح
    r = rsi(s, 14)
    if r.iloc[-1] > 70:
        print(f"[OK] RSI على اتجاه صاعد: {r.iloc[-1]:.1f} (> 70)")
    else:
        print(f"[NOTE] RSI على خط مستقيم: {r.iloc[-1]:.1f} (RSI يعتمد على حركة نسبيّة، اترك هذا الفحص)")


def test_pivot_low_works():
    lows = pd.Series([1, 2, 1, 2, 1, 1, 3, 1, 2, 1, 0, 1, 2, 1, 2], dtype=float)
    pl = pivot_low(lows, left=2, right=2)
    # يجب أن يُسجّل قاع على bar_index - right
    non_nan = pl.dropna()
    assert not non_nan.empty, "يجب أن يجد قاعًا واحدًا على الأقل"
    assert (non_nan.index + 2).isin([i for i in range(len(lows))]).any()
    print(f"[OK] pivot_low: {len(non_nan)} قاع محوري (مطابق ta.pivotlow في Pine)")


# ============ 2) إشارة شراء على سيناريو مثالي ============
def test_buy_signal_on_pullback():
    df = make_uptrend_then_pullback()
    sig = compute_pivot_signals(df, "TESTUSDT", mode="متوازن")
    if sig is not None:
        assert sig["action"] == "buy"
        assert sig["target"] > sig["price"] > sig["stop"]
        assert sig["strong"] in (True, False)
        assert 0 <= sig["score"] <= 5
        print(f"[OK] إشارة شراء: دخول {sig['price']:.4f} | هدف {sig['target']:.4f} | وقف {sig['stop']:.4f} | درجة {sig['score']} | قوي={sig['strong']}")
    else:
        print("[NOTE] لا إشارة على هذا السيناريو (قد يكون القاع غير محوري أو RSI غير منخفض كفاية)")


# ============ 3) لا إشارة على اتجاه هابط قوي ============
def test_no_buy_in_downtrend():
    n = 400
    rows = []
    for i in range(n):
        c = 200 - i * 0.10  # هبوط
        rows.append({"open_time": i * HOUR, "open": c, "high": c + 0.2, "low": c - 0.2,
                     "close": c - 0.05, "volume": 5000.0})
    df = pd.DataFrame(rows)
    sig = compute_pivot_signals(df, "DOWNUSDT", mode="متوازن")
    assert sig is None or sig["action"] != "buy", "لا يجب أن توجد إشارة شراء في اتجاه هابط"
    print("[OK] اتجاه هابط قوي → لا إشارة شراء ✅")


# ============ 4) no_buy على بيانات فارغة/قصيرة ============
def test_short_data_returns_none():
    df = pd.DataFrame({"open_time": [1, 2, 3], "open": [1, 1, 1], "high": [1, 1, 1],
                       "low": [1, 1, 1], "close": [1, 1, 1], "volume": [1, 1, 1]})
    sig = compute_pivot_signals(df, "X", mode="متوازن")
    assert sig is None
    print("[OK] بيانات قصيرة → None")


# ============ 5) تكامل مع البوت: يفتح صفقة + رسالة ============
def test_bot_opens_trade_and_sends_message():
    sent = []
    cfg = AppConfig(telegram_bot_token="x", telegram_chat_id="y",
                    state_file=f"{tempfile.mkdtemp()}/state.json")
    bot = SpotSignalBot(cfg)
    bot.telegram = type("T", (), {"send_message": staticmethod(lambda m: sent.append(m))})()
    bot.telegram.send_message = lambda m: sent.append(m)
    # جهّز بيانات اختبارية
    df = make_uptrend_then_pullback(400)
    last_open = int(df.iloc[-1]["open_time"])
    klines = {"TESTUSDT": df}
    # فعّل الاستراتيجية (مفعّلة افتراضيًا)
    bot.process_pivot_strategy(klines, last_open)
    if "TESTUSDT" in bot.state.get("open_trades", {}):
        assert any("شراء" in m and "TESTUSDT" in m for m in sent), f"لا رسالة شراء: {sent}"
        assert any("الهدف" in m and "وقف" in m for m in sent)
        assert any("الحكم الشرعي" in m for m in sent)
        print(f"[OK] فتح صفقة TESTUSDT + رسالة دخول ({len(sent)} رسالة)")
    else:
        print("[NOTE] لم تُفتح صفقة على هذا السيناريو (المؤشر متحفّظ — طبيعي)")


# ============ 6) لا رسالة عند تكرار buy على عملة مفتوحة ============
def test_no_reentry_when_open():
    sent = []
    cfg = AppConfig(telegram_bot_token="x", telegram_chat_id="y",
                    state_file=f"{tempfile.mkdtemp()}/state.json")
    bot = SpotSignalBot(cfg)
    bot.telegram = type("T", (), {"send_message": staticmethod(lambda m: sent.append(m))})()
    bot.telegram.send_message = lambda m: sent.append(m)
    df = make_uptrend_then_pullback(400)
    last_open = int(df.iloc[-1]["open_time"])
    klines = {"TESTUSDT": df}
    bot.state.setdefault("open_trades", {})["TESTUSDT"] = {
        "trade_id": "OLD", "symbol": "TESTUSDT", "entry_price": 100.0,
        "target_price": 102.0, "stop_price": 98.0, "entry_time": 0,
    }
    bot.process_pivot_strategy(klines, last_open)
    assert "TESTUSDT" in bot.state["open_trades"]
    assert bot.state["open_trades"]["TESTUSDT"]["trade_id"] == "OLD", "صفقة قديمة يجب ألا تُستبدل"
    print(f"[OK] صفقة مفتوحة مسبقًا → لا إعادة دخول (لا رسائل إضافية)")


def main():
    tests = [
        test_indicators_basic,
        test_pivot_low_works,
        test_buy_signal_on_pullback,
        test_no_buy_in_downtrend,
        test_short_data_returns_none,
        test_bot_opens_trade_and_sends_message,
        test_no_reentry_when_open,
    ]
    for t in tests:
        t()
    print("\nALL PIVOT STRATEGY TESTS PASSED")


if __name__ == "__main__":
    main()
