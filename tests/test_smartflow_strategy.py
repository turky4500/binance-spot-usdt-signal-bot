"""اختبارات فرضية Smart Flow v1.1 (المعايَرة) — منطق خالص عبر pytest.

python -m pytest tests/test_smartflow_strategy.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.smartflow_strategy import (  # noqa: E402
    SmartFlowSettings,
    atr,
    evaluate,
    rsi,
    sma,
)

H = 3_600_000
BASE = 1_699_999_200_000  # محاذاة لشبكة الساعة UTC


def make_df(rows):
    n = len(rows)
    return pd.DataFrame({
        "open_time": [BASE + i * H for i in range(n)],
        "close_time": [BASE + (i + 1) * H - 1 for i in range(n)],
        "open": [float(r[0]) for r in rows],
        "high": [float(r[1]) for r in rows],
        "low": [float(r[2]) for r in rows],
        "close": [float(r[3]) for r in rows],
    })


def _row(o: float, h: float, low: float, c: float) -> tuple:
    return (o, h, low, c)


def flat_then_breakout_rows():
    """تسطيح ثم هبوط ثم شمعة اختراق كبيرة — شراء (انقلاب نافذة 10 شموع)."""
    rows = []
    c = 100.0
    for i in range(188):
        delta = 0.3 if i % 2 == 0 else -0.3
        o, cl = c, c + delta
        rows.append(_row(o, max(o, cl) + 0.1, min(o, cl) - 0.1, cl))
        c = cl
    for _ in range(10):  # هبوط: الإغلاقات من 99.5 إلى 95.0
        o, cl = c, c - 0.5
        rows.append(_row(o, o + 0.1, cl - 0.3, cl))
        c = cl
    o, cl = c, c + 8.0   # شمعة الانقلاب: إغلاق 103.0
    rows.append(_row(o, cl + 0.5, o - 0.2, cl))
    return rows


def flat_then_breakdown_rows():
    """تسطيح ثم صعود ثم شمعة كسر كبيرة — بيع (شبكة معكوسة)."""
    rows = []
    c = 100.0
    for i in range(188):
        delta = 0.3 if i % 2 == 0 else -0.3
        o, cl = c, c + delta
        rows.append(_row(o, max(o, cl) + 0.1, min(o, cl) - 0.1, cl))
        c = cl
    for _ in range(10):  # صعود: الإغلاقات من 100.5 إلى 105.0
        o, cl = c, c + 0.5
        rows.append(_row(o, cl + 0.3, o - 0.2, cl))
        c = cl
    o, cl = c, c - 8.0   # شمعة الكسر: إغلاق 97.0
    rows.append(_row(o, o + 0.2, cl - 0.5, cl))
    return rows


def flat_rows(n: int = 200):
    rows = []
    c = 100.0
    for i in range(n):
        delta = 0.3 if i % 2 == 0 else -0.3
        o, cl = c, c + delta
        rows.append(_row(o, max(o, cl) + 0.1, min(o, cl) - 0.1, cl))
        c = cl
    return rows


def long_decline_then_breakout_rows():
    """هبوط مطّرد (EMA20 تحت EMA50) ثم انقلاب — يثبت أن فرضية v1.1 بلا شرط اتجاه."""
    rows = []
    c = 300.0
    for _ in range(150):
        o, cl = c, c - 1.0
        rows.append(_row(o, o + 0.2, cl - 0.3, cl))
        c = cl
    for _ in range(20):  # استقرار قصير
        o, cl = c, c - 0.05
        rows.append(_row(o, o + 0.1, cl - 0.2, cl))
        c = cl
    o, cl = c, c + 8.0   # شمعة الانقلاب من القاع
    rows.append(_row(o, cl + 0.4, o - 0.3, cl))
    return rows


def rising_rows(n: int = 300):
    return [_row(float(i), float(i) + 0.5, float(i) - 0.5, float(i)) for i in range(n)]


def falling_rows(n: int = 300):
    rows = []
    price = 500.0
    for _ in range(n):
        price -= 1.0
        rows.append(_row(price + 1.0, price + 1.2, price - 0.5, price))
    return rows


ZH = SmartFlowSettings()


# ── RSI ──
def test_rsi_rising_pure_is_100():
    series = pd.Series([float(i) for i in range(300)])
    assert rsi(series, 14).iloc[-1] == pytest.approx(100.0)


def test_rsi_falling_pure_is_0():
    series = pd.Series([300.0 - float(i) for i in range(300)])
    assert rsi(series, 14).iloc[-1] == pytest.approx(0.0)


# ── ATR / SMA ──
def test_atr_constant_range():
    rows = [(float(i), float(i) + 1.0, float(i) - 1.0, float(i)) for i in range(300)]
    assert atr(make_df(rows), 14).iloc[-1] == pytest.approx(2.0)


def test_sma_known_value():
    series = pd.Series([float(i) for i in range(50)])
    assert sma(series, 20).iloc[-1] == pytest.approx(sum(range(30, 50)) / 20.0)


# ── فرضية v1.1: انقلاب النافذة ──
def test_buy_on_window_breakout():
    sig = evaluate(make_df(flat_then_breakout_rows()), ZH)
    assert sig is not None
    assert sig["kind"] == "buy"
    assert sig["close"] == pytest.approx(103.0)
    # المستوى = أعلى قمة نافذة 10 السابقة (قمم المهمة الهابطة ≈ 100.1)
    assert sig["breakout_level"] == pytest.approx(100.1, abs=0.11)
    # الوقف المبدئي = أدنى قاع النافذة (≈ 94.7)
    assert sig["stop_level"] == pytest.approx(94.7, abs=0.11)


def test_sell_on_window_breakdown():
    sig = evaluate(make_df(flat_then_breakdown_rows()), ZH)
    assert sig is not None
    assert sig["kind"] == "sell"
    assert sig["close"] == pytest.approx(97.0)
    assert sig["breakout_level"] == pytest.approx(99.8, abs=0.11)
    assert sig["stop_level"] == pytest.approx(105.2, abs=0.11)


def test_no_signal_on_flat():
    assert evaluate(make_df(flat_rows(200)), ZH) is None


def test_buy_without_trend_condition_v1_1():
    """لقطة البائع: الشراء وقع عند القاع والاتجاه ما زال هابطًا — فلا شرط اتجاه في v1.1."""
    sig = evaluate(make_df(long_decline_then_breakout_rows()), ZH)
    assert sig is not None
    assert sig["kind"] == "buy"
    assert sig["trend_up"] is False


def test_rsi_gate_off_by_default_and_functional_when_enabled():
    df = make_df(flat_then_breakout_rows())
    assert evaluate(df, ZH) is not None                     # البوابة مغلقة افتراضيًا
    strict = SmartFlowSettings(use_rsi_gate=True, buy_rsi_min=99.9, sell_rsi_max=0.0)
    assert evaluate(df, strict) is None                     # بوابة تعصف بالشراء من قاع (RSI منخفض)
    loose = SmartFlowSettings(use_rsi_gate=True, buy_rsi_min=0.0, sell_rsi_max=100.0)
    assert evaluate(df, loose) is not None and evaluate(df, loose)["kind"] == "buy"


def test_evaluate_returns_signal_candle_open():
    """v1.2: سعر الدخول = افتتاح شمعة الإشارة (ما يعرضه جدول المؤشر في اللقطات)."""
    sig = evaluate(make_df(flat_then_breakout_rows()), ZH)
    assert sig is not None
    assert sig["open"] == pytest.approx(95.0)  # افتتاح شمعة الانقلاب (قبل +8.0)


def test_not_enough_bars_returns_none():
    assert evaluate(make_df(flat_rows(59)), ZH) is None


def test_min_bars_and_settings_from_env(monkeypatch):
    monkeypatch.setenv("SMARTFLOW_EMA_SLOW_LEN", "60")
    monkeypatch.setenv("SMARTFLOW_BREAK_LEN", "12")
    monkeypatch.setenv("SMARTFLOW_SIGNAL_MODE", "every_bar")
    monkeypatch.setenv("SMARTFLOW_USE_RSI_GATE", "1")
    s = SmartFlowSettings.from_env()
    assert s.ema_slow_len == 60 and s.break_len == 12
    assert s.signal_mode == "every_bar" and s.use_rsi_gate is True
    assert s.min_bars() >= 70
