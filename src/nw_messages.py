"""قوالب رسائل نظام NW 1H — عربية، رسالة واحدة لكل حدث، بلا وعود ربح.

تغييرات 2026-11-06 (بأمر المستخدم):
  * «الإرسال يشمل كل العملات بجميع أحكامها» ⇒ الحكم الشرعي **ديناميكي**
    (مباح / غير مباح / مشبوه / لا يوجد حكم) بدل سطر «مباح» الثابت.
  * رسالة الخروج تذكر صراحة: سعر الدخول، سعر الخروج، والنسبة المحققة
    مع تصنيف **ربح ✅ / خسارة 🛑 / تعادل ⚖️**، والإجمالي قبل العمولة.
  * **إلغاء شرط «الخط الأخضر» (2026-11-06):** حُذفت جملة «📈 الخط أخضر»
    من كل الرسائل. لون الخط لم يعد شرطًا، ولا يجوز ادعاؤه في رسالة تُرسل
    للمستخدم وهو يرى الخط أحمر على TradingView.
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
COMMISSION_NOTE = "0.1% لكل جهة"


def riyadh_time(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, RIYADH).strftime("%Y-%m-%d %H:%M")


def fmt_price(value: float) -> str:
    text = f"{float(value):,.8f}".rstrip("0").rstrip(".")
    return text or "0"


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


def _common(rec: dict, ruling: str, show_warning: bool) -> list[str]:
    lines = [
        f"💰 سعر التوصية: {fmt_price(rec['confirm_close'])}",
        "✅ شمعة التأكيد خضراء: "
        f"{fmt_pct(rec['confirm_pct'])} ({fmt_price(rec['confirm_open'])} ← {fmt_price(rec['confirm_close'])})",
        f"🕌 الحكم الشرعي: {ruling or NO_RULING} (أكاديمية البيتكوين العربية)",
        f"🕒 {riyadh_time(rec['confirm_bar_time'])} بتوقيت الرياض",
    ]
    if show_warning:
        lines.append(REPAINT_WARNING)
    return lines


def entry_message(sym: str, rec: dict, ruling: str = NO_RULING, show_warning: bool = True) -> str:
    """أول توصية دخول (لا توجد توصية مفتوحة لهذا الزوج)."""
    return "\n".join([
        f"📥 توصية دخول — {sym}",
        "🕐 فريم الساعة (1H) · Nadaraya-Watson [LuxAlgo] · h=8",
        *_common(rec, ruling, show_warning),
    ])


def repeat_message(sym: str, rec: dict, count: int, ruling: str = NO_RULING, show_warning: bool = True) -> str:
    """تكرار العلامة الخضراء ⇒ «إشارة دخول ثانية» (ثم ثالثة/رابعة…)."""
    return "\n".join([
        f"🔁 إشارة دخول {ordinal(count)} — {sym}",
        "🕐 فريم الساعة (1H) · Nadaraya-Watson [LuxAlgo] · h=8",
        *_common(rec, ruling, show_warning),
        f"🔢 عدد إشارات الدخول على هذه التوصية: {count}",
        "ℹ️ التوصية الأصلية ما زالت مفتوحة (لا أهداف ولا وقف)",
    ])


def exit_message(
    sym: str,
    pos: dict,
    rec: dict,
    gross_pct: float,
    net_pct: float,
    ruling: str = NO_RULING,
    show_warning: bool = True,
) -> str:
    """إشارة الخروج: سعرا الدخول والخروج + النسبة المحققة ربحًا أو خسارةً."""
    lines = [
        f"🔴 إشارة خروج — {sym}",
        "📉 ظهور العلامة الحمراء ▼ (قمة المنحنى)",
        f"📥 سعر الدخول: {fmt_price(pos['entry_price'])}",
        f"📤 سعر الخروج: {fmt_price(rec['confirm_close'])}",
        f"📊 النتيجة: {result_label(net_pct)} {fmt_pct(net_pct)} (صافي بعد عمولة {COMMISSION_NOTE})",
        f"   ├ الإجمالي قبل العمولة: {fmt_pct(gross_pct)}",
        f"   └ الصافي المحقّق: {fmt_pct(net_pct)}",
        f"🕌 الحكم الشرعي: {ruling or NO_RULING} (أكاديمية البيتكوين العربية)",
        f"🔢 إشارات الدخول على التوصية: {pos.get('entry_count', 1)}",
        f"🕒 {riyadh_time(rec['confirm_bar_time'])} بتوقيت الرياض",
    ]
    if show_warning:
        lines.append(REPAINT_WARNING)
    return "\n".join(lines)
