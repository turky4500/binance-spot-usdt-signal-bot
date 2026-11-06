"""القالب المبسّط للمستخدم: أسطر منفصلة + توقيت 12 ساعة + لا تفاصيل زائدة."""

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
    "confirm_close": 15.87,
    "confirm_open": 15.60,
    "confirm_pct": 1.73,
    "confirm_bar_time": 1_700_000_000_000,   # 2023-11-14 22:13 UTC ⇒ 01:13 AM الرياض
    "bar_time": 1_700_000_000_000 - 3_600_000,
    "signal_price": 15.50,
    "confirm_green": True,
    "color_newest": "up",
    "color_at_signal": "up",
}
POS = {"entry_price": 15.00, "entry_time": REC["bar_time"], "entry_count": 2}


def test_ordinals():
    assert ordinal(2) == "ثانية" and ordinal(3) == "ثالثة" and ordinal(10) == "عاشرة"
    assert ordinal(11) == "رقم 11"


def test_formatters_and_result_label():
    assert fmt_price(15.87) == "15.87"
    assert fmt_price(15.00) == "15.00"
    assert fmt_price(16.10) == "16.10"
    assert fmt_price(0.003987) == "0.003987"
    assert fmt_pct(1.234) == "+1.23%"
    assert result_label(1.0) == "ربح ✅"
    assert result_label(-1.0) == "خسارة 🛑"
    assert result_label(0.0) == "تعادل ⚖️"


def test_time_is_12_hour_with_am_pm():
    # 1970-01-01 00:00 UTC = 03:00 صباحًا بالرياض
    assert riyadh_time(0) == "1970-01-01 03:00 AM"
    # منتصف الليل بالرياض 1970-01-01 = 1969-12-31 21:00 UTC
    assert riyadh_time(-3 * 3_600_000) == "1970-01-01 12:00 AM"
    # الظهر بالرياض = 09:00 UTC
    assert riyadh_time(9 * 3_600_000) == "1970-01-01 12:00 PM"
    # مساءً
    assert riyadh_time(15 * 3_600_000) == "1970-01-01 06:00 PM"


def test_entry_message_matches_the_users_template_exactly():
    text = entry_message("TRBUSDT", REC, "✅ مباح")
    assert text == "\n\n".join([
        "📥 توصية دخول — TRBUSDT",
        "🕐 فريم الساعة (1H) · Nadaraya",
        "💰 سعر التوصية: 15.87",
        "🕌 الحكم الشرعي: ✅ مباح",
        "🕒 2023-11-15 01:13 AM",
    ])
    # لا تفاصيل زائدة ولا ادعاءات قديمة
    for gone in ("شمعة التأكيد", "الخط أخضر", "لون الخط", "📈", "LuxAlgo",
                 "أكاديمية البيتكوين", "أهداف", "الوقف", REPAINT_WARNING):
        assert gone not in text, gone
    assert "AM" in text or "PM" in text


def test_warning_can_be_re_enabled_without_changing_the_rest():
    text = entry_message("TRBUSDT", REC, "✅ مباح", show_warning=True)
    assert REPAINT_WARNING in text
    assert "🕐 فريم الساعة (1H) · Nadaraya" in text


def test_ruling_is_dynamic_not_hardcoded_halal():
    for ruling in ("⛔ غير مباح", "⚠️ مشبوه", NO_RULING):
        text = entry_message("XYZUSDT", REC, ruling)
        assert f"🕌 الحكم الشرعي: {ruling}" in text
        assert "الحكم الشرعي: ✅ مباح" not in text
    assert NO_RULING in entry_message("XYZUSDT", REC)


def test_repeat_message_uses_the_same_simplified_template():
    text = repeat_message("TRBUSDT", REC, 2, "✅ مباح")
    assert text == "\n\n".join([
        "🔁 إشارة دخول ثانية — TRBUSDT",
        "🕐 فريم الساعة (1H) · Nadaraya",
        "💰 سعر التوصية: 15.87",
        "🕌 الحكم الشرعي: ✅ مباح",
        "🕒 2023-11-15 01:13 AM",
    ])
    assert "إشارة دخول ثالثة" in repeat_message("TRBUSDT", REC, 3)


def test_exit_message_keeps_prices_and_result_in_simplified_form():
    text = exit_message("TRBUSDT", POS, REC, 5.80, 5.60, "✅ مباح")
    assert text == "\n\n".join([
        "🔴 إشارة خروج — TRBUSDT",
        "🕐 فريم الساعة (1H) · Nadaraya",
        "📥 سعر الدخول: 15.00",
        "📤 سعر الخروج: 15.87",
        "📊 النتيجة: ربح ✅ +5.60%",
        "🕌 الحكم الشرعي: ✅ مباح",
        "🕒 2023-11-15 01:13 AM",
    ])
    assert "سعر الدخول" in text and "سعر الخروج" in text
    assert REPAINT_WARNING not in text


def test_exit_message_loss_and_breakeven():
    loss = exit_message("TRBUSDT", POS, REC, -2.10, -2.30, "⛔ غير مباح")
    assert "خسارة 🛑 -2.30%" in loss
    assert "🕌 الحكم الشرعي: ⛔ غير مباح" in loss
    even = exit_message("TRBUSDT", POS, REC, 0.0, 0.0)
    assert "تعادل ⚖️" in even
