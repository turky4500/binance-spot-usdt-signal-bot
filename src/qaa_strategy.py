"""نظام القاع — تحويل Pine (swing_bottom_backtest/qaa_indicator_final.pine) إلى pandas.

نظام مستقل موازٍ داخل المستودع (لا علاقة له بـ Target Trend):

    الدخول: شمعة خضراء تُغلق فوق قاع مؤكد (.pivotlow 3/1 بشروط صارمة) وفوق EMA200،
            ومهلة > 3 شموع منذ آخر دخول (ومنع إعادة دخول في يوم الخروج كما في الاختبارات).
    الوقف:  يبدأ عند قاع الشمعة القاعية ثم يتحرك: max(الوقف، أعلى قمة × (1 - 8%))
            — يرتفع فقط ولا ينزل أبداً، ويُحسب من قمم الشموع السابقة فقط (بلا انتحار
            نفس الشمعة: تعديل ← فحص ← تحديث القمة — نفس ترتيب بايثون المُختبر).
    الخروج: لمس الوقف أو انتهاء المهلة (40 يومًا) — لا هدف ربح ثابت (مقصود ومُختبر).
            الهدف المعروض 2R للرؤية فقط.
    الفريم: يومي فقط (1h/4h خسارة بالأرقام).

أرقام التحقق المصححة (24 عملة، رسوم 0.3%، صفقة واحدة في المرة):
    ثابت + مهلة 20 = +18.2%  •  وقف متحرك 8% + مهلة 40 = +11.5% وهبوط أقصى أخف
    (24% مقابل 32%) — المتحرك مُفعّل هنا طلبًا للأمان كما في النسخة المُسلَّمة.

التطابق مع الاختبارات:
    - دلالات القاع strict (تساوي الجيران يُسقط القاع) — pivot_tie_test: فارق ~2%
      من الصفقات ونسبة فوز متطابقة 29.3%.
    - EMA تبدأ من أول قيمة في النافذة (ewm adjust=False) — مطابق لبايثون المُختبر؛
      مع 499 شمعة يومية ينثر أثر البذرة قبل الشمعة الأخيرة (~0.7%) فيهمل.
    - منع إعادة الدخول في يوم الخروج (block_until = نهاية الصفقة في الاختبارات).

⚠️ العزل: هذا الملف لا يستورد من app.py ولا من src/strategy.py — قواعده مكتوبة هنا
   مستقلة، ولا يقرأ/يكتب أي ملف يخص النظام الآخر (الحالة: state_qaa.json).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

DAY_MS = 86_400_000
EMA_LEN = 200


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class QaaSettings:
    """إعدادات نظام القاع — كلها قابلة للتعديل من متغيرات البيئة QAA_*."""

    left_bars: int = _env_int("QAA_LEFT_BARS", 3)                 # الشموع يسار القاع
    right_bars: int = _env_int("QAA_RIGHT_BARS", 1)               # الشموع يمين القاع (تأكيد)
    max_hold: int = _env_int("QAA_MAX_HOLD", 40)                  # أقصى مدة للصفقة (أيام)
    cooldown_bars: int = _env_int("QAA_COOLDOWN_BARS", 3)         # مهلة أقل بين الصفقات (شموع)
    use_trailing: bool = _env_int("QAA_USE_TRAILING", 1) != 0      # وقف متحرك (1/0)
    trail_pct: float = _env_float("QAA_TRAIL_PCT", 8.0)           # نسبة الوقف المتحرك %
    target_r: float = _env_float("QAA_TARGET_R", 2.0)             # الهدف = كم ضعف مسافة الوقف (عرض فقط)
    risk_pct: float = _env_float("QAA_RISK_PCT", 1.0)             # المخاطرة لكل صفقة %
    commission_side_pct: float = _env_float("QAA_COMMISSION_SIDE_PCT", 0.1)  # العمولة لكل جهة %
    # عتبة رسالة تحديث الوقف: رسالة عند ارتفاع ≥ هذه النسبة عن آخر رسالة (0 = عند كل تغيير)
    stop_alert_step_pct: float = _env_float("QAA_STOP_ALERT_STEP_PCT", 1.0)
    # ساعة إرسال تقرير الصفقات المفتوحة يوميًا (بتوقيت TIMEZONE، 24 ساعة)
    open_report_hour: int = _env_int("QAA_OPEN_REPORT_HOUR", 9)
    # إرسال التقرير فورًا بغض النظر عن الساعة (زر Run workflow → send_open_report)
    force_open_report: bool = _env_int("QAA_FORCE_OPEN_REPORT", 0) != 0

    @property
    def min_bars(self) -> int:
        """أدنى شموع لصالح الحساب: تمهيد EMA200 + هوامش (القاع يحتاج 3+1)."""
        return EMA_LEN + 10

    def describe(self) -> str:
        trailing = f"قف متحرك {self.trail_pct:g}%" if self.use_trailing else "قف ثابت"
        return (
            f"نظام القاع (يومي): قاع {self.left_bars}/{self.right_bars} • "
            f"EMA{EMA_LEN} • مهلة {self.max_hold} يومًا • {trailing} • "
            f"هدف عرض {self.target_r:g}R • تحديث القف عند +{self.stop_alert_step_pct:g}%"
        )


# ─────────────────────────────────────────────────────────────────────────────
# المؤشرات
# ─────────────────────────────────────────────────────────────────────────────
def ema(values: pd.Series) -> pd.Series:
    """EMA بمعامل 2/(n+1) تبدأ من أول قيمة (adjust=False) — مطابق لبايثون المُختبر."""
    return values.ewm(alpha=2.0 / (EMA_LEN + 1), adjust=False).mean()


def is_strict_pivot_low(lows: np.ndarray, idx: int, left: int, right: int) -> bool:
    """هل `idx` قاع صارم: أقل من كل الجيران يسارًا ويمينًا؟ التساوي مع أي جار يُسقطه.

    مطابق لـ find_pivots المرجعي في الاختبارات (الجار <= القاع ⇒ رفض).
    """
    n = len(lows)
    if idx - left < 0 or idx + right >= n:
        return False
    pivot = lows[idx]
    if not np.isfinite(pivot):
        return False
    for k in range(1, left + 1):
        if lows[idx - k] <= pivot:
            return False
    for k in range(1, right + 1):
        if lows[idx + k] <= pivot:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# الدخول
# ─────────────────────────────────────────────────────────────────────────────
def evaluate_entry(
    closed_df: pd.DataFrame,
    settings: QaaSettings,
    last_entry_bar_open_time: int = 0,
    last_exit_bar_open_time: int = 0,
) -> dict | None:
    """إشارة الدخول على الشمعة اليومية المغلقة الأخيرة (بدون انظار للمستقبل).

    تُرجع dict يحمل الدخول والوقف والهدف والحجم، أو None عند غياب أي شرط.
    """
    n = len(closed_df)
    if n < settings.min_bars:
        return None

    row = closed_df.iloc[-1]
    bar_open = int(row["open_time"])

    # ممنع إعادة دخول في يوم الخروج (block_until في الاختبارات)
    if last_exit_bar_open_time and bar_open == last_exit_bar_open_time:
        return None
    # مهلة > cooldown_bars شموع منذ آخر دخول (شمعة واحدة في المرة)
    if last_entry_bar_open_time and (bar_open - last_entry_bar_open_time) <= settings.cooldown_bars * DAY_MS:
        return None

    # القاع: شمعة الإشارة هي شمعة التأكيد — القاع بعدها مباشرة (rightBars للخلف)
    pivot_idx = n - 1 - settings.right_bars
    lows = closed_df["low"].to_numpy(dtype="float64")
    if not is_strict_pivot_low(lows, pivot_idx, settings.left_bars, settings.right_bars):
        return None
    pivot_value = float(lows[pivot_idx])

    entry_price = float(row["close"])
    if not (entry_price > float(row["open"])):        # شمعة خضراء
        return None
    if not (entry_price > pivot_value):               # إغلاق فوق القاع
        return None
    if not np.isfinite(entry_price) or entry_price <= 0:
        return None
    ema_last = float(ema(closed_df["close"]).iloc[-1])
    if not np.isfinite(ema_last) or not (entry_price > ema_last):   # فوق EMA200
        return None

    stop_price = pivot_value
    stop_dist_pct = (entry_price - stop_price) / entry_price * 100.0
    if stop_dist_pct <= 0:
        return None
    size_suggest_pct = min(settings.risk_pct / stop_dist_pct, 100.0)
    target_price = entry_price + settings.target_r * (entry_price - stop_price)

    return {
        "action": "buy",
        "bar_open_time": bar_open,
        "bar_close_time": int(row["close_time"]),
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "stop_dist_pct": stop_dist_pct,
        "size_suggest_pct": size_suggest_pct,
        "peak_price": float(row["high"]),   # قمة شمعة الإشارة (كما في Pine: peakHigh := high عند الدخول)
    }


# ─────────────────────────────────────────────────────────────────────────────
# الوقف المتحرك والخروج
# ─────────────────────────────────────────────────────────────────────────────
def next_stop(stop: float, peak: float, settings: QaaSettings) -> float:
    """الوقف التالي: يرتفع فقط ولا ينزل — من قمم الشموع السابقة فقط."""
    if not settings.use_trailing:
        return stop
    return max(stop, peak * (1.0 - settings.trail_pct / 100.0))


def stop_fill(stop: float, open_price: float) -> float:
    """سعر تنفيذ الوقف: فجوة أسفل الوقف = تنفيذ عند الفتح (الأدنى)، وإلا عند الوقف.

    مطابق للاختبارات: px = sl if sl <= open else open.
    """
    return min(stop, open_price)


def should_raise_alert(new_stop: float, last_alert_price: float, step_pct: float) -> bool:
    """هل يستحق الوقف الجديد رسالة؟ ≥ عتبة النسبة عن آخر رسالة مُرسلة (0 = أي ارتفاع)."""
    if not last_alert_price or last_alert_price <= 0:
        return True
    if new_stop <= last_alert_price:
        return False
    if step_pct <= 0:
        return True
    return new_stop >= last_alert_price * (1.0 + step_pct / 100.0)


def is_timeout(entry_bar_open_time: int, candle_open_time: int, max_hold: int) -> bool:
    """هل انتهت مهلة الصفقة (>= max_hold شموع يومية من شمعة الدخول)؟"""
    return (candle_open_time - entry_bar_open_time) >= max_hold * DAY_MS
