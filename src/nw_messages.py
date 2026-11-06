"""قوالب رسائل نظام NW 1H — **الصيغة المبسّطة** (قرار المستخدم 2026-11-06).

القالب المطلوب حرفيًا (سطر ثم سطر فارغ)، والتوقيت بنظام 12 ساعة:

    📥 توصية دخول — TRBUSDT

    🕐 فريم الساعة (1H) · Nadaraya

    💰 سعر التوصية: 15.87

    🕌 الحكم الشرعي: ✅ مباح

    🕒 2026-11-06 09:00 AM

ما حُذف من الرسائل بناءً على الطلب: سطر «شمعة التأكيد خضراء»، ونسبة الشمعة،
واسم المؤشر المطوّل، ومصدر الحكم بين قوسين، وسطر تحذير إعادة الرسم
(يبقى موثّقًا في README، ويمكن إرجاعه بـ ``NW_SHOW_REPAINT_WARNING=1``)،
وعدّاد إشارات الدخول. الشروط نفسها ما زالت **مُطبَّقة في الكود** لكنها لم تعد تُعرض.

رسالة الخروج تحتفظ بما طلبه المستخدم قبله: سعر الدخول، سعر الخروج،
والنتيجة المحققة صريحةً ربحًا أو خسارةً.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

RIYADH = ZoneInfo("Asia/Riyadh")
_ORDINALS = {
    2: "ثانية", 3: "ثالثة", 4: "رابعة", 5: "خامسة", 6: "سادسة",
    7: "سابعة", 8: "ثامنة", 9: "تاسعة", 10: "عاشرة",
}
REPAINT_WARNING = "⚠️ مؤشر Nadaraya-Watson يعيد الرسم: قد تتغيّر الإشارة بأثر رجعي"
NO_RULING = "ℹ️ لا يوجد حكم"


def riyadh_time(ms: int) -> str:
    """توقيت الرياض بنظام **12 ساعة** بصيغة YYYY-MM-DD hh:mm AM/PM."""
    dt = datetime.fromtimestamp(int(ms) / 1000, RIYADH)
    hour12 = dt.hour % 12 or 12               # 0 ⇒ 12 AM، 12 ⇒ 12 PM
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{dt:%Y-%m-%d} {hour12:02d}:{dt.minute:02d} {suffix}"


def fmt_price(value: float) -> str:
    """حتى 8 خانات للعملات الصغيرة، وبحد أدنى خانتان للأسعار الدائرية (15 ⇒ 15.00)."""
    text = f"{float(value):,.8f}".rstrip("0").rstrip(".")
    if not text:
        return "0.00"
    if "." not in text:
        return text + ".00"
    decimals = text.split(".")[1]
    return text + "0" if len(decimals) < 2 else text


def fmt_pct(value: float) -> str:
    return f"{float(value):+.2f}%"


def ordinal(count: int) -> str:
    return _ORDINALS.get(count, f"رقم {count}")


def result_label(net_pct: float) -> str:
    """تصنيف النتيجة المحققة صراحةً: ربح / خسارة / تعادل."""
    if net_pct > 0:
        return "ربح ✅"
    if net_pct < 0:
        return "خسارة 🛑"
    return "تعادل ⚖️"


def _join(*lines: str) -> str:
    """سطر ثم سطر فارغ — نفس شكل القالب الذي أرسله المستخدم."""
    return "\n\n".join(lines)


def _frame() -> str:
    return "🕐 فريم الساعة (1H) · Nadaraya"


def _ruling_line(ruling: str) -> str:
    return f"🕌 الحكم الشرعي: {ruling or NO_RULING}"


def _warning(show_warning: bool) -> list[str]:
    return [REPAINT_WARNING] if show_warning else []


def entry_message(sym: str, rec: dict, ruling: str = NO_RULING, show_warning: bool = False) -> str:
    """أول توصية دخول — القالب المبسّط بالحرف."""
    return _join(
        f"📥 توصية دخول — {sym}",
        _frame(),
        f"💰 سعر التوصية: {fmt_price(rec['confirm_close'])}",
        _ruling_line(ruling),
        f"🕒 {riyadh_time(rec['confirm_bar_time'])}",
        *_warning(show_warning),
    )


def repeat_message(
    sym: str, rec: dict, count: int, ruling: str = NO_RULING, show_warning: bool = False
) -> str:
    """تكرار العلامة الخضراء ⇒ «إشارة دخول ثانية» بنفس القالب المبسّط."""
    return _join(
        f"🔁 إشارة دخول {ordinal(count)} — {sym}",
        _frame(),
        f"💰 سعر التوصية: {fmt_price(rec['confirm_close'])}",
        _ruling_line(ruling),
        f"🕒 {riyadh_time(rec['confirm_bar_time'])}",
        *_warning(show_warning),
    )


def exit_message(
    sym: str,
    pos: dict,
    rec: dict,
    gross_pct: float,
    net_pct: float,
    ruling: str = NO_RULING,
    show_warning: bool = False,
) -> str:
    """إشارة الخروج: أسعار الدخول/الخروج + النتيجة ربحًا أو خسارةً، بالقالب المبسّط.

    ``gross_pct`` محفوظ في الدفتر ولا يُعرض لإبقاء الرسالة مختصرة؛ المعروض هو
    الصافي بعد عمولة 0.1% لكل جهة.
    """
    _ = gross_pct
    return _join(
        f"🔴 إشارة خروج — {sym}",
        _frame(),
        f"📥 سعر الدخول: {fmt_price(pos['entry_price'])}",
        f"📤 سعر الخروج: {fmt_price(rec['confirm_close'])}",
        f"📊 النتيجة: {result_label(net_pct)} {fmt_pct(net_pct)}",
        _ruling_line(ruling),
        f"🕒 {riyadh_time(rec['confirm_bar_time'])}",
        *_warning(show_warning),
    )
