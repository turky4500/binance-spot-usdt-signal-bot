"""الدخول الذكي: بوابة زمنية مُتحقَّق منها + ترتيب وحد أقصى للصفقات المتزامنة.

نتائج القياس على 495 عملة / 78,637 صفقة (فريم الساعة، 2000 شمعة لكل عملة):

| البوابة | نجاح | متوسط/صفقة | t |
|---|---|---|---|
| بدون فلتر | 51.7% | −0.1139% | −15.91 |
| **نافذة 00:00–03:00 الرياض** | **54.3%** | **+0.0821%** | **+7.58** |
| النافذة + زخم RSI | 51.2% | −0.0267% | −1.74 |
| النافذة + EMA stack | 51.8% | −0.0424% | −2.42 |
| النافذة + MACD | 50.1% | −0.0477% | −2.80 |
| النافذة + زخم كامل | 49.1% | −0.0633% | −2.45 |

**الخلاصة:** البوابة الزمنية وحدها هي التي تضيف قيمة. مؤشرات الزخم والمتوسطات —
على هذا الفريم وفي هذه الفترة — **تُقلّل** النتيجة عندما تُستخدم كبوابات.
لذلك تُحسب هنا كـ«درجة جودة» للتسجيل والقياس فقط، ولا تُمنع بها الصفقات.
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


# النافذة الزمنية المُتحقَّق منها خارج العينة (اختيار من النصف الأول، اختبار على الثاني)
DEFAULT_ENTRY_HOURS = "0,1,2"
# مجموعة أوسع مُتحقَّق منها أيضًا (t=+3.15 خارج العينة) لكن أضعف من النافذة الضيقة
WIDE_ENTRY_HOURS = "0,2,5,6,14,21,23"

REJECTION_REASONS_AR = {
    "outside_time_window": "خارج النافذة الزمنية المعتمدة",
    "concurrency_cap": "تجاوز الحد الأقصى للصفقات المتزامنة",
}


@dataclass(slots=True)
class SmartEntrySettings:
    enabled: bool = field(default_factory=lambda: _env_bool("SMART_ENTRY", True))
    allowed_hours: list[int] = field(default_factory=lambda: _parse_hours(os.getenv("ENTRY_HOURS", DEFAULT_ENTRY_HOURS)))
    max_open_trades: int = field(default_factory=lambda: _env_int("MAX_OPEN_TRADES", 15))
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
        if not self.enabled:
            return "البوابة الزمنية معطّلة"
        return f"النافذة الزمنية: {self.hours_text()} (توقيت {self.timezone_name}) • حد أقصى {self.max_open_trades} صفقة متزامنة"

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
        if not self.enabled:
            return sorted(candidates, key=self.sort_key), []
        ranked = sorted(candidates, key=self.sort_key)
        slots = max(0, int(self.max_open_trades) - int(open_trades_count))
        return ranked[:slots], ranked[slots:]
