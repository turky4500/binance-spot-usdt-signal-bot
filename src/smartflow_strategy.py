"""Smart Flow PRO MAX — نسخة بايثون معايَرة (منطق خالص بلا إدخال/إخراج).

المؤشر الأصلي كود مغلق على TradingView («Smart Flow PRO MAX + Last 2 Signals 🔥»)
ولا يوجد عنه أي شرح مكتوب من البائع، لذا هذه النسخة مبنية على **المعايرة البصرية**:

فرضية v1.1 (2026-10-25) — بأصل لقطة شراء حقيقية من المالك (BTCUSDT):
    - شريط التدفق الأصفر على الشارت = خط درّجي يتبع أعلى قمم/أدنى قيعان نافذة
      (سلوك ta.highest/ta.lowest مع الاحتفاظ بالدرجات) — ي_flip عند العبور:
        شراء = إغلاق الشمعة يقطع لأعلى أعلى قمة لعدد break_len شموع سابقة
               (مع عدم كون الشمعة السابقة قد اخترقتها نفسها — مثل ta.crossover)
        بيع  = إغلاق الشمعة يكسر لأدنى أدنى قاع لنفس النافذة (crossunder)
    - لقطة الشراء طابقت: الدخول ≈ إغلاق شمعة الانقلاب • الوقف ≈ أدنى قاع للنافذة
      (77,734 / 62,751) — فأصبح القاع/القمة الزمنية للنافذة هو «الوقف المبدئي».
    - معاملات الإعدادات الظاهرة (EMA20/EMA50/EMA10/SMA20/RSI14+مستوى45/اختراق10/ATR14)
      تُحسب جميعها وتُرسَل داخل الرسائل للمعايرة؛ **ولا يُشترَط أي منها في الإشارة**
      باستثناء بوابة RSI الاختيارية (SMARTFLOW_USE_RSI_GATE، افتراضيًا مغلقة —
      لقطة الشراء وقعت وRSI منخفض بعد الانهيار، لذا البوابة المغلقة افتراضيًا).
    - الهدفان (هدف 1 / هدف 2) من الجدول الجانبي لا تُعرف قاعدتهما بعد → قيد المعايرة.

ملاحظة دقة: RSI/ATR هنا تنفيذ Wilder المطابق لـ ta.rsi/ta.atr في Pine،
وta.ema يعادل ewm(span=n, adjust=False).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
import pandas as pd


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(float(raw)) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return raw.lower() not in ("0", "false", "no", "off")


@dataclass(slots=True)
class SmartFlowSettings:
    """إعدادات النظام — كلها قابلة للتجاوز بمتغيرات بيئة SMARTFLOW_*"""

    ema_len: int = 20            # EMA 20
    ema_slow_len: int = 50       # EMA 50
    ema_fast_len: int = 10       # EMA البيع (سريعة) 10
    sma_len: int = 20            # متوسط السعر 20
    rsi_len: int = 14            # مؤشر القوى النسبية 14
    # مستوى RSI الظهِر في إعدادات المؤشر = 45 — بوابة اختيارية (معطّلة افتراضيًا)
    # إلى أن تُعايَر قاعدتها الفعلية من لقطات نسخة البائع.
    buy_rsi_min: float = 45.0
    sell_rsi_max: float = 45.0
    use_rsi_gate: bool = False   # SMARTFLOW_USE_RSI_GATE=1 لتفعيلها
    break_len: int = 10          # اختراق القمة 10 (نافذة الانقلاب)
    atr_len: int = 14            # متوسط المدى الحقيقي 14
    # سلوك البوت (ليس من المؤشر الأصلي — ضد إزعاج الرسائل):
    signal_mode: str = "on_flip"   # on_flip (أول إشارة أو انقلاب) | every_bar
    cooldown_bars: int = 6         # مهلة أقل بين إشارتين متوافقتي الاتجاه

    @classmethod
    def from_env(cls) -> "SmartFlowSettings":
        mode = os.getenv("SMARTFLOW_SIGNAL_MODE", "").strip().lower()
        return cls(
            ema_len=_env_int("SMARTFLOW_EMA_LEN", 20),
            ema_slow_len=_env_int("SMARTFLOW_EMA_SLOW_LEN", 50),
            ema_fast_len=_env_int("SMARTFLOW_EMA_FAST_LEN", 10),
            sma_len=_env_int("SMARTFLOW_SMA_LEN", 20),
            rsi_len=_env_int("SMARTFLOW_RSI_LEN", 14),
            buy_rsi_min=_env_float("SMARTFLOW_BUY_RSI_MIN", 45.0),
            sell_rsi_max=_env_float("SMARTFLOW_SELL_RSI_MAX", 45.0),
            use_rsi_gate=_env_bool("SMARTFLOW_USE_RSI_GATE", False),
            break_len=max(_env_int("SMARTFLOW_BREAK_LEN", 10), 2),
            atr_len=_env_int("SMARTFLOW_ATR_LEN", 14),
            signal_mode=mode if mode in ("on_flip", "every_bar") else "on_flip",
            cooldown_bars=min(max(_env_int("SMARTFLOW_COOLDOWN_BARS", 6), 0), 500),
        )

    def min_bars(self) -> int:
        """أقل عدد شموع مغلقة حتى تتعامل كل المؤشرات بقيمها المعرفة."""
        return max(
            self.ema_slow_len, self.ema_len, self.sma_len, self.rsi_len,
            self.atr_len, self.break_len + 1,
        ) + 10

    def describe(self) -> str:
        gate = f"مفعّلة ({self.buy_rsi_min:g}/{self.sell_rsi_max:g})" if self.use_rsi_gate else "مغلقة"
        return (
            f"SmartFlow v1.1 — نافذة الانقلاب {self.break_len} شموع • "
            f"EMA {self.ema_len}/{self.ema_slow_len} · SMA {self.sma_len} · RSI {self.rsi_len} (بوابة {gate}) · "
            f"ATR {self.atr_len} • نمط الرسائل {self.signal_mode} مهلة {self.cooldown_bars} شمعة"
        )


# ─────────────────────────────────────────────────────────────────────────────
# المؤشرات (مطابقة Pine قدر الإمكان)
# ─────────────────────────────────────────────────────────────────────────────
def ema(series: pd.Series, length: int) -> pd.Series:
    """ta.ema في Pine = EMA بمعامل 2/(n+1)."""
    return series.ewm(span=length, adjust=False).mean()


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length).mean()


def rsi(series: pd.Series, length: int) -> pd.Series:
    """ta.rsi في Pine — تنعيم وايلدر (alpha = 1/n).

    صعود خالص (لا خسائر) → 100 • هبوط خالص (لا أرباح) → 0.
    """
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.where(avg_loss > 0)
    out = (100.0 - 100.0 / (1.0 + rs)).fillna(100.0)
    return out.where(avg_gain > 0, 0.0)


def atr(df: pd.DataFrame, length: int) -> pd.Series:
    """ta.atr في Pine — متوسط المدى الحقيقي بتنعيم وايلدر."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()


