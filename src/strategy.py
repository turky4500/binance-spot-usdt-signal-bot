"""Target Trend [BigBeluga] — تحويل Pine v5 إلى pandas.

هذا هو **المؤشر الوحيد المعتمد** في البوت (بقية الأنظمة حُذفت 2026-10-09).

المنطق المطابق للـ Pine:
    atr_value = sma(atr(200), 200) * 0.8
    sma_high  = sma(high, length) + atr_value      ← خط الاتجاه الصاعد
    sma_low   = sma(low,  length) - atr_value      ← خط الاتجاه الهابط (والوقف)

    trend = true  عند crossover(close, sma_high)
    trend = false عند crossunder(close, sma_low)
    signal_up   = تحوّل الاتجاه هابطًا ← صاعدًا   → إشارة شراء (عند إغلاق الشمعة)
    signal_down = تحوّل الاتجاه صاعدًا ← هابطًا   → إشارة إغلاق الصفقة

عند إشارة الشراء:
    الدخول  = إغلاق شمعة الإشارة
    الوقف   = sma_low لنفس الشمعة (خط stop_loss الذي يرسمه المؤشر)
    الأهداف = الدخول + (5/10/15 + فارق Set Targets) × atr_value
    الخروج  = إشارة البيع، أو لمس الوقف

⚠️ ملاحظة تقنية حرجة:
    sma(atr(200), 200) تحتاج ≥ 400 شمعة حتى تصبح صالحة، لذلك يجب أن يكون
    KLINE_LIMIT ≥ 420 (مضبوط 499 افتراضيًا — وهو أقصى حد قبل أن يصبح وزن
    طلب Binance لـ klines = 5 بدل 2).

الرخصة: CC BY-NC-SA 4.0 © BigBeluga (نسب العمل إلى المؤشر عند نشره).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


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
class StrategySettings:
    """إعدادات Target Trend — كلها قابلة للتعديل من متغيرات البيئة."""

    length: int = _env_int("TREND_LENGTH", 10)                 # Trend Length
    target_offset: int = _env_int("TREND_TARGET_OFFSET", 0)    # Set Targets
    atr_len: int = 200                                         # فترة ATR الأساسية
    atr_smooth_len: int = 200                                  # تسطيح ATR (sma over atr)
    atr_scale: float = 0.8                                     # ×0.8 كما في Pine
    commission_per_side_pct: float = 0.1                       # العمولة لكل جهة %

    @property
    def min_bars(self) -> int:
        """أدنى عدد شموع لصالح الحساب (warmup الـ ATR المسطّح + الماوس)."""
        return self.atr_len + self.atr_smooth_len + self.length + 10

    @property
    def target_multipliers(self) -> tuple[float, float, float]:
        """مضاعفات الأهداف الثلاثة كما في Pine: (5+o, 10+2o, 15+3o)."""
        o = float(self.target_offset)
        return (5.0 + o, 10.0 + 2.0 * o, 15.0 + 3.0 * o)

    def describe(self) -> str:
        m = self.target_multipliers
        return (
            f"Target Trend: طول الاتجاه {self.length} • "
            f"أهداف {m[0]:g}/{m[1]:g}/{m[2]:g}×ATR"
        )


# ─────────────────────────────────────────────────────────────────────────────
# المؤشرات الأساسية (نفس بنية Pine: RMA/ATR)
# ─────────────────────────────────────────────────────────────────────────────
def rma(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def atr(df: pd.DataFrame, length: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return rma(tr, length)


def compute_trend_frame(df: pd.DataFrame, settings: StrategySettings, initial_trend: float = float("nan")) -> pd.DataFrame:
    """يبني إطار الاتجاه والأهداف: atr_value, sma_high, sma_low, trend, signals.

    initial_trend: حالة الاتجاه قبل بداية النافذة (0.0 هابط / 1.0 صاعد / nan مجهول).
    تُستخدم لاستكمال سلسلة الاتجاه عند غياب أي تقاطع داخل النافذة (أنظر latest_signal).
    """
    data = df.copy().reset_index(drop=True)

    atr_value = atr(data, settings.atr_len).rolling(settings.atr_smooth_len).mean() * settings.atr_scale
    data["atr_value"] = atr_value
    data["sma_high"] = data["high"].rolling(settings.length).mean() + atr_value
    data["sma_low"] = data["low"].rolling(settings.length).mean() - atr_value

    close = data["close"]
    sma_high = data["sma_high"]
    sma_low = data["sma_low"]

    # ta.crossover / ta.crossunder — مقارنات مع NaN تعطي False تلقائيًا كما في Pine
    cross_up = (close > sma_high) & (close.shift(1) <= sma_high.shift(1))
    cross_dn = (close < sma_low) & (close.shift(1) >= sma_low.shift(1))
    data["cross_up"] = cross_up
    data["cross_dn"] = cross_dn

    # var bool trend = na → يبقى na حتى أول تقاطع ثم يحمل قيمته لكل شمعة تالية
    trend = pd.Series(np.nan, index=data.index, dtype="float64")
    trend = trend.mask(cross_up, 1.0).mask(cross_dn, 0.0)
    trend = trend.ffill()
    # استكمال الحالة قبل أول تقاطع داخل النافذة من حالة ما قبلها (إن عُرفت)
    if initial_trend is not None and not (isinstance(initial_trend, float) and np.isnan(initial_trend)):
        trend = trend.fillna(float(initial_trend))
    data["trend"] = trend

    prev = trend.shift(1)
    # signal_up   = ta.change(trend) and not trend[1]  → كان false والآن true
    # signal_down = ta.change(trend) and trend[1]      → كان true  والآن false
    # (المقارنة مع prev == False تعطي False تلقائيًا لو كانت na — مطابق لـ Pine:
    #  أي لا إشارة عند أول انتقال na→true، وهذه الحالة تُكشف وتُستدعى عبر action="uncertain")
    data["signal_up"] = (trend == 1.0) & (prev == 0.0)
    data["signal_down"] = (trend == 0.0) & (prev == 1.0)
    return data


def latest_signal(
    df: pd.DataFrame,
    settings: StrategySettings,
    initial_trend: float = float("nan"),
) -> dict[str, Any] | None:
    """إشارة الشمعة المغلقة الأخيرة.

    يُرجع:
      - {"action": "buy"/"sell", ...} عند انقلاب اتجاه مؤكد
      - {"action": "uncertain"} عند تقاطع على الشمعة الأخيرة دون أي تقاطع سابق
        داخل النافذة (حالة الاتجاه قبلها مجهولة) — يجب في هذه الحالة إعادة الحساب
        بسجل أوسع قبل اتخاذ قرار، وإلا أُعيد تطبيق نفس منطق Pine (لا إشارة عند na).
      - None عند غياب أي إشارة (مؤكد)
    """
    if df is None or df.empty or len(df) < settings.min_bars:
        return None

    data = compute_trend_frame(df, settings, initial_trend=initial_trend)
    row = data.iloc[-1]
    bar_open_time = int(row["open_time"])
    bar_close_time = int(row["close_time"])

    # هل وقع تقاطع على الشمعة الأخيرة بينما الحالة السابقة مجهولة؟
    prev_trend = data["trend"].iloc[-2] if len(data) > 1 else float("nan")
    if (bool(row["cross_up"]) or bool(row["cross_dn"])) and pd.isna(prev_trend):
        return {"action": "uncertain", "bar_open_time": bar_open_time, "bar_close_time": bar_close_time}

    if bool(row["signal_up"]):
        entry_price = float(row["close"])
        stop_price = float(row["sma_low"])
        atr_value = float(row["atr_value"])
        if not np.isfinite(atr_value) or not np.isfinite(stop_price) or entry_price <= 0:
            return None
        targets = [entry_price + mult * atr_value for mult in settings.target_multipliers]
        if not all(np.isfinite(t) for t in targets):
            return None
        return {
            "action": "buy",
            "bar_open_time": bar_open_time,
            "bar_close_time": bar_close_time,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "targets": targets,
            "atr_value": atr_value,
            "trend_length": settings.length,
        }

    if bool(row["signal_down"]):
        return {
            "action": "sell",
            "bar_open_time": bar_open_time,
            "bar_close_time": bar_close_time,
            "price": float(row["close"]),
        }

    return None
