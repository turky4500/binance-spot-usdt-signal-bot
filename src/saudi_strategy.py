"""نظام القاع — نسخة السعودية (تداول): قواعد مطابقة لنظام القاع الكريبتو مع تكييف جوهري.

الكيان المستقل: لا يستورد من qaa_strategy ولا من strategy — قواعده مكتوبة هنا
مستقلة، ولا يقرأ/يكتب أي ملف يخص نظامًا آخر (الحالة: state_sa.json).

القواعد كما في نظام القاع (مُتحقَّق منها على الكريبتو، وتُعاد هنا التحقق على السعودية):
    الدخول: شمعة خضراء تُغلق فوق قاع مؤكد (pivotlow يسار3/يمين1 صارم — التساوي يُسقطه)
            وفوق EMA200، مع مهلة ≥ cooldown شموع منذ آخر دخول، ولا إعادة دخول في يوم الخروج.
    الوقف:  يبدأ عند قاع الشمعة القاعية ثم max(الوقف، أعلى قمة × (1 - trail%)) — يرتفع فقط،
            والقمة تشمل قمة شمعة الدخول نفسها، ويُحسب من قمم الشموع السابقة فقط
            (لا حماية بنفس الشمعة: تعديل ← فحص ← تحديث القمة).
    الخروج: لمس الوقف (مع تنفيذ الفجوة عند الفتح) أو انتهاء المهلة (max_hold شموع) —
            لا هدف ربح ثابت (الهدف 2R عرض فقط).
    الفريم: يومي حصريًا.

⚠️ التكييف الجوهري للسعودية — عدّ الشموع بدل فروق الزمن:
    في الكريبتو كل شمعة يوم = DAY_MS وتتمağa الأيام متصلة، فالمهلة والـcooldown
    تُحسب بـ (فرق open_time ≤ n × DAY_MS). لكن أيام تداول السعودية **غير متصلة**
    (عطلة نهاية أسبوع + عطلات رسمية): الشمعة التالية قد بعد 3 أو 5 أيام تقويمية.
    لذا نعدّ **فرق الفهرس (عدد الشموع الفعلية)** في بيانات الرمز — صحيح تمامًا
    مع أي تقويم عطلات، ولا يفترض تباعدًا زمنيًا منتظمًا.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

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
class SaSettings:
    """إعدادات نظام القاع — السعودية. كلها قابلة للتعديل من متغيرات البيئة SA_*."""

    left_bars: int = _env_int("SA_LEFT_BARS", 3)                  # الشموع يسار القاع
    right_bars: int = _env_int("SA_RIGHT_BARS", 1)                # شمعة تأكيد القاع
    max_hold: int = _env_int("SA_MAX_HOLD", 40)                   # أقصى مدة (شموع تداول)
    cooldown_bars: int = _env_int("SA_COOLDOWN_BARS", 3)          # مهلة أقل بين الدخولات (شموع)
    use_trailing: bool = _env_int("SA_USE_TRAILING", 1) != 0      # وقف متحرك (1/0)
    trail_pct: float = _env_float("SA_TRAIL_PCT", 8.0)            # نسبة الوقف المتحرك %
    target_r: float = _env_float("SA_TARGET_R", 2.0)              # الهدف = كم ضعف مسافة الوقف (عرض)
    risk_pct: float = _env_float("SA_RISK_PCT", 1.0)              # المخاطرة لكل صفقة %
    # عمولة لكل جهة (%): تداول ~0.15% لكل جهة (تُضبط حسب وسيطك)
    commission_side_pct: float = _env_float("SA_COMMISSION_SIDE_PCT", 0.15)
    # عتبة رسالة تحديث الوقف: رسالة عند ارتفاع ≥ هذه النسبة عن آخر رسالة (0 = عند كل تغيير)
    stop_alert_step_pct: float = _env_float("SA_STOP_ALERT_STEP_PCT", 1.0)

    @property
    def min_bars(self) -> int:
        """أدنى شموع لصالح الحساب: تمهيد EMA200 + هوامش (القاع يحتاج 3+1)."""
        return EMA_LEN + 10

    def describe(self) -> str:
        trailing = f"قف متحرك {self.trail_pct:g}%" if self.use_trailing else "قف ثابت"
        return (
            f"نظام القاع (سهم يومي): قاع {self.left_bars}/{self.right_bars} • "
            f"EMA{EMA_LEN} • مهلة {self.max_hold} شمعة • {trailing} • "
            f"هدف عرض {self.target_r:g}R • تحديث القف عند +{self.stop_alert_step_pct:g}%"
        )


# ───────────────────────────────────────────── المؤشرات
def ema(values: pd.Series) -> pd.Series:
    """EMA بمعامل 2/(n+1) تبدأ من أول قيمة (adjust=False) — مطابق للمُختبر."""
    return values.ewm(alpha=2.0 / (EMA_LEN + 1), adjust=False).mean()


def is_strict_pivot_low(lows: np.ndarray, idx: int, left: int, right: int) -> bool:
    """هل `idx` قاع صارم: أقل من كل الجيران يسارًا ويمينًا؟ التساوي مع أي جار يُسقطه."""
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


# ───────────────────────────────────────────── عدّ الشموع (بدل فروق الزمن)
def find_bar_index(df: pd.DataFrame, open_time_ms: int) -> int | None:
    """فهرس الشمعة ذات open_time المحدّد، أو None إن لم تكن ضمن النافذة."""
    if df is None or df.empty or not open_time_ms:
        return None
    idxs = np.flatnonzero(df["open_time"].to_numpy(dtype="int64") == int(open_time_ms))
    return int(idxs[-1]) if len(idxs) else None


def bars_between(df: pd.DataFrame, from_open_ms: int, to_open_ms: int) -> int | None:
    """عدد الشموع (فروق فهرس) من شمعة إلى شمعة — أساس المهلة/الـcooldown بالتداول.

    يُرجع None إذا لم تُوجد إحدى الشمعتين ضمن النافذة (يعامل كـ«قديم جدًا»).
    """
    i = find_bar_index(df, from_open_ms)
    j = find_bar_index(df, to_open_ms)
    if i is None or j is None:
        return None
    return max(j - i, 0)


# ───────────────────────────────────────────── الدخول
def evaluate_entry(
    closed_df: pd.DataFrame,
    settings: SaSettings,
    last_entry_bar_open_time: int = 0,
    last_exit_bar_open_time: int = 0,
) -> dict | None:
    """إشارة الدخول على الشمعة اليومية المغلقة الأخيرة (بدون انظار للمستقبل).

    تُرجع dict يحمل الدخول والوقف والهدف والحجم، أو None عند غياب أي شرط.
    المهلة والـcooldown تُحسب بفرق الفهرس (شموع تداول فعلية) لا بزمن التقويم.
    """
    n = len(closed_df)
    if n < settings.min_bars:
        return None

    row = closed_df.iloc[-1]
    bar_open = int(row["open_time"])

    # ممنع إعادة دخول في يوم الخروج (مطابقة open_time للخروج)
    if last_exit_bar_open_time and bar_open == last_exit_bar_open_time:
        return None

    # مهلة ≥ cooldown شموع منذ آخر دخول (عدّ فهرس؛ الشمعة القديمة خارج النافذة لا تمنع)
    last_idx = find_bar_index(closed_df, last_entry_bar_open_time)
    if last_idx is not None and (n - 1 - last_idx) <= settings.cooldown_bars:
        return None

    # القاع: شمعة الإشارة هي شمعة التأكيد — القاع بعدها مباشرة (right_bars للخلف)
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
        "bar_close_time": int(row.get("close_time", bar_open)),
        "bar_date": str(row.get("date", "")),
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "stop_dist_pct": stop_dist_pct,
        "size_suggest_pct": size_suggest_pct,
        "peak_price": float(row["high"]),   # قمة شمعة الإشارة (peakHigh := high عند الدخول)
    }


# ───────────────────────────────────────────── الوقف المتحرك والخروج
def next_stop(stop: float, peak: float, settings: SaSettings) -> float:
    """الوقف التالي: يرتفع فقط ولا ينزل — من قمم الشموع السابقة فقط."""
    if not settings.use_trailing:
        return stop
    return max(stop, peak * (1.0 - settings.trail_pct / 100.0))


def stop_fill(stop: float, open_price: float) -> float:
    """سعر تنفيذ الوقف: فجوة أسفل الوقف = تنفيذ عند الفتح (الأدنى)، وإلا عند الوقف."""
    return min(stop, open_price)


def should_raise_alert(new_stop: float, last_alert_price: float, step_pct: float) -> bool:
    """هل يستحق الوقف الجديد رسالة؟ ≥ عتبة النسبة عن آخر رسالة (0 = أي ارتفاع)."""
    if not last_alert_price or last_alert_price <= 0:
        return True
    if new_stop <= last_alert_price:
        return False
    if step_pct <= 0:
        return True
    return new_stop >= last_alert_price * (1.0 + step_pct / 100.0)


def is_timeout(bars_held: int | None, max_hold: int) -> bool:
    """هل انتهت المهلة (>= max_hold شموع تداول من شمعة الدخول)؟ None = تعذّر العدّ فلا مهلة."""
    if bars_held is None:
        return False
    return bars_held >= max_hold