def add_indicators(df: pd.DataFrame, settings: SmartFlowSettings) -> pd.DataFrame:
    """يضيف أعمدة المؤشرات على نسخة من DataFrame المغلق."""
    d = df.copy()
    d["ema_mid"] = ema(d["close"], settings.ema_len)
    d["ema_slow"] = ema(d["close"], settings.ema_slow_len)
    d["ema_fast"] = ema(d["close"], settings.ema_fast_len)
    d["sma_mid"] = sma(d["close"], settings.sma_len)
    d["rsi"] = rsi(d["close"], settings.rsi_len)
    d["atr"] = atr(d, settings.atr_len)
    d["hh_prev"] = d["high"].rolling(settings.break_len).max().shift(1)  # ta.highest(high, n)[1]
    d["ll_prev"] = d["low"].rolling(settings.break_len).min().shift(1)   # ta.lowest(low,  n)[1]
    return d


# ─────────────────────────────────────────────────────────────────────────────
# تقييم الإشارة على شمعة مغلقة واحدة (آخر صف في df)
# ─────────────────────────────────────────────────────────────────────────────
def evaluate(df: pd.DataFrame, settings: SmartFlowSettings) -> dict | None:
    """تقييم آخر شمعة مغلقة — فرضية v1.1 (انقلاب نافذة القمة/القاع).

    شراء = إغلاق فوق أعلى قمة break_len شموع سابقة والشمعة السابقة لم تخترقها.
    بيع  = إغلاق تحت أدنى قاع break_len شموع سابقة والشمعة السابقة لم تكسرها.
    بوابة RSI اختيارية (معطّلة افتراضيًا). يعني None ما لم تكتمل المؤشرات.
    """
    if df is None or df.empty:
        return None
    if len(df) < settings.min_bars():
        return None

    d = add_indicators(df, settings)
    last = d.iloc[-1]
    prev = d.iloc[-2]

    needed = ("close", "rsi", "ema_fast", "ema_mid", "ema_slow", "sma_mid", "atr", "hh_prev", "ll_prev")
    if last[list(needed)].isna().any() or prev[["close", "hh_prev", "ll_prev"]].isna().any():
        return None

    close = float(last["close"])
    prev_close = float(prev["close"])

    buy_cross = close > float(last["hh_prev"]) and prev_close <= float(prev["hh_prev"])
    sell_cross = close < float(last["ll_prev"]) and prev_close >= float(prev["ll_prev"])

    rsi_value = float(last["rsi"])
    ok_buy = buy_cross and (not settings.use_rsi_gate or rsi_value >= settings.buy_rsi_min)
    ok_sell = sell_cross and (not settings.use_rsi_gate or rsi_value <= settings.sell_rsi_max)

    if not (ok_buy or ok_sell):
        return None

    kind = "buy" if ok_buy else "sell"
    breakout_level = float(last["hh_prev"]) if kind == "buy" else float(last["ll_prev"])
    return {
        "kind": kind,
        "close": close,
        "rsi": rsi_value,
        "ema_fast": float(last["ema_fast"]),
        "ema_mid": float(last["ema_mid"]),
        "ema_slow": float(last["ema_slow"]),
        "sma": float(last["sma_mid"]),
        "atr": float(last["atr"]),
        "breakout_level": breakout_level,
        # الوقف المبدئي: أدنى قاع النافذة عند الشراء / أعلى قمتها عند البيع (فرضية طابقت اللقطة)
        "stop_level": float(last["ll_prev"]) if kind == "buy" else float(last["hh_prev"]),
        "trend_up": float(last["ema_mid"]) > float(last["ema_slow"]),
    }
