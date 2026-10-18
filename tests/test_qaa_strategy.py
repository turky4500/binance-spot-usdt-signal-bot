"""اختبارات منطق نظام القاع (الاستراتيجية) — تعمل بـ pytest مباشرة.

python -m pytest tests/ -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.qaa_strategy import (  # noqa: E402
    DAY_MS,
    EMA_LEN,
    QaaSettings,
    ema,
    evaluate_entry,
    is_strict_pivot_low,
    is_timeout,
    next_stop,
    should_raise_alert,
    stop_fill,
)

BASE_MS = 1_700_000_000_000


def make_df(closes, opens=None, highs=None, lows=None):
    n = len(closes)
    opens = list(opens) if opens is not None else [float(c) for c in closes]
    highs = list(highs) if highs is not None else [max(o, c) + 1.0 for o, c in zip(opens, closes)]
    lows = list(lows) if lows is not None else [min(o, c) - 1.0 for o, c in zip(opens, closes)]
    return pd.DataFrame({
        "open_time": [BASE_MS + i * DAY_MS for i in range(n)],
        "close_time": [BASE_MS + (i + 1) * DAY_MS - 1 for i in range(n)],
        "open": [float(x) for x in opens],
        "high": [float(x) for x in highs],
        "low": [float(x) for x in lows],
        "close": [float(x) for x in closes],
    })


def entry_df(green: bool = True, deep_pivot: float = 60.0):
    """250 يومًا صاعدًا (سعر فوق EMA200) + قاع صارم عند n-2 مع شمعة تأكيد أخيرة."""
    n = 250
    closes = [100.0 + i for i in range(n)]
    opens = list(closes)
    opens[n - 1] = closes[n - 1] - 10.0 if green else closes[n - 1] + 10.0
    highs = [max(o, c) + 1.0 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 1.0 for o, c in zip(opens, closes)]
    lows[n - 2] = closes[n - 2] - deep_pivot  # القاع: أقل صراحة من3 يسار + الجار الأيمن
    return make_df(closes, opens, highs, lows)


# ─────────────────────────────────────────── القاع الصارم
def test_strict_pivot_and_ties():
    lows = [10.0, 9.0, 8.0, 7.0, 6.0, 7.0, 8.0]
    assert is_strict_pivot_low(lows, 4, 3, 1)          # أقل صراحة من الجميع
    tied = [10.0, 9.0, 8.0, 6.0, 6.0, 7.0, 8.0]       # تساوي مع جار يسارًا
    assert not is_strict_pivot_low(tied, 4, 3, 1)
    higher = [10.0, 9.0, 8.0, 7.0, 7.0, 8.0, 9.0]     # ليس أقل من جاره
    assert not is_strict_pivot_low(higher, 4, 3, 1)
    assert not is_strict_pivot_low(lows, 2, 3, 1)      # خارج النافذة يسارًا
    assert not is_strict_pivot_low(lows, 6, 3, 1)      # خارج النافذة يمينًا


# ─────────────────────────────────────────── الدخول
def test_entry_full_conditions():
    df = entry_df()
    signal = evaluate_entry(df, QaaSettings())
    assert signal is not None
    assert signal["action"] == "buy"
    assert signal["entry_price"] == 349.0              # إغلاق الشمعة الأخيرة
    assert signal["stop_price"] == 288.0               # القاع (348 - 60)
    assert signal["target_price"] == 349.0 + 2.0 * (349.0 - 288.0)  # 2R عرض
    assert signal["peak_price"] == 350.0               # قمة شمعة الإشارة
    assert signal["bar_open_time"] == BASE_MS + 249 * DAY_MS
    assert signal["stop_dist_pct"] == pytest.approx((349.0 - 288.0) / 349.0 * 100.0)
    assert signal["size_suggest_pct"] == pytest.approx(min(1.0 / signal["stop_dist_pct"], 100.0))


def test_entry_rejects_red_candle():
    assert evaluate_entry(entry_df(green=False), QaaSettings()) is None


def test_entry_rejects_below_ema():
    # قاع أعمق (180) مع إغلاق 200 — شمعة خضراء فوق القاع لكن تحت EMA200 (~248)
    df = entry_df(deep_pivot=168.0)
    n = len(df)
    df.loc[n - 1, ["open", "high", "low", "close"]] = [190.0, 201.0, 189.0, 200.0]
    assert evaluate_entry(df, QaaSettings()) is None


def test_entry_rejects_tied_pivot():
    df = entry_df()
    n = len(df)
    df.loc[n - 3, "low"] = df.loc[n - 2, "low"]        # جار مساوٍ للقاع → يُسقطه
    assert evaluate_entry(df, QaaSettings()) is None


def test_cooldown_blocks_recent_entries():
    df = entry_df()
    bar_open = int(df.iloc[-1]["open_time"])
    s = QaaSettings()
    assert evaluate_entry(df, s, last_entry_bar_open_time=bar_open - 4 * DAY_MS) is not None  # 4 شموع مسموحة
    assert evaluate_entry(df, s, last_entry_bar_open_time=bar_open - 3 * DAY_MS) is None      # 3 = مهلة غير مكتملة
    assert evaluate_entry(df, s, last_entry_bar_open_time=bar_open) is None                   # نفس اليوم


def test_same_day_exit_blocks_reentry():
    df = entry_df()
    bar_open = int(df.iloc[-1]["open_time"])
    assert evaluate_entry(df, QaaSettings(), last_exit_bar_open_time=bar_open) is None


# ─────────────────────────────────────────── الوقف المتحرك والخروج
def test_next_stop_only_rises():
    s = QaaSettings()
    assert next_stop(100.0, 50.0, s) == 100.0    # 46 < 100 → يبقى
    assert next_stop(100.0, 200.0, s) == 184.0   # 200×0.92
    assert next_stop(120.0, 200.0, s) == 184.0   # يرتفع
    assert next_stop(200.0, 200.0, s) == 200.0   # 184 < 200 → لا ينزل
    off = QaaSettings(use_trailing=False)
    assert next_stop(100.0, 500.0, off) == 100.0


def test_stop_fill_gap_rule():
    assert stop_fill(100.0, 105.0) == 100.0      # لا فجوة → عند الوقف
    assert stop_fill(100.0, 95.0) == 95.0        # فجوة أسفل → تنفيذ عند الفتح


def test_should_raise_alert_threshold():
    assert should_raise_alert(101.5, 100.0, 1.0) is True    # ≥ 1%
    assert should_raise_alert(100.5, 100.0, 1.0) is False   # دون العتبة
    assert should_raise_alert(100.5, 100.0, 0.0) is True    # 0 = أي ارتفاع
    assert should_raise_alert(100.0, 100.0, 1.0) is False   # بلا ارتفاع
    assert should_raise_alert(101.0, 0.0, 1.0) is True      # لا سجل سابق


def test_timeout_boundary():
    entry_open = BASE_MS
    assert not is_timeout(entry_open, entry_open + 39 * DAY_MS, 40)
    assert is_timeout(entry_open, entry_open + 40 * DAY_MS, 40)
    assert is_timeout(entry_open, entry_open + 41 * DAY_MS, 40)


def test_ema_matches_reference_loop():
    vals = [10.0 + (i % 7) * 0.5 for i in range(50)]
    got = ema(pd.Series(vals)).tolist()
    k = 2.0 / (EMA_LEN + 1)
    ref = [vals[0]]
    for v in vals[1:]:
        ref.append(v * k + ref[-1] * (1.0 - k))
    assert got == pytest.approx(ref)
