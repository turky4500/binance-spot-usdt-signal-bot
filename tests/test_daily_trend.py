"""اختبارات نظام «تتبّع الاتجاه اليومي» — منطق الإشارة والوقف والحجم بدون شبكة.

python -m pytest tests/test_daily_trend.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.daily_trend import (  # noqa: E402
    DailyTrendSettings,
    atr_series,
    breakout_signal,
    closed_bars,
    ema_series,
    exit_reason,
    market_gate,
    net_pnl_pct,
    position_notional,
    position_pnl_usd,
    update_trailing_stop,
)

DAY = 86_400_000
BASE = 1_700_000_000_000


def make_daily(closes, highs=None, lows=None, start=BASE):
    n = len(closes)
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    return pd.DataFrame({
        "open_time": [start + i * DAY for i in range(n)],
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1000.0] * n,
        "close_time": [start + i * DAY + DAY - 1 for i in range(n)],
        "quote_asset_volume": [1_000_000.0] * n,
    })


# ─────────────────────────── المؤشرات ───────────────────────────
def test_ema_matches_manual_recursion():
    values = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = ema_series(values, 3)
    assert np.isnan(out.iloc[1])
    assert out.iloc[2] == pytest.approx(2.0)                 # متوسط أول 3
    assert out.iloc[3] == pytest.approx(2.0 + 0.5 * (4.0 - 2.0))
    assert out.iloc[4] == pytest.approx(3.0 + 0.5 * (5.0 - 3.0))


def test_ema_needs_full_window():
    out = ema_series(pd.Series([1.0, 2.0]), 5)
    assert out.isna().all()


def test_atr_is_positive_and_uses_true_range():
    closes = [100, 101, 102, 103, 104, 105, 106]
    highs = [c + 2 for c in closes]
    lows = [c - 2 for c in closes]
    df = make_daily(closes, highs, lows)
    atr = atr_series(df, 3)
    assert np.isnan(atr.iloc[1])
    assert atr.iloc[2] == pytest.approx(4.0, rel=1e-6)       # TR = 4 لكل شمعة
    assert atr.iloc[-1] == pytest.approx(4.0, rel=1e-6)


def test_closed_bars_excludes_forming_candle():
    df = make_daily([1, 2, 3])
    now = int(df["close_time"].iloc[-1]) - 1                 # آخر شمعة لم تُغلق بعد
    assert len(closed_bars(df, now)) == 2


# ─────────────────────────── بوابة السوق ───────────────────────────
def _btc_4h(closes):
    n = len(closes)
    return pd.DataFrame({
        "open_time": [BASE + i * 4 * 3_600_000 for i in range(n)],
        "open": closes, "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
        "close": closes, "volume": [1.0] * n,
        "close_time": [BASE + i * 4 * 3_600_000 + 4 * 3_600_000 - 1 for i in range(n)],
    })


def test_gate_closed_without_enough_history():
    gate = market_gate(_btc_4h([100.0] * 50), BASE + 100 * DAY, 200)
    assert gate["open"] is False


def test_gate_open_in_uptrend_and_closed_in_downtrend():
    up = _btc_4h([100.0 + i for i in range(260)])
    now = int(up["close_time"].iloc[-1]) + 1
    assert market_gate(up, now, 200)["open"] is True
    down = _btc_4h([400.0 - i for i in range(260)])
    now = int(down["close_time"].iloc[-1]) + 1
    assert market_gate(down, now, 200)["open"] is False


# ─────────────────────────── إشارة الدخول ───────────────────────────
def _settings(**kw):
    base = dict(lookback=5, atr_len=3, atr_mult=2.5, min_stop_pct=0.5, max_stop_pct=20.0)
    base.update(kw)
    return DailyTrendSettings(**base)


def test_breakout_detected_on_close_above_prior_highs():
    closes = [100, 101, 102, 103, 104, 105, 120]             # آخر شمعة تخترق بقوة
    df = make_daily(closes)
    sig = breakout_signal(df, BASE + 10 * DAY, _settings())
    assert sig is not None
    assert sig["entry_price"] == pytest.approx(120)
    assert sig["breakout_high"] == pytest.approx(105 * 1.01)  # أعلى high للشموع السابقة


def test_breakout_ignored_when_close_below_prior_high():
    closes = [100, 101, 102, 103, 104, 105, 105.5]
    df = make_daily(closes)
    assert breakout_signal(df, BASE + 10 * DAY, _settings()) is None


def test_breakout_ignores_the_current_bar_high_no_lookahead():
    """أعلى سعر الشمعة الحالية لا يدخل في حساب الاختراق (لا نظر للمستقبل)."""
    closes = [100, 101, 102, 103, 104, 105, 120]
    highs = [c * 1.01 for c in closes]
    highs[-1] = 500.0                                        # شمعة الدخول نفسها ضخمة
    df = make_daily(closes, highs)
    # ATR يتضخم بسبب الشمعة الضخمة ⇒ نرفع سقف الوقف في هذا الاختبار فقط
    sig = breakout_signal(df, BASE + 10 * DAY, _settings(max_stop_pct=1e9))
    assert sig is not None
    assert sig["breakout_high"] == pytest.approx(105 * 1.01)


def test_breakout_requires_enough_history():
    df = make_daily([100, 101, 102])
    assert breakout_signal(df, BASE + 10 * DAY, _settings(lookback=5)) is None


def test_breakout_rejects_absurd_stop():
    closes = [100, 101, 102, 103, 104, 105, 120]
    df = make_daily(closes, highs=[c * 1.6 for c in closes], lows=[c * 0.4 for c in closes])
    # ATR ضخم ⇒ الوقف أبعد من الحد الأقصى ⇒ رفض
    assert breakout_signal(df, BASE + 10 * DAY, _settings(max_stop_pct=5.0)) is None


def test_breakout_stop_equals_entry_minus_atr_multiple():
    closes = [100, 101, 102, 103, 104, 105, 120]
    highs = [c + 2 for c in closes]
    lows = [c - 2 for c in closes]
    df = make_daily(closes, highs, lows)
    s = _settings(atr_len=3, atr_mult=2.5)
    sig = breakout_signal(df, BASE + 10 * DAY, s)
    atr = atr_series(closed_bars(df, BASE + 10 * DAY), s.atr_len).iloc[-1]
    assert sig["stop_price"] == pytest.approx(120 - 2.5 * atr)


# ─────────────────────────── الوقف المتحرك ───────────────────────────
def test_trailing_stop_never_moves_backwards():
    atr, mult = 2.0, 2.5
    stop = update_trailing_stop(100.0, high_since=110.0, atr_entry=atr, atr_mult=mult)
    assert stop == pytest.approx(105.0)
    higher_high = update_trailing_stop(stop, high_since=120.0, atr_entry=atr, atr_mult=mult)
    assert higher_high == pytest.approx(115.0)
    lower_high = update_trailing_stop(higher_high, high_since=90.0, atr_entry=atr, atr_mult=mult)
    assert lower_high == pytest.approx(115.0)                # لا يتراجع


def test_trailing_stop_ratchets_to_breakeven_after_one_r():
    entry, atr, mult = 100.0, 2.0, 2.5
    stop = entry - mult * atr                                 # 95
    stop = update_trailing_stop(stop, high_since=entry + mult * atr, atr_entry=atr, atr_mult=mult)
    assert stop == pytest.approx(entry)


# ─────────────────────────── الخروج ───────────────────────────
def test_exit_on_stop_touch():
    pos = {"stop_price": 100.0}
    assert exit_reason(pos, 99.9, True) == "stop"
    assert exit_reason(pos, 100.0, True) == "stop"
    assert exit_reason(pos, 100.1, True) is None


def test_exit_when_gate_closes_even_if_price_is_fine():
    pos = {"stop_price": 90.0}
    assert exit_reason(pos, 120.0, False) == "gate"
    assert exit_reason(pos, 120.0, True) is None


# ─────────────────────────── الحجم والربح ───────────────────────────
def test_position_size_respects_risk_and_cap():
    # رصيد 300، مخاطرة 1% = 3$، وقف 5% ⇒ 60$ (أقل من 300/2 = 150)
    assert position_notional(300, 1.0, 100, 95, 2) == pytest.approx(60.0)
    # وقف 1% ⇒ 300$ مطلوبة لكن الحد الأقصى 150$
    assert position_notional(300, 1.0, 100, 99, 2) == pytest.approx(150.0)


def test_position_size_zero_for_invalid_stop():
    assert position_notional(300, 1.0, 100, 100, 2) == 0.0
    assert position_notional(0, 1.0, 100, 95, 2) == 0.0


def test_net_pnl_includes_both_commissions():
    assert net_pnl_pct(100, 105, 0.1) == pytest.approx(4.8)
    pos = {"notional": 100.0, "entry_price": 100.0}
    assert position_pnl_usd(pos, 105, 0.1) == pytest.approx(4.8)
