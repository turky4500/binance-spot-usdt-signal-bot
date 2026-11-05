"""اختبارات تحويل مؤشر LuxAlgo Nadaraya-Watson إلى Python.

الهدف: إثبات مطابقة الرياضيات للـPine، وإثبات أن نمط endpoint لا ينظر للمستقبل،
وتوثيق أن نمط repaint يعيد رسم التاريخ (لا يصلح لبوت حيّ بلا تأكيد متأخر).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from src.nw_strategy import (
    NWConfig,
    annotate_repaint_signals,
    endpoint_estimator,
    endpoint_flip_signals,
    gauss,
    nw_curve,
    nw_line_colors,
    repaint_extremum_signals,
)


def test_gauss_matches_pine():
    assert gauss(0) == pytest.approx(1.0)
    assert gauss(8) == pytest.approx(math.exp(-0.5))
    assert gauss(-3) == pytest.approx(gauss(3))
    # math.exp(-(x^2)/(h*h*2)) بالحرف
    assert gauss(5, 2.0) == pytest.approx(math.exp(-(25.0 / (4.0 * 2.0))))


def test_config_defaults():
    c = NWConfig()
    assert (c.bandwidth, c.window, c.interval, c.mode) == (8.0, 500, "1h", "endpoint")
    with pytest.raises(ValueError):
        NWConfig(bandwidth=0)
    with pytest.raises(ValueError):
        NWConfig(mode="unknown")


def test_endpoint_constant_series_and_pine_formula():
    # سلسلة ثابتة ⇒ الخارج = نفس الثابت
    closes = np.full(600, 123.45)
    out = endpoint_estimator(closes)
    assert np.isnan(out[:499]).all()
    assert out[499:] == pytest.approx(123.45)

    # مطابقة الصيغة الحرفية: sum(src[i]*gauss(i,h)) / sum(gauss(i,h))
    rng = np.random.default_rng(7)
    closes = 100 + np.cumsum(rng.normal(0, 0.5, 700))
    out = endpoint_estimator(closes, h=8.0, window=500)
    t = 650
    w = np.array([math.exp(-(i * i) / (8.0 * 8.0 * 2.0)) for i in range(500)])
    manual = sum(closes[t - i] * w[i] for i in range(500)) / w.sum()
    assert out[t] == pytest.approx(manual, rel=1e-12)


def test_endpoint_is_causal_no_lookahead():
    rng = np.random.default_rng(11)
    base = 100 + np.cumsum(rng.normal(0, 0.4, 800))
    out_a = endpoint_estimator(base)
    extended = np.concatenate([base, rng.normal(50, 20, 50)])  # «مستقبل» عشوائي
    out_b = endpoint_estimator(extended)
    assert np.allclose(out_a, out_b[: len(base)], equal_nan=True)


def test_curve_first_point_equals_endpoint():
    """y[0] في وضع repaint = out في وضع endpoint (W[0,j] = gauss(-j) = gauss(j))."""
    rng = np.random.default_rng(13)
    closes = 100 + np.cumsum(rng.normal(0, 0.3, 500))
    y = nw_curve(closes)
    out = endpoint_estimator(closes)
    assert y[0] == pytest.approx(out[-1], rel=1e-12)
    assert len(y) == 500


def test_repaint_labels_mark_troughs_and_peaks():
    # y[0] = الأحدث؛ قاع عند i=4 ⇒ ▲ شراء، قمة عند i=8 ⇒ ▼ بيع
    y = np.array([5, 4, 3, 2, 1, 2, 3, 4, 5, 4, 3], dtype=float)
    closes = np.arange(100, 111, dtype=float)
    sigs = repaint_extremum_signals(y, closes)
    assert [(s["bars_ago"], s["kind"]) for s in sigs] == [(4, "buy"), (8, "sell")]
    assert sigs[0]["nw"] == pytest.approx(1.0)
    assert sigs[0]["price"] == pytest.approx(closes[-1 - 4])
    assert sigs[0]["index"] == pytest.approx(len(closes) - 1 - 4)


def test_repaint_labels_ignore_flat_slope():
    y = np.array([3, 2, 2, 2, 3, 4], dtype=float)
    assert repaint_extremum_signals(y) == []


def test_endpoint_flip_signals_are_lagged_by_one_bar():
    out = np.array([5, 4, 3, 2, 1, 2, 3, 4, 5, 4, 3], dtype=float)
    closes = np.arange(100, 111, dtype=float)
    sigs = endpoint_flip_signals(out, closes)
    assert [(s["index"], s["extremum_index"], s["kind"]) for s in sigs] == [
        (5, 4, "buy"),
        (9, 8, "sell"),
    ]
    assert sigs[0]["price"] == pytest.approx(closes[5])
    assert sigs[0]["extremum_price"] == pytest.approx(closes[4])


def test_endpoint_flip_signals_skip_warmup_nans():
    out = np.concatenate([np.full(499, np.nan), np.array([3.0, 2.0, 1.0, 2.0])])
    sigs = endpoint_flip_signals(out)
    assert len(sigs) == 1 and sigs[0]["kind"] == "buy"


def test_repainting_documented_behaviour():
    """إضافة شمعة واحدة تغيّر تاريخ المنحنى/الإشارات — هذه هي مشكلة إعادة الرسم."""
    rng = np.random.default_rng(21)
    base = 100 + np.cumsum(rng.normal(0, 0.6, 520))
    y_before = nw_curve(base[:500])
    y_after = nw_curve(base[:501])
    # نفس الشمعة التاريخية (i=10 شمعة ماضية من نهاية كل نافذة) لا تعطي نفس القيمة
    assert y_before[10] != pytest.approx(y_after[11])
    # الإشارات التاريخية تتحرك: لا يجوز استخدامها كإشارة حيّة بلا تأكيد
    s_before = {(s["bars_ago"], s["kind"]) for s in repaint_extremum_signals(y_before)}
    s_after = {(s["bars_ago"], s["kind"]) for s in repaint_extremum_signals(y_after)}
    assert s_before != s_after


def test_line_colors_match_pine_teal_red_rule():
    # y[0]=الأحدث؛ d>0 ⇒ أحمر (down)، d<=0 ⇒ أخضر/تركوازي (up)
    y = np.array([5, 4, 3, 2, 1, 2, 3, 4, 5, 4, 3], dtype=float)
    c = nw_line_colors(y)
    assert c[0] is None
    assert list(c[1:5]) == ["up", "up", "up", "up"]      # المنحدر نحو القاع أخضر
    assert list(c[5:9]) == ["down", "down", "down", "down"]
    assert c[9] == "up" and c[10] == "up"


def test_line_color_at_buy_signal_is_green_by_definition():
    y = np.array([5, 4, 3, 2, 1, 2, 3, 4, 5, 4, 3], dtype=float)
    sigs = annotate_repaint_signals(y)
    buy = next(s for s in sigs if s["kind"] == "buy")
    assert buy["bars_ago"] == 4
    assert buy["color_at_signal"] == "up"     # «الخط أخضر» عند ▲ تحصيل حاصل
    assert buy["color_newest"] == "up"
    assert buy["newer_all_up"] is True
    sell = next(s for s in sigs if s["kind"] == "sell")
    assert sell["color_at_signal"] == "down"


def test_insufficient_bars_raises():
    with pytest.raises(ValueError):
        endpoint_estimator(np.arange(100, dtype=float))
    with pytest.raises(ValueError):
        nw_curve(np.arange(100, dtype=float))
