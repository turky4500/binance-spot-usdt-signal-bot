from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

WEEKDAY_AR = {
    0: "الاثنين",
    1: "الثلاثاء",
    2: "الأربعاء",
    3: "الخميس",
    4: "الجمعة",
    5: "السبت",
    6: "الأحد",
}


def format_price(value: float, tick_size: str | float | None = None) -> str:
    """تنسيق سعر كما تكتبه Binance تمامًا.

    tick_size (من PRICE_FILTER في exchangeInfo) يجعل الرقم مطابقًا للزوج:
    تقريب لأقرب tick وكتابة بعدد خاناته — مثل 89400.57 للـBTC (tick=0.01)
    بدل 89400.572255 الناتج عن float.
    بدون tick_size يعود للسلوك القديم (8 خانات بلا أصفار زائدة).
    """
    if value is None:
        return "—"
    if tick_size:
        try:
            tick = Decimal(str(tick_size)).normalize()
            decimals = max(0, -tick.as_tuple().exponent)
            quantized = Decimal(str(float(value))).quantize(tick, rounding=ROUND_HALF_UP)
            return f"{quantized:.{decimals}f}"
        except Exception:  # noqa: BLE001 — أي قيمة غير صالحة تعود للتنسيق القديم
            pass
    formatted = f"{value:.8f}".rstrip("0").rstrip(".")
    return formatted or "0"


def ms_to_local_text(ms: int, tz_name: str) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz_name))
    period = "مساءً" if dt.hour >= 12 else "صباحًا"
    hour12 = dt.hour % 12
    if hour12 == 0:
        hour12 = 12
    return f"{dt:%Y-%m-%d} {hour12}:{dt:%M:%S} {period}"


def _arabic_unit(n: int, singular: str, dual: str, plural: str, singular_after_ten: str | None = None) -> str:
    if n == 1:
        return singular
    if n == 2:
        return dual
    if 3 <= n <= 10:
        return f"{n} {plural}"
    return f"{n} {singular_after_ten or singular}"


def humanize_duration_ar(start_ms: int, end_ms: int) -> str:
    total_seconds = max(int((end_ms - start_ms) / 1000), 0)
    total_minutes = total_seconds // 60
    if total_minutes <= 0:
        return "أقل من دقيقة"

    days, rem_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem_minutes, 60)

    parts: list[str] = []
    if days:
        parts.append(_arabic_unit(days, "يوم واحد", "يومان", "أيام", "يومًا"))
    if hours:
        parts.append(_arabic_unit(hours, "ساعة واحدة", "ساعتان", "ساعات", "ساعة"))
    if minutes:
        parts.append(_arabic_unit(minutes, "دقيقة واحدة", "دقيقتان", "دقائق", "دقيقة"))

    return " و ".join(parts[:2]) if parts else "أقل من دقيقة"


def local_date_key_from_ms(ms: int, tz_name: str) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz_name))
    return dt.strftime("%Y-%m-%d")


def weekday_ar_from_ms(ms: int, tz_name: str) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz_name))
    return WEEKDAY_AR.get(dt.weekday(), "")


def local_hour_from_ms(ms: int, tz_name: str) -> int:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz_name)).hour
