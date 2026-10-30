"""اختبارات منطق ZigZag++ (1H) — تعمل بـ pytest مباشرة.

python -m pytest tests/ -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.zigzag import (  # noqa: E402
    HOUR_MS,
    MINUTE_MS,
    ZigZagSettings,
    evaluate_entry_candle,
    evaluate_path_1h,
    evaluate_path_1m,
    find_latest_bottom,
    next_closed_bar,
)

BASE_MS = 1_699_999_200_000  # بداية ساعة كاملة


def make_hour_df(rows):
    """rows: قائمة (open, high, low, close) — كل صف شمعة ساعة."""
    n = len(rows)
    return pd.DataFrame({
        "open_time": [BASE_MS + i * HOUR_MS for i in range(n)],
        "close_time": [BASE_MS + (i + 1) * HOUR_MS - 1 for i in range(n)],
        "open": [float(r[0]) for r in rows],
        "high": [float(r[1]) for r in rows],
        "low": [float(r[2]) for r in rows],
        "close": [float(r[3]) for r in rows],
    })


def flat_rows(n=20, price=100.0):
    return [(price, price + 1, price - 1, price) for _ in range(n)]


def make_1m(rows):
    """rows: قائمة (open, high, low, close, close_time)."""
    return pd.DataFrame({
        "open_time": [int(r[4]) - MINUTE_MS + 1 for r in rows],
        "open": [float(r[0]) for r in rows],
        "high": [float(r[1]) for r in rows],
        "low": [float(r[2]) for r in rows],
        "close": [float(r[3]) for r in rows],
        "close_time": [int(r[4]) for r in rows],
    })


# ─────────────────────────────────────────── اكتشاف القاع
def test_find_latest_bottom_detects_lowest_of_depth():
    rows = flat_rows()
    rows[15] = (100.0, 101.0, 90.0, 100.0)
    df = make_hour_df(rows)
    bottom = find_latest_bottom(df, ZigZagSettings())
    assert bottom is not None
    assert bottom["bar_index"] == 15
    assert bottom["low"] == 90.0
    assert bottom["bar_open_time"] == BASE_MS + 15 * HOUR_MS


def test_find_latest_bottom_rejects_ties():
    rows = flat_rows()
    rows[15] = (100.0, 101.0, 90.0, 100.0)
    rows[16] = (100.0, 101.0, 90.0, 100.0)  # تساوٍ — ليس أدنى صراحة
    df = make_hour_df(rows)
    bottom = find_latest_bottom(df, ZigZagSettings())
    assert bottom is not None
    assert bottom["bar_index"] == 15


def test_find_latest_bottom_ignores_old_bottoms():
    rows = flat_rows()
    rows[5] = (100.0, 101.0, 80.0, 100.0)  # قاع أقدم من lookback (6)
    df = make_hour_df(rows)
    assert find_latest_bottom(df, no_ema()) is None


def test_find_latest_bottom_needs_depth_bars():
    rows = flat_rows(n=10)
    df = make_hour_df(rows)
    assert find_latest_bottom(df, no_ema()) is None


def test_next_closed_bar_matches_exact_hour():
    df = make_hour_df(flat_rows(n=20))
    nxt = next_closed_bar(df, BASE_MS + 15 * HOUR_MS)
    assert nxt is not None
    assert int(nxt["open_time"]) == BASE_MS + 16 * HOUR_MS
    assert next_closed_bar(df, BASE_MS + 19 * HOUR_MS) is None  # لا شمعة تالية


# ─────────────────────────────────────────── تقييم شمعة الدخول
def _bottom(low=90.0, index=15):
    return {"bar_index": index, "bar_open_time": BASE_MS + index * HOUR_MS,
            "bar_close_time": BASE_MS + (index + 1) * HOUR_MS - 1, "low": low}


def _row(open_, high, low, close, index=16):
    return pd.Series({
        "open_time": BASE_MS + index * HOUR_MS,
        "close_time": BASE_MS + (index + 1) * HOUR_MS - 1,
        "open": float(open_), "high": float(high), "low": float(low), "close": float(close),
    })


def test_entry_green_candle():
    signal = evaluate_entry_candle(_bottom(), _row(100.0, 103.0, 99.0, 102.0), no_ema())
    assert signal is not None
    assert signal["entry_price"] == 102.0
    assert signal["stop_price"] == 90.0
    assert signal["target_price"] == pytest.approx(102.0 * 1.02)
    assert signal["entry_bar_open_time"] == BASE_MS + 16 * HOUR_MS


def no_ema(**kwargs) -> ZigZagSettings:
    """إعدادات لاختبار قواعد الشمعة وحدها (فلتر EMA200 له اختبارات مستقلة)."""
    return ZigZagSettings(ema_filter=0, **kwargs)


def test_entry_rejects_red_candle():
    assert evaluate_entry_candle(_bottom(), _row(102.0, 103.0, 99.0, 100.0), no_ema()) is None


def test_entry_rejects_doji():
    # جسم 0.02% ≤ 0.05% ⇒ دوجي رغم أنه أخضر
    assert evaluate_entry_candle(_bottom(), _row(100.0, 101.0, 99.0, 100.02), no_ema()) is None


def test_entry_rejects_close_below_bottom():
    # خضراء لكن إغلاقها تحت قاع الإشارة (فجوة سفلية)
    assert evaluate_entry_candle(_bottom(low=95.0), _row(90.0, 96.0, 89.0, 94.5), no_ema()) is None


def test_min_stop_filter_off_by_default():
    # الافتراضي 0 = بلا فلتر: قاع قريب جدًا (2%) يبقى مقبولًا
    settings = no_ema()
    assert settings.min_stop_pct == 0
    signal = evaluate_entry_candle(_bottom(low=100.0), _row(100.0, 103.0, 99.0, 102.0), settings)
    assert signal is not None


def test_min_stop_filter_rejects_close_bottom():
    settings = no_ema(min_stop_pct=1.5)
    # المسافة 102 ← 100 = 1.96% ≥ 1.5% ⇒ مقبول
    assert evaluate_entry_candle(_bottom(low=100.0), _row(100.0, 103.0, 99.0, 102.0), settings) is not None
    # المسافة 102 ← 101 = 0.98% < 1.5% ⇒ مرفوض
    assert evaluate_entry_candle(_bottom(low=101.0), _row(100.0, 103.0, 100.5, 102.0), settings) is None


# ─────────────────────────────────────────── مسار الدقيقة
def test_path_1m_target_touch_is_win():
    target, stop = 102.0, 90.0
    df = make_1m([(100.0, 102.5, 99.0, 101.0, BASE_MS + 16 * HOUR_MS + MINUTE_MS - 1)])
    event = evaluate_path_1m(df, target, stop)
    assert event is not None
    assert event["outcome"] == "win"
    assert event["reason"] == "target"
    assert event["fill_price"] == 102.0


def test_path_1m_gap_above_target_fills_at_open():
    df = make_1m([(103.0, 104.0, 102.5, 103.5, BASE_MS + 16 * HOUR_MS + MINUTE_MS - 1)])
    event = evaluate_path_1m(df, 102.0, 90.0)
    assert event is not None and event["fill_price"] == 103.0


def test_path_1m_hour_close_below_bottom_is_loss():
    hour_end = BASE_MS + 17 * HOUR_MS - 1
    df = make_1m([(100.0, 101.0, 88.0, 89.0, hour_end)])
    event = evaluate_path_1m(df, 102.0, 90.0)
    assert event is not None
    assert event["outcome"] == "loss"
    assert event["reason"] == "close_below_bottom"
    assert event["fill_price"] == 89.0


def test_path_1m_intra_hour_close_below_bottom_is_not_loss():
    # إغلاق دقيقة تحت القاع ليس خسارة — القاعدة إغلاق شمعة الساعة
    df = make_1m([(100.0, 101.0, 88.0, 89.0, BASE_MS + 16 * HOUR_MS + MINUTE_MS - 1)])
    assert evaluate_path_1m(df, 102.0, 90.0) is None


def test_path_1m_target_wins_over_same_candle_close():
    hour_end = BASE_MS + 17 * HOUR_MS - 1
    df = make_1m([(100.0, 102.5, 88.0, 89.0, hour_end)])
    event = evaluate_path_1m(df, 102.0, 90.0)
    assert event is not None and event["outcome"] == "win"


def test_path_1m_empty_returns_none():
    assert evaluate_path_1m(pd.DataFrame(), 102.0, 90.0) is None
    assert evaluate_path_1m(None, 102.0, 90.0) is None


# ─────────────────────────────────────────── المسار الاحتياطي (1H)
def test_path_1h_target_and_loss():
    df = make_hour_df([
        (100.0, 101.0, 99.0, 100.0),
        (100.0, 103.0, 99.0, 102.0),   # لمس الهدف
        (102.0, 103.0, 89.0, 90.0),
    ])
    win = evaluate_path_1h(df, BASE_MS - HOUR_MS, 102.0, 90.0)
    assert win is not None and win["outcome"] == "win"

    loss_df = make_hour_df([
        (100.0, 101.0, 99.0, 100.0),
        (100.0, 101.0, 89.0, 89.5),
    ])
    loss = evaluate_path_1h(loss_df, BASE_MS - HOUR_MS, 102.0, 90.0)
    assert loss is not None and loss["outcome"] == "loss"
