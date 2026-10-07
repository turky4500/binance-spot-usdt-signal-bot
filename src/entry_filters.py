from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# أسباب الرفض مع شرح عربي مختصر
REJECTION_REASONS_AR = {
    "volume_too_low": "حجم تداول أقل من الحد الأدنى المطلوب",
    "volume_blowoff": "حجم تداول مبالغ فيه (احتمال انفجار سعري مؤقت)",
    "di_trend_negative": "ضغط البيع أعلى من أو يساوي ضغط الشراء",
    "missing_volume_data": "بيانات الحجم النسبي غير متوفرة",
    "missing_di_data": "بيانات مؤشر الاتجاه DI غير متوفرة",
}


@dataclass(slots=True)
class EntryGateSettings:
    """بوابات الجودة الجديدة على الإشارات قبل إرسالها.

    مبنية على تحليل أول 172 صفقة: أنسب نطاق للحجم النسبي هو 1.2 – 2.5،
    وأن الدخول يكون أنظف عندما يكون ضغط الشراء (DI+) أعلى من ضغط البيع (DI-).
    كل القيم قابلة للتغيير من متغيرات البيئة بدون تعديل الكود.
    """

    min_relative_volume: float = _env_float("GATE_MIN_REL_VOLUME", 1.20)
    max_relative_volume: float = _env_float("GATE_MAX_REL_VOLUME", 2.50)
    require_di_positive: bool = _env_bool("GATE_REQUIRE_DI_POSITIVE", True)
    min_di_spread: float = _env_float("GATE_MIN_DI_SPREAD", 0.0)

    def describe(self) -> str:
        parts = [
            f"الحجم النسبي بين {self.min_relative_volume:.2f} و {self.max_relative_volume:.2f}",
        ]
        if self.require_di_positive:
            parts.append(f"DI+ يتجاوز DI- بفارق {self.min_di_spread:.1f} على الأقل")
        else:
            parts.append("فلتر اتجاه DI معطّل")
        return " • ".join(parts)


def evaluate_entry_gates(metrics: dict | None, settings: EntryGateSettings) -> tuple[bool, str, str]:
    """يرجّع (مسموح, رمز_السبب, شرح_عربي)."""
    metrics = metrics or {}

    relative_volume = metrics.get("relative_volume")
    if relative_volume is None:
        return False, "missing_volume_data", REJECTION_REASONS_AR["missing_volume_data"]
    if relative_volume < settings.min_relative_volume:
        return False, "volume_too_low", REJECTION_REASONS_AR["volume_too_low"]
    if relative_volume > settings.max_relative_volume:
        return False, "volume_blowoff", REJECTION_REASONS_AR["volume_blowoff"]

    if settings.require_di_positive:
        plus_di = metrics.get("plus_di")
        minus_di = metrics.get("minus_di")
        if plus_di is None or minus_di is None:
            return False, "missing_di_data", REJECTION_REASONS_AR["missing_di_data"]
        if plus_di <= minus_di + settings.min_di_spread:
            return False, "di_trend_negative", REJECTION_REASONS_AR["di_trend_negative"]

    return True, "", ""
