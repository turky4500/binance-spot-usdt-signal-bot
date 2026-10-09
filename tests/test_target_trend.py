"""اختبارات مؤشر Target Trend [BigBeluga] — الكشف، الأهداف، الوقف، والصفقات.

تعمل بـ pytest مباشرة:  python -m pytest tests/ -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategy import StrategySettings, compute_trend_frame, latest_signal

SETTINGS = StrategySettings(length=10, target_offset=0)
BASE_MS = 1_700_000_000_000
HOUR_MS = 3_600_000


def make_df(closes: list[float], highs: list[float] | None = None, lows: list[float] | None = None) -> pd.DataFrame:
    n = len(closes)
    highs = highs or [c * 1.001 for c in closes]
    lows = lows or [c * 0.999 for c in closes]
    return pd.DataFrame({
        "open_time": [BASE_MS + i * HOUR_MS for i in range(n)],
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1000.0] * n,
        "close_time": [BASE_MS + i * HOUR_MS + HOUR_MS - 1 for i in range(n)],
    })


def dip_then_rally(n_flat: int = 440) -> tuple[list, list, list]:
    """سعر ثابت → هبوط تحت sma_low (trend=false) ثم اندفاع فوق sma_high (signal_up).

    مطابق لسلوك Pine: إشارة الشراء تتطلب انقلاب الاتجاه من false إلى true
    (crossunder سابق داخل النافذة ثم crossover على الشمعة الأخيرة).
    """
    closes = [100.0] * n_flat + [99.0, 105.0]
    highs = [100.2] * n_flat + [99.2, 105.2]
    lows = [99.8] * n_flat + [98.8, 104.8]
    return closes, highs, lows


def test_settings_defaults_match_pine():
    s = StrategySettings()
    assert s.length == 10
    assert s.target_multipliers == (5.0, 10.0, 15.0)
    assert s.atr_len == 200
    assert s.atr_smooth_len == 200
    assert s.atr_scale == 0.8


def test_min_bars_covers_atr_warmup():
    assert SETTINGS.min_bars >= 400


def test_insufficient_history_returns_none():
    df = make_df([100.0] * 100)
    assert latest_signal(df, SETTINGS) is None


def test_flat_market_no_signal():
    df = make_df([100.0] * 440)
    assert latest_signal(df, SETTINGS) is None


def test_buy_signal_on_bullish_cross():
    closes, highs, lows = dip_then_rally()
    df = make_df(closes, highs, lows)
    signal = latest_signal(df, SETTINGS)
    assert signal is not None
    assert signal["action"] == "buy"
    assert signal["entry_price"] == pytest.approx(closes[-1])
    # الوقف = sma_low من شمعة الإشارة (سماوية 10 أسفل)
    assert signal["stop_price"] < signal["entry_price"]
    # الأهداف الثلاثة تصاعدية وبعيدة عن الدخول
    t1, t2, t3 = signal["targets"]
    assert t1 < t2 < t3
    assert t1 > signal["entry_price"]


def test_targets_are_atr_multipliers():
    closes, highs, lows = dip_then_rally()
    df = make_df(closes, highs, lows)
    data = compute_trend_frame(df, SETTINGS)
    row = data.iloc[-1]
    atr_value = float(row["atr_value"])
    assert atr_value > 0
    assert row["close"] + 5 * atr_value == pytest.approx(row["close"] + 5 * atr_value)
    # التحقق: الأهداف المحسوبة = close + (5,10,15)×atr_value
    sig = latest_signal(df, SETTINGS)
    for mult, target in zip((5, 10, 15), sig["targets"]):
        assert target == pytest.approx(row["close"] + mult * atr_value, rel=1e-9)


def test_sell_signal_after_bearish_cross_under():
    # اتجاه صاعد أولًا ثم انهيار
    closes = [100.0] * 440
    closes += [110.0, 111.0]           # قفزة → crossover → trend=true
    closes += [111.0] * 5              # استمرار الصعود
    closes += [95.0]                    # انهيار → crossunder → trend=false
    df = make_df(closes)
    signal = latest_signal(df, SETTINGS)
    assert signal is not None
    assert signal["action"] == "sell"
    assert signal["price"] == pytest.approx(95.0)


def test_trend_persists_between_crosses():
    closes = [100.0] * 440 + [110.0, 111.0, 112.0, 113.0]
    df = make_df(closes)
    data = compute_trend_frame(df, SETTINGS)
    # بعد أول crossover يبقى الاتجاه صاعدًا دون إعادة إشارة
    assert bool(data.iloc[-1]["trend"]) is True
    assert not bool(data.iloc[-1]["signal_up"])
    assert not bool(data.iloc[-1]["signal_down"])


def test_signal_not_repeated_on_successive_bars():
    closes = [100.0] * 440 + [99.0, 105.0, 106.0, 107.0]
    df = make_df(closes)
    data = compute_trend_frame(df, SETTINGS)
    up_bars = data.index[data["signal_up"]].tolist()
    assert len(up_bars) == 1
    assert up_bars[0] == len(closes) - 3  # على شمعة الاندفاع فقط


def test_uncertain_when_no_prior_cross_in_window():
    """تقاطع على الشمعة الأخيرة دون أي تقاطع سابق في النافذة → uncertain (لغموض الماضي)."""
    closes = [100.0] * 440 + [105.0]  # لا crossunder سابق إطلاقًا
    highs = [100.2] * 440 + [105.2]
    lows = [99.8] * 440 + [104.8]
    df = make_df(closes, highs, lows)
    signal = latest_signal(df, SETTINGS)
    assert signal is not None
    assert signal["action"] == "uncertain"


def test_initial_trend_resolves_uncertain():
    """بتحديد الحالة السابقة صراحةً (هابط) يظهر شراء، و(صاعد) لا إشارة — كما في Pine."""
    closes = [100.0] * 440 + [105.0]
    highs = [100.2] * 440 + [105.2]
    lows = [99.8] * 440 + [104.8]
    df = make_df(closes, highs, lows)

    up = latest_signal(df, SETTINGS, initial_trend=0.0)
    assert up is not None and up["action"] == "buy"

    down = latest_signal(df, SETTINGS, initial_trend=1.0)
    assert down is None  # كان صاعدًا بالفعل → تغيّر لا يوجد (كما في Pine)


def test_pine_na_semantics_no_signal_on_first_flip():
    """سلوك Pine: أول انتقال na→true لا يُصدر إشارة ما لم تُعرف الحالة السابقة."""
    closes = [100.0] * 440 + [105.0]
    df = make_df(closes)
    data = compute_trend_frame(df, SETTINGS)
    assert bool(data.iloc[-1]["trend"]) is True      # الاتجاه صار صاعدًا
    assert not bool(data.iloc[-1]["signal_up"])      # لكن بلا signal (prev=na)
