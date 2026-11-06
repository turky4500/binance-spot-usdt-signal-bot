"""اختبارات قرار إشارات NW 1H — شروط المستخدم بالحرف.

سلاسل جيبية مُعايرة تولّد تسمية ▲ عند الشمعة السابقة مباشرةً (bars_ago=1)
أو ▼، فيمكن اختبار «الشمعة التالية الخضراء» و«الخط الأخضر» بلا شبكة.
"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from src.nw_signals import (
    NWSignalConfig,
    candles_closed,
    confirm_green,
    evaluate_symbol,
)
from src.nw_strategy import gauss, nw_curve

N = 500
BUY_ARGS = (20, 1.01)      # ▲ عند bars_ago=1، أحدث قطعة خضراء
SELL_ARGS = (50, 2.76)     # ▼ عند bars_ago=1، أحدث قطعة حمراء


def sine_series(period, phase, confirm_delta=0.0, n=N):
    """confirm_delta < 0 ⇒ شمعة التأكيد خضراء (الإغلاق > الافتتاح)."""
    k = np.arange(n)
    c = 100 + 10 * np.sin(2 * np.pi * k / period + phase)
    o = c.copy()
    o[-1] = c[-1] + confirm_delta
    t = np.arange(n, dtype=np.int64) * 3_600_000
    return o, c, t


def test_green_confirmation_candle_is_required_for_entry():
    o, c, t = sine_series(*BUY_ARGS, confirm_delta=-1.0)
    res = evaluate_symbol(o, c, t)
    assert len(res["buy"]) == 1
    rec = res["buy"][0]
    assert rec["bars_ago"] == 1
    assert rec["confirm_green"] is True
    assert rec["color_newest"] == "up"          # حقيقة رياضية عند القاع، لكنها لم تعد شرطًا
    assert rec["color_at_signal"] == "up"
    assert rec["confirm_bar_time"] == int(t[-1])
    assert rec["bar_time"] == int(t[-2])

    # دوجي أو شمعة حمراء ⇒ لا تُرسل التوصية
    for delta in (0.0, 1.0):
        o2, c2, t2 = sine_series(*BUY_ARGS, confirm_delta=delta)
        assert evaluate_symbol(o2, c2, t2)["buy"] == []


def test_exit_uses_red_label_without_green_candle_condition():
    for delta in (-1.0, 0.0, 1.0):             # شمعة التأكيد حمراء أيضًا
        o, c, t = sine_series(*SELL_ARGS, confirm_delta=delta)
        res = evaluate_symbol(o, c, t)
        assert len(res["sell"]) == 1
        assert res["sell"][0]["bars_ago"] == 1
        assert res["sell"][0]["color_at_signal"] == "down"
        assert res["buy"] == []


def test_exit_green_confirmation_can_be_enabled_explicitly():
    o, c, t = sine_series(*SELL_ARGS, confirm_delta=1.0)
    cfg = NWSignalConfig(require_exit_confirm_green=True)
    assert evaluate_symbol(o, c, t, cfg)["sell"] == []


def test_red_line_does_not_block_entry_by_default():
    """شرطا المستخدم فقط: ▲ + شمعة تالية خضراء ⇒ تُرسل ولو كان الخط أحمر."""
    o, c, t = sine_series(*BUY_ARGS, confirm_delta=-1.0)
    fake = [{
        "bars_ago": 1, "kind": "buy", "index": N - 2, "nw": 100.0,
        "color_at_signal": "down", "color_newest": "down", "newer_all_up": False,
    }]
    assert NWSignalConfig().require_newest_green is False
    with patch("src.nw_signals.annotate_repaint_signals", return_value=fake):
        res = evaluate_symbol(o, c, t)          # الإعداد الافتراضي
        assert len(res["buy"]) == 1
        assert res["buy"][0]["color_newest"] == "down"   # سجّلناه معلومة، لا شرطًا


def test_newest_green_filter_is_honoured():
    """إن كان أحدث جزء من المنحنى أحمر، لا تُرسل توصية الدخول."""
    o, c, t = sine_series(*BUY_ARGS, confirm_delta=-1.0)
    fake = [{
        "bars_ago": 1, "kind": "buy", "index": N - 2, "nw": 100.0,
        "color_at_signal": "up", "color_newest": "down", "newer_all_up": False,
    }]
    with patch("src.nw_signals.annotate_repaint_signals", return_value=fake):
        assert evaluate_symbol(o, c, t, NWSignalConfig(require_newest_green=True))["buy"] == []
        assert len(evaluate_symbol(o, c, t, NWSignalConfig(require_newest_green=False))["buy"]) == 1


def test_retroactive_labels_are_ignored_and_counted():
    o, c, t = sine_series(*BUY_ARGS, confirm_delta=-1.0)
    res = evaluate_symbol(o, c, t, NWSignalConfig(max_age_bars=1))
    assert res["ignored_retro"] > 0            # تسميات تاريخية كثيرة بسبب إعادة الرسم
    assert all(r["bars_ago"] == 1 for r in res["buy"] + res["sell"])


def test_not_enough_bars():
    o, c, t = sine_series(*BUY_ARGS, confirm_delta=-1.0, n=100)
    res = evaluate_symbol(o, c, t)
    assert res["ok"] is False and res["reason"] == "not_enough_bars"


def test_499_bar_window_equals_500_at_the_right_edge():
    """h=8 ⇒ gauss(499,8) يصفر تمامًا؛ الـ499 شمعة تعطي نفس قيم الطرف الأيمن."""
    o, c, t = sine_series(*BUY_ARGS, confirm_delta=-1.0, n=600)
    assert gauss(499, 8.0) == 0.0
    y500 = nw_curve(c, window=500)
    y499 = nw_curve(c, window=499)
    assert np.allclose(y500[:5], y499[:5], atol=1e-12)


def test_candles_closed_excludes_unfinished_bar():
    df = pd.DataFrame({
        "open_time": [0, 3_600_000, 7_200_000],
        "open": [1.0, 2.0, 3.0],
        "close": [1.1, 2.1, 3.1],
        "close_time": [3_599_999, 7_199_999, 10_799_999],
    })
    o, c, t = candles_closed(df, now_ms=8_000_000)
    assert list(c) == [1.1, 2.1]
    assert list(t) == [0, 3_600_000]
    assert candles_closed(df, now_ms=0)[1].size == 0


def test_confirm_green_definition():
    assert confirm_green(100.0, 101.0) is True
    assert confirm_green(100.0, 100.0) is False
    assert confirm_green(100.0, 99.0) is False


def test_config_validation():
    with pytest.raises(ValueError):
        NWSignalConfig(max_age_bars=0)
    with pytest.raises(ValueError):
        NWSignalConfig(window=5)
