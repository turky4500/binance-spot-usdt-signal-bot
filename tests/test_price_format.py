"""تنسيق الأسعار بدقّة tickSize — مطابقة كتابة Binance تمامًا."""
from __future__ import annotations

from src.utils import format_price


def test_btc_like_tick_rounds_to_pair_precision():
    # BTC: tick=0.01 — الهدف المحسوب يجب أن يُكتب بخانتين مثل Binance
    assert format_price(89400.572255, "0.01000000") == "89400.57"


def test_sui_like_tick_four_decimals():
    assert format_price(1.20045975, "0.00010000") == "1.2005"
    assert format_price(1.1235, "0.00010000") == "1.1235"


def test_pepe_like_tick_eight_decimals():
    assert format_price(0.000004463, "0.00000001") == "0.00000446"


def test_entry_price_unchanged_when_already_tick_aligned():
    # أسعار الدخول (من klines) بدقّة tick — لا تتغيّر
    assert format_price(0.1278, "0.00010000") == "0.1278"
    assert format_price(83669.23, "0.01000000") == "83669.23"
    assert format_price(0.00000417, "0.00000001") == "0.00000417"


def test_rounding_half_up():
    assert format_price(1.20045, "0.0001") == "1.2005"
    assert format_price(1.20044999, "0.0001") == "1.2004"


def test_zero_decimal_tick():
    assert format_price(123.6, "1") == "124"
    assert format_price(123.0, "1") == "123"


def test_float_tick_accepted():
    assert format_price(89400.572255, 0.01) == "89400.57"


def test_float_scientific_tick_accepted():
    # JSON قد يعطي tick كـfloat مثل 1e-08
    assert format_price(4.463e-06, 1e-08) == "0.00000446"


def test_fallback_without_tick_matches_old_behavior():
    assert format_price(1.2300000000000002) == "1.23"
    assert format_price(0.00000417) == "0.00000417"
    assert format_price(100.0) == "100"


def test_invalid_tick_falls_back_to_old_behavior():
    assert format_price(1.5, "garbage") == "1.5"


def test_none_value_returns_dash():
    assert format_price(None) == "—"
    assert format_price(None, "0.01") == "—"
