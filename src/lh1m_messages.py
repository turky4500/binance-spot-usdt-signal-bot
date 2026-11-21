"""قوالب مستقلة لنظام L/H 1m؛ جميع الأسعار/النتائج تقديرية لا تنفيذية."""
from __future__ import annotations

from .lh1m_indicator import LEFT_BARS, RIGHT_BARS
from .nw_messages import fmt_pct, ordinal, result_label, riyadh_time
from .utils import humanize_duration_ar


def price(value: float) -> str:
    text = f"{float(value):,.12f}".rstrip("0").rstrip(".")
    if "." not in text:
        return text + ".00"
    return text + "0" if len(text.rsplit(".", 1)[-1]) == 1 else text


def _caution(event: dict, now_ms: int) -> list[str]:
    lines = [f"ℹ️ يتأكد الوسم بعد {RIGHT_BARS} شمعة؛ السعر المرجعي هو إغلاق شمعة التأكيد."]
    if int(event.get("confirmation_trades", 0)) == 0 or float(event.get("confirmation_volume", 0)) == 0:
        lines.append("⚠️ شمعة التأكيد بلا تداول؛ السعر لا يثبت إمكان التنفيذ.")
    lag_s = max(0, (int(now_ms) - int(event["confirmation_time_ms"])) // 1000)
    if lag_s >= 45:
        lines.append(f"⏱ التأخير قبل الإرسال {lag_s} ثانية على الأقل بعد التأكيد.")
    return lines


def entry(sym: str, event: dict, now_ms: int, count: int = 1) -> str:
    title = (f"📥 توصية دخول — {sym}" if count == 1
             else f"🔁 إشارة دخول {ordinal(count)} — {sym}")
    return "\n\n".join([
        title,
        f"🕐 فريم الدقيقة (1M) · Trend Lines ({LEFT_BARS}/{RIGHT_BARS}) — L",
        f"💰 إغلاق شمعة التأكيد: {price(event['confirmation_close'])}",
        "🕌 الحكم الشرعي: ✅ مباح وفق ملف الأكاديمية",
        f"🕒 وقت التأكيد: {riyadh_time(event['confirmation_time_ms'])}",
        *_caution(event, now_ms),
    ])


def exit(sym: str, event: dict, position: dict, net_pct: float,
         fee_side_pct: float, now_ms: int) -> str:
    old = position.get("entry_settings", {})
    mixed = (["ℹ️ الدخول مسجّل بإعداد 30/30 والخروج بإعداد 90/90؛ النتيجة مختلطة الإعدادات."]
             if old == {"left": 30, "right": 30} else [])
    return "\n\n".join([
        f"🔴 إشارة خروج — {sym}",
        f"🕐 فريم الدقيقة (1M) · Trend Lines ({LEFT_BARS}/{RIGHT_BARS}) — H",
        f"📥 سعر الدخول المرجعي: {price(position['entry_price'])}",
        f"📤 إغلاق شمعة التأكيد للخروج: {price(event['confirmation_close'])}",
        f"📊 النتيجة التقديرية: {result_label(net_pct)} {fmt_pct(net_pct)}",
        f"⏳ المدة: {humanize_duration_ar(position['entry_time_ms'], event['confirmation_time_ms'])}",
        "🕌 الحكم الشرعي: ✅ مباح وفق ملف الأكاديمية",
        f"🕒 وقت التأكيد: {riyadh_time(event['confirmation_time_ms'])}",
        f"ℹ️ بعد عمولة مفترضة {fee_side_pct:g}% لكل جهة، دون سبريد أو انزلاق.",
        *mixed,
        *_caution(event, now_ms),
    ])


def exit_without_entry(sym: str, event: dict, now_ms: int) -> str:
    return "\n\n".join([
        f"🔴 إشارة خروج — {sym}",
        f"🕐 فريم الدقيقة (1M) · Trend Lines ({LEFT_BARS}/{RIGHT_BARS}) — H",
        f"📤 إغلاق شمعة التأكيد: {price(event['confirmation_close'])}",
        "🕌 الحكم الشرعي: ✅ مباح وفق ملف الأكاديمية",
        f"🕒 وقت التأكيد: {riyadh_time(event['confirmation_time_ms'])}",
        "ℹ️ لا توجد توصية دخول مسجّلة لهذا الزوج في النظام؛ لم تُحسب صفقة أو نتيجة.",
        *_caution(event, now_ms),
    ])


def settings_changed(symbols: list[str]) -> str:
    lines = [
        "⚙️ تحديث إعداد Trend Lines — الدقيقة",
        f"الإعداد الحالي: Left Bars = {LEFT_BARS}، Right Bars = {RIGHT_BARS}.",
        f"تتأكد العلامة بعد إغلاق {RIGHT_BARS} شمعة تالية؛ لا تُرسل بأثر رجعي وقت القمة أو القاع.",
    ]
    if symbols:
        shown = "، ".join(symbols[:20])
        if len(symbols) > 20:
            shown += f" وغيرها ({len(symbols)} إجمالًا)"
        lines += [f"التوصيات الافتراضية السابقة مستمرة: {shown}.",
                  "تُتابع بخروج 90/90؛ ونتائجها تجمع بين إعداد الدخول السابق والخروج الجديد."]
    return "\n\n".join(lines)


def missed_exit(sym: str, confirmation_ms: int, now_ms: int) -> str:
    age = max(0, (now_ms - confirmation_ms) // 60_000)
    return "\n\n".join([
        f"⚠️ إشارة H فائتة — {sym}",
        f"تأكدت إشارة الخروج عند {riyadh_time(confirmation_ms)}؛ اكتُشفت متأخرة بنحو {age} دقيقة.",
        "راجع السعر والتوصية يدويًا. لم يُسجّل النظام خروجًا أو ربحًا افتراضيًا على سعر قديم.",
    ])


def gap_notice(last_ms: int, now_ms: int) -> str:
    gap = max(0, (now_ms - last_ms) // 60_000)
    return "\n\n".join([
        "⚠️ فجوة في متابعة نظام L/H 1M",
        f"آخر فحص محفوظ كان قبل نحو {gap} دقيقة؛ قد تكون إشارات L أو H قد فاتت.",
        "راجع التوصيات المفتوحة يدويًا؛ لا يُفترض أن جميع إشارات الخروج وصلت.",
    ])


def market_data_missing(sym: str) -> str:
    return "\n\n".join([
        f"⚠️ تعذرت متابعة الزوج المفتوح — {sym}",
        "لم تتوفر شموع دقيقة مكتملة في دورتين متتاليتين، وقد تفوت إشارة H.",
        "راجع التوصية والسعر يدويًا حتى تعود البيانات.",
    ])


def blocked_positions(symbols: list[str]) -> str:
    shown = "، ".join(symbols[:30])
    remainder = f" وغيرها ({len(symbols)} إجمالًا)" if len(symbols) > 30 else ""
    return "\n\n".join([
        "⚠️ توصيات سابقة لم تعد مستوفية للمتابعة",
        f"الأزواج: {shown}{remainder}",
        "تغيّر الحكم أو لم يعد الزوج متاحًا في السبوت. لن تُرسل إشارة H له حتى يستوفي الفلتر؛ راجع يدويًا.",
    ])


def rulings_paused(last_check_ms: int | None) -> str:
    last = riyadh_time(last_check_ms) if last_check_ms else "غير متاح أو غير مكتمل"
    return "\n\n".join([
        "⚠️ نظام L/H 1M: توقفت التوصيات مؤقتًا",
        "ملف الأحكام غير محدث أو تعذّر التحقق من صلاحيته؛ لن تُرسل إشارات L/H حتى ينجح التحديث.",
        f"🕌 آخر تحقق معروف: {last}",
        "يستأنف النظام تلقائيًا بعد وصول ملف شرعي صالح ومحدّث.",
    ])


def report(day: str, counts: dict, open_count: int, blocked_count: int,
           rulings_fresh: bool, fee_side_pct: float = 0.1) -> str:
    wins, losses = int(counts.get("wins", 0)), int(counts.get("losses", 0))
    rate = wins / (wins + losses) * 100 if wins + losses else 0.0
    lines = [
        "📊 التقرير اليومي لنظام Trend Lines (L/H 1M)",
        "",
        f"📅 اليوم: {day} (حتى 12:00 AM بتوقيت الرياض)",
        "",
        f"📥 إجمالي توصيات الدخول المرسلة: {int(counts.get('entries', 0))}",
        f"📌 مفتوحة حاليًا: {open_count}",
        f"🔒 الصفقات الافتراضية المغلقة: {int(counts.get('closed', 0))}",
        f"ℹ️ إشارات H بلا دخول مسجّل: {int(counts.get('unmatched_exits', 0))}",
        f"✅ نجاح: {wins} • 🛑 خسارة: {losses}",
        f"📈 نسبة النجاح: {rate:.1f}%",
        f"💰 مجموع نسب الربح/الخسارة التقديرية: {fmt_pct(float(counts.get('net_sum', 0.0)))}",
        "",
        f"ℹ️ النتائج تقديرية من أسعار الإغلاق، بعد عمولة مفترضة {fee_side_pct:g}% لكل جهة، وليست تنفيذًا أو عائد محفظة.",
    ]
    if counts.get("mixed_settings"):
        lines.append("⚠️ إحصاءات اليوم قد تجمع دخول 30/30 وخروج 90/90؛ ليست أداء إعداد 90 وحده.")
    if blocked_count:
        lines.append(f"⚠️ توصيات مفتوحة معلّقة بسبب الفلتر الشرعي أو بيانات قديمة: {blocked_count}")
    if not rulings_fresh:
        lines.append("⚠️ ملف الأحكام غير محدث؛ التوصيات متوقفة حتى يتجدد.")
    return "\n".join(lines)
