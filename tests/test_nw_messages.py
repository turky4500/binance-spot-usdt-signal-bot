"""قوالب الرسائل: الشروط الثلاثة، الحكم الديناميكي، ونتيجة الخروج ربحًا/خسارةً."""

from __future__ import annotations

from src.nw_messages import (
    NO_RULING,
    REPAINT_WARNING,
    entry_message,
    exit_message,
    fmt_pct,
    fmt_price,
    ordinal,
    repeat_message,
    result_label,
    riyadh_time,
)

REC = {
    "confirm_close": 76366.01,
    "confirm_open": 76180.0,
    "confirm_pct": 0.244,
    "confirm_bar_time": 1_700_000_000_000,
    "bar_time": 1_700_000_000_000 - 3_600_000,
    "signal_price": 76100.0,
    "confirm_green": True,
    "color_newest": "up",
    "color_at_signal": "up",
}
POS = {"entry_price": 75000.0, "entry_time": REC["bar_time"], "entry_count": 2}


def test_ordinals():
    assert ordinal(2) == "ثانية" and ordinal(3) == "ثالثة" and ordinal(10) == "عاشرة"
    assert ordinal(11) == "رقم 11"


def test_formatters_and_result_label():
    assert fmt_price(76366.01) == "76,366.01"
    assert fmt_price(0.00001230) == "0.0000123"
    assert fmt_pct(1.234) == "+1.23%"
    assert riyadh_time(0).startswith("1970-01-01 03:00")   # UTC+3
    assert result_label(1.0) == "ربح ✅"
    assert result_label(-1.0) == "خسارة 🛑"
    assert result_label(0.0) == "تعادل ⚖️"


def test_entry_message_contains_all_user_conditions():
    text = entry_message("BTCUSDT", REC, "✅ مباح")
    assert "توصية دخول — BTCUSDT" in text
    assert "فريم الساعة (1H)" in text
    assert "شمعة التأكيد خضراء" in text
    assert "الخط أخضر" in text
    assert "الحكم الشرعي: ✅ مباح" in text
    assert REPAINT_WARNING in text
    assert REPAINT_WARNING not in entry_message("BTCUSDT", REC, "✅ مباح", show_warning=False)


def test_ruling_is_dynamic_not_hardcoded_halal():
    """«كل العملات بجميع أحكامها» ⇒ يظهر الحكم الفعلي لا «مباح» ثابت."""
    for ruling in ("⛔ غير مباح", "⚠️ مشبوه", NO_RULING):
        text = entry_message("XYZUSDT", REC, ruling)
        assert f"الحكم الشرعي: {ruling}" in text
        assert f"الحكم الشرعي: ✅ مباح" not in text
    assert NO_RULING in entry_message("XYZUSDT", REC)


def test_repeat_message_is_second_entry():
    text = repeat_message("BTCUSDT", REC, 2, "⛔ غير مباح")
    assert "إشارة دخول ثانية — BTCUSDT" in text
    assert "عدد إشارات الدخول على هذه التوصية: 2" in text
    assert "التوصية الأصلية ما زالت مفتوحة" in text
    assert "الحكم الشرعي: ⛔ غير مباح" in text
    assert "إشارة دخول ثالثة" in repeat_message("BTCUSDT", REC, 3)


def test_exit_message_reports_prices_and_realized_result():
    text = exit_message("BTCUSDT", POS, REC, 1.82, 1.62, "✅ مباح")
    assert "إشارة خروج — BTCUSDT" in text
    assert "العلامة الحمراء ▼" in text
    # سعر الدخول والخروج
    assert "📥 سعر الدخول: 75,000" in text
    assert "📤 سعر الخروج: 76,366.01" in text
    # النسبة المحققة مع تصريح ربح/خسارة
    assert "ربح ✅ +1.62%" in text
    assert "الإجمالي قبل العمولة: +1.82%" in text
    assert "الصافي المحقّق: +1.62%" in text
    assert "إشارات الدخول على التوصية: 2" in text
    assert REPAINT_WARNING in text


def test_exit_message_loss_and_breakeven():
    loss = exit_message("BTCUSDT", POS, REC, -2.10, -2.30, "⛔ غير مباح")
    assert "خسارة 🛑 -2.30%" in loss
    assert "الحكم الشرعي: ⛔ غير مباح" in loss
    even = exit_message("BTCUSDT", POS, REC, 0.0, 0.0)
    assert "تعادل ⚖️" in even
