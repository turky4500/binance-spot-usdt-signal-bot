"""قواعد الخروج — قابلة للتبديل عبر متغيرات البيئة لقياس الأثر على صفقات حقيقية."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


EXIT_MODE_FIXED = "fixed_target"
EXIT_MODE_TRAILING = "trailing"
EXIT_MODE_HYBRID = "hybrid"


@dataclass(slots=True)
class ExitSettings:
    """إعدادات الخروج.

    - fixed_target: السلوك القديم — هدف +2% ووقف ثابت.
    - trailing: وقف متحرك بمسافة مضاعف×ATR من أعلى سعر، بلا هدف ثابت.
      مبني على نتيجة المختبر: نفس منطق الدخول انتقل من -0.088% إلى +0.096% لكل صفقة.
    """

    mode: str = os.getenv("EXIT_MODE", EXIT_MODE_FIXED).strip().lower()
    trail_atr_mult: float = _env_float("TRAIL_ATR_MULT", 2.0)
    trail_atr_len: int = _env_int("TRAIL_ATR_LEN", 14)
    reference_target_pct: float = _env_float("REFERENCE_TARGET_PCT", 2.0)
    hybrid_lock_pct: float = _env_float("HYBRID_LOCK_PCT", 0.0)

    @property
    def is_trailing(self) -> bool:
        return self.mode == EXIT_MODE_TRAILING

    @property
    def is_hybrid(self) -> bool:
        """وقف الإشارة الأصلي حتى بلوغ الهدف المرجعي، ثم وقف متحرك يقفل ربحًا."""
        return self.mode == EXIT_MODE_HYBRID

    @property
    def uses_trailing_stop(self) -> bool:
        return self.mode in (EXIT_MODE_TRAILING, EXIT_MODE_HYBRID)

    def trail_level(self, peak_price: float, atr_value: float) -> float:
        return float(peak_price) - (self.trail_atr_mult * float(atr_value))

    def hybrid_lock_level(self, entry_price: float) -> float:
        return float(entry_price) * (1.0 + self.hybrid_lock_pct / 100.0)

    def describe(self) -> str:
        if self.is_trailing:
            return (
                f"وقف متحرك: {self.trail_atr_mult:.1f}×ATR({self.trail_atr_len}) من أعلى سعر "
                f"• الهدف المرجعي +{self.reference_target_pct:.1f}% (لا يُغلق الصفقة)"
            )
        if self.is_hybrid:
            return (
                f"هجينة: وقف الإشارة الأصلي حتى +{self.reference_target_pct:.1f}%، "
                f"ثم وقف متحرك {self.trail_atr_mult:.1f}×ATR مع أرضية {self.hybrid_lock_pct:+.2f}%"
            )
        return f"هدف ثابت +{self.reference_target_pct:.1f}% ووقف ثابت"
