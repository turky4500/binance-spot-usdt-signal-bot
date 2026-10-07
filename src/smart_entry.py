"""الدخول الذكي: بوابة زمنية مُتحقَّق منها + ترتيب وحد أقصى للصفقات المتزامنة.

نتائج القياس على 495 عملة / 78,637 صفقة (فريم الساعة، 2000 شمعة لكل عملة):

| البوابة | نجاح | متوسط/صفقة | t | خارج العينة |
|---|---|---|---|---|
| بدون فلتر | 51.7% | −0.1139% | −15.91 | −0.0980% |
| **نافذة شموع 00:00/01:00/02:00 الرياض** | **58.6%** | **+0.1559%** | **+7.25** | **+0.1336% (t=+4.37)** |
| النافذة + ارتداد EMA | 55.2% | +0.0356% | +0.99 | +0.0935% |
| النافذة + زخم RSI | 53.1% | −0.0188% | −0.69 | +0.0638% |
| النافذة + EMA stack | 53.9% | −0.0429% | −1.32 | −0.1210% |
| النافذة + MACD | 51.1% | −0.0717% | −2.54 | −0.0408% |
| النافذة + ترند+زخم | 52.2% | −0.1022% | −3.32 | −0.1279% |

**الخلاصة:** البوابة الزمنية وحدها هي التي تضيف قيمة — وهي الأفضل داخل النافذة أيضًا
وفي خارج العينة. مؤشرات الزخم والمتوسطات على هذا الفريم **تُقلّل** النتيجة، لذا
تُحسب هنا كـ«درجة جودة» للتسجيل والترتيب فقط، ولا تُمنع بها أي صفقة.

**التسمية:** النافذة تُحدَّد بساعة شمعة الإشارة (شمعة الساعة 00:00 تُغلق عند 01:00)،
لأن القياس صُنِّف الصفقات بنفس الطريقة — فتطابق البوابة والقياس 100%.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _parse_hours(raw: str) -> list[int]:
    hours: list[int] = []
    for chunk in raw.replace(" ", "").split(","):
        if not chunk:
            continue
        try:
            value = int(chunk)
        except ValueError:
            continue
        if 0 <= value <= 23:
            hours.append(value)
    return sorted(set(hours))


# القائمة المعتمدة (اختيار المستخدم): توازن بين عدد الرسائل وحجم الحافة —
# 56.3% نجاح و+0.0710%/صفقة، ومُتحقَّق منها خارج العينة (t=+3.15).
DEFAULT_ENTRY_HOURS = "0,2,5,6,14,21,23"
# البديل الأضيق: أعلى أثرًا (58.6% و+0.1559%، t=+7.25، خارج العينة t=+4.37) وأقل رسائل بكثير.
NARROW_ENTRY_HOURS = "0,1,2"


# متوسط نتيجة الصفقة لكل ساعة من قياس 78,637 صفقة (495 عملة، فريم الساعة، توقيت الرياض)
# المفتاح: الساعة المحلية → (متوسط الصافي %, الدلالة t, عدد الصفقات)
HOUR_STATS: dict[int, tuple[float, float, int]] = {
    0: (0.327, 9.65, 3301),   1: (-0.010, -0.24, 2455),  2: (0.099, 2.60, 2758),
    3: (-0.283, -7.35, 2782), 4: (-0.102, -2.76, 2910),  5: (0.113, 2.78, 2358),
    6: (0.017, 0.51, 3715),   7: (-0.069, -2.06, 3566),  8: (-0.192, -6.25, 4226),
    9: (-0.308, -8.21, 2838), 10: (-0.249, -6.32, 2612), 11: (-0.303, -7.81, 2738),
    12: (-0.229, -5.89, 2751), 13: (-0.239, -6.95, 3357), 14: (-0.022, -0.70, 4021),
    15: (-0.171, -4.86, 3269), 16: (-0.303, -8.04, 2975), 17: (-0.208, -5.41, 2801),
    18: (-0.040, -1.26, 3965), 19: (-0.388, -14.08, 5008), 20: (-0.142, -5.06, 5094),
    21: (-0.088, -2.67, 3618), 22: (0.092, 2.61, 3195),  23: (0.127, 3.04, 2324),
}

VERDICT_AR = {"strong": "قوية", "normal": "متوسطة", "weak": "ضعيفة"}
VERDICT_EMOJI = {"strong": "🟢", "normal": "🟡", "weak": "🔴"}

REJECTION_REASONS_AR = {
    "outside_time_window": "خارج النافذة الزمنية المعتمدة",
    "concurrency_cap": "تجاوز الحد الأقصى للصفقات المتزامنة",
}


@dataclass(slots=True)
class SmartEntrySettings:
    # افتراضيًا: البوابة الزمنية معطّلة (تشغيل 24 ساعة) والسقف مفتوح —
    # قرار المستخدم: لا يريد تفويت أي إشارة، ولا مشكلة عنده من كثرة الرسائل.
    # كل قياسات الحافة الزمنية محفوظة في التقارير، ويمكن تفعيلها بسطر واحد.
    enabled: bool = field(default_factory=lambda: _env_bool("SMART_ENTRY", False))
    allowed_hours: list[int] = field(default_factory=lambda: _parse_hours(os.getenv("ENTRY_HOURS", DEFAULT_ENTRY_HOURS)))
    max_open_trades: int = field(default_factory=lambda: _env_int("MAX_OPEN_TRADES", 0))  # 0 = بلا حد
    timezone_name: str = field(default_factory=lambda: os.getenv("TIMEZONE", "Asia/Riyadh"))

    @property
    def _tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def local_hour(self, time_ms: int) -> int:
        return datetime.fromtimestamp(int(time_ms) / 1000, tz=timezone.utc).astimezone(self._tz).hour

    def is_hour_allowed(self, time_ms: int) -> bool:
        if not self.enabled or not self.allowed_hours:
            return True
        return self.local_hour(time_ms) in self.allowed_hours

    def hours_text(self) -> str:
        if not self.allowed_hours:
            return "كل الساعات"
        return " • ".join(f"{h:02d}:00" for h in self.allowed_hours)

    def describe(self) -> str:
        window = (
            "كل الساعات (24 ساعة)" if not self.enabled or not self.allowed_hours
            else f"النافذة الزمنية: {self.hours_text()} (توقيت {self.timezone_name})"
        )
        cap = f"حد أقصى {int(self.max_open_trades)} صفقة متزامنة" if self.has_open_trade_cap else "بلا حد لعدد الصفقات المتزامنة"
        return f"{window} • {cap}"

    @staticmethod
    def hour_verdict(hour: int) -> str | None:
        """تصنيف الساعة تاريخيًا من القياس: strong / normal / weak."""
        stats = HOUR_STATS.get(int(hour))
        if not stats:
            return None
        avg, t, _ = stats
        if avg > 0 and t >= 2.0:
            return "strong"
        if t <= -2.5:
            return "weak"
        return "normal"

    def hour_note_ar(self, hour: int) -> str | None:
        """سطر جاهز للرسالة: تقييم الساعة تاريخيًا (ليس توصية، بل رقم مقيس)."""
        stats = HOUR_STATS.get(int(hour))
        verdict = self.hour_verdict(hour)
        if not stats or verdict is None:
            return None
        avg, _t, n = stats
        return (
            f"⏰ تقييم الساعة (تاريخيًا): {VERDICT_EMOJI[verdict]} {VERDICT_AR[verdict]}"
            f" — متوسط {avg:+.3f}% على {n:,} صفقة سابقة"
        )

    @property
    def has_open_trade_cap(self) -> bool:
        return int(self.max_open_trades) > 0

    def quality_score(self, metrics: dict | None, buy_score: int | None = None) -> int:
        """درجة جودة للتسجيل والترتيب — كل تأكيد = نقطة. لا تُستخدم لمنع دخول."""
        metrics = metrics or {}
        score = 0
        if buy_score is not None and buy_score >= 3:
            score += 1
        plus_di, minus_di = metrics.get("plus_di"), metrics.get("minus_di")
        if plus_di is not None and minus_di is not None and plus_di > minus_di:
            score += 1
        adx = metrics.get("adx")
        if adx is not None and adx >= 25:
            score += 1
        rsi = metrics.get("rsi")
        if rsi is not None and 45 <= rsi <= 65:
            score += 1
        rvol = metrics.get("relative_volume")
        if rvol is not None and 1.2 <= rvol <= 2.5:
            score += 1
        return score

    def sort_key(self, candidate: dict) -> tuple:
        """ترتيب المرشحين داخل نفس الشمعة: درجة الجودة ثم سيولة الحجم النسبي."""
        signal = candidate.get("signal", {})
        metrics = signal.get("metrics", {}) or {}
        return (
            -self.quality_score(metrics, signal.get("buy_score")),
            -float(metrics.get("relative_volume") or 0.0),
            str(candidate.get("symbol", "")),
        )

    def select(self, candidates: list[dict], open_trades_count: int) -> tuple[list[dict], list[dict]]:
        """يرجّع (المقبولون، المرفوضون لحد التزامن)."""
        ranked = sorted(candidates, key=self.sort_key)
        if not self.has_open_trade_cap:
            return ranked, []
        slots = max(0, int(self.max_open_trades) - int(open_trades_count))
        return ranked[:slots], ranked[slots:]
