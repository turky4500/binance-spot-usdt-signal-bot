"""قوالب رسائل نظام NW 1H — عربية، رسالة واحدة لكل حدث، بلا وعود ربح."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

RIYADH = ZoneInfo("Asia/Riyadh")
_ORDINALS = {
    2: "ثانية", 3: "ثالثة", 4: "رابعة", 5: "خامسة", 6: "سادسة",
    7: "سابعة", 8: "ثامنة", 9: "تاسعة", 10: "عاشرة",
}
REPAINT_WARNING = "⚠️ مؤشر Nadaraya-Watson يعيد الرسم: قد تتغيّر الإشارة بأثر رجعي"


def riyadh_time(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, RIYADH).strftime("%Y-%m-%d %H:%M")


def fmt_price(value: float) -> str:
    text = f"{float(value):,.8f}".rstrip("0").rstrip(".")
    return text or "0"


def fmt_pct(value: float) -> str:
    return f"{float(value):+.2f}%"


def ordinal(count: int) -> str:
    return _ORDINALS.get(count, f"رقم {count}")


def _common(rec: dict, show_warning: bool) -> list[str]:
    lines = [
        f"💰 سعر التوصية: {fmt_price(rec['confirm_close'])}",
        "✅ شمعة التأكيد خضراء: "
        f"{fmt_pct(rec['confirm_pct'])} ({fmt_price(rec['confirm_open'])} ← {fmt_price(rec['confirm_close'])})",
        "📈 الخط أخضر: المنحنى صاعد عند الإرسال",
        "🕌 الحكم الشرعي: مباح (مصدر أكاديمية البيتكوين العربية)",
        f"🕒 {riyadh_time(rec['confirm_bar_time'])} بتوقيت الرياض",
    ]
    if show_warning:
        lines.append(REPAINT_WARNING)
    return lines


def entry_message(sym: str, rec: dict, show_warning: bool = True) -> str:
    """أول توصية دخول (لا توجد توصية مفتوحة لهذا الزوج)."""
    return "\n".join([
        f"📥 توصية دخول — {sym}",
        "🕐 فريم الساعة (1H) · Nadaraya-Watson [LuxAlgo] · h=8",
        *_common(rec, show_warning),
    ])


def repeat_message(sym: str, rec: dict, count: int, show_warning: bool = True) -> str:
    """تكرار العلامة الخضراء ⇒ «إشارة دخول ثانية» (ثم ثالثة/رابعة…)."""
    return "\n".join([
        f"🔁 إشارة دخول {ordinal(count)} — {sym}",
        "🕐 فريم الساعة (1H) · Nadaraya-Watson [LuxAlgo] · h=8",
        *_common(rec, show_warning),
        f"🔢 عدد إشارات الدخول على هذه التوصية: {count}",
        "ℹ️ التوصية الأصلية ما زالت مفتوحة (لا أهداف ولا وقف)",
    ])


def exit_message(sym: str, pos: dict, rec: dict, net_pct: float, show_warning: bool = True) -> str:
    lines = [
        f"🔴 إشارة خروج — {sym}",
        "📉 ظهور العلامة الحمراء ▼ (قمة المنحنى)",
        f"💰 الدخول: {fmt_price(pos['entry_price'])} ← الخروج: {fmt_price(rec['confirm_close'])}",
        f"📊 الصافي: {fmt_pct(net_pct)} (بعد 0.1% عمولة لكل جهة)",
        f"🔢 إشارات الدخول على التوصية: {pos.get('entry_count', 1)}",
        f"🕒 {riyadh_time(rec['confirm_bar_time'])} بتوقيت الرياض",
    ]
    if show_warning:
        lines.append(REPAINT_WARNING)
    return "\n".join(lines)
