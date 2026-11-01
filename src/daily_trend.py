"""نظام «تتبّع الاتجاه اليومي» — منطق خالص بلا إدخال/إخراج (قابل للاختبار بالكامل).

القواعد (مثبتة باختبار سنتين و~600 تهيئة — تقرير 2026-11-01):
    1. الإطار: شمعة يومية مغلقة (00:00 UTC).
    2. بوابة السوق: لا دخول إلا إذا كان إغلاق BTC(4h) **فوق** EMA200(4h).
       وإذا أُغلقت البوابة ⇒ تُغلق كل المراكز فورًا (السوق النقدي مركز).
    3. الدخول: إغلاق اليوم أعلى من أعلى سعر للـ`lookback` يومًا السابقة.
    4. الوقف الأولي: الدخول − `atr_mult` × ATR(14 يومي).
    5. الوقف المتحرك: كل يوم = أعلى سعر منذ الدخول − atr_mult × ATR(المسجّل عند الدخول)،
       ولا يتراجع أبدًا (ratchet).
    6. لا هدف ثابت: الصفقة تُترك حتى يُلمس الوقف أو تُغلق بوابة السوق.
    7. الحجم: مخاطرة `risk_pct`% من الرصيد للصفقة، بحد أقصى رصيد/`max_positions` للمركز.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
import pandas as pd


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


@dataclass(slots=True)
class DailyTrendSettings:
    lookback: int = _env_int("DT_LOOKBACK", 20)             # اختراق أعلى سعر لـ20 يومًا
    atr_len: int = _env_int("DT_ATR_LEN", 14)
    atr_mult: float = _env_float("DT_ATR_MULT", 2.5)        # وقف متحرك = 2.5 × ATR
    risk_pct: float = _env_float("DT_RISK_PCT", 1.0)        # مخاطرة الصفقة من الرصيد %
    max_positions: int = _env_int("DT_MAX_POSITIONS", 2)    # مركزان متزامنان
    gate_ema_len: int = _env_int("DT_GATE_EMA_LEN", 200)    # EMA200 على 4 ساعات
    commission_side_pct: float = _env_float("DT_COMMISSION_SIDE_PCT", 0.1)
    max_stop_pct: float = _env_float("DT_MAX_STOP_PCT", 15.0)   # رفض وقف غير منطقي
    min_stop_pct: float = _env_float("DT_MIN_STOP_PCT", 1.0)
    capital: float = _env_float("DT_CAPITAL", 300.0)        # رأس المال الابتدائي
    daily_limit: int = _env_int("DT_DAILY_LIMIT", 120)      # شموع يومية مطلوبة
    gate_limit: int = _env_int("DT_GATE_LIMIT", 320)        # شموع 4h مطلوبة (EMA200 + هامش)

    def describe(self) -> str:
        return (
            f"تتبّع اتجاه يومي: اختراق {self.lookback} يومًا • وقف {self.atr_mult:g}×ATR({self.atr_len}) متحرك • "
            f"بوابة BTC فوق EMA{self.gate_ema_len}(4h) • مخاطرة {self.risk_pct:g}% • "
            f"حد أقصى {self.max_positions} مراكز"
        )


# ─────────────────────────── مؤشرات ───────────────────────────
def ema_series(values: pd.Series, length: int) -> pd.Series:
    """متوسط متحرك أسي (نفس صيغة الباك-تست: seed = متوسط أول length قيمة)."""
    values = pd.Series(values, dtype="float64")
    out = pd.Series(np.nan, index=values.index, dtype="float64")
    if len(values) < length:
        return out
    out.iloc[length - 1] = values.iloc[:length].mean()
    alpha = 2.0 / (length + 1.0)
    prev = out.iloc[length - 1]
    for i in range(length, len(values)):
        prev = prev + alpha * (values.iloc[i] - prev)
        out.iloc[i] = prev
    return out


def atr_series(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """ATR بصيغة Wilder (RMA) على أعمدة high/low/close."""
    high = pd.Series(df["high"], dtype="float64").reset_index(drop=True)
    low = pd.Series(df["low"], dtype="float64").reset_index(drop=True)
    close = pd.Series(df["close"], dtype="float64").reset_index(drop=True)
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    out = pd.Series(np.nan, index=tr.index, dtype="float64")
    if len(tr) < length:
        return out
    out.iloc[length - 1] = tr.iloc[:length].mean()
    prev = out.iloc[length - 1]
    for i in range(length, len(tr)):
        prev = prev + (tr.iloc[i] - prev) / length
        out.iloc[i] = prev
    return out


def closed_bars(df: pd.DataFrame, now_ms: int) -> pd.DataFrame:
    """يُبقي الشموع المغلقة فقط (close_time < الآن)."""
    if df.empty:
        return df
    return df[df["close_time"].astype("int64") < int(now_ms)].reset_index(drop=True)


# ─────────────────────────── بوابة السوق ───────────────────────────
def market_gate(
    btc_4h: pd.DataFrame, now_ms: int, ema_len: int = 200
) -> dict:
    """حالة السوق: هل BTC فوق EMA200 على 4 ساعات؟ (آخر شمعة 4h مغلقة فقط)."""
    closed = closed_bars(btc_4h, now_ms)
    if len(closed) < ema_len + 1:
        return {"open": False, "close": float("nan"), "ema": float("nan"), "reason": "بيانات غير كافية"}
    ema = ema_series(closed["close"], ema_len)
    last = len(closed) - 1
    c, e = float(closed["close"].iloc[last]), float(ema.iloc[last])
    ok = bool(np.isfinite(e) and c > e)
    return {"open": ok, "close": c, "ema": e,
            "reason": "فوق المتوسط" if ok else "تحت المتوسط"}


# ─────────────────────────── إشارة الدخول ───────────────────────────
def breakout_signal(
    daily: pd.DataFrame, now_ms: int, settings: DailyTrendSettings
) -> dict | None:
    """هل أغلق آخر يوم فوق أعلى سعر للـlookback يومًا السابقة؟"""
    closed = closed_bars(daily, now_ms)
    need = settings.lookback + 1
    if len(closed) < need:
        return None
    last = closed.iloc[-1]
    prior = closed.iloc[-need:-1]                      # الشموع السابقة فقط (لا نظر للمستقبل)
    hh = float(prior["high"].max())
    entry = float(last["close"])
    if not np.isfinite(entry) or not np.isfinite(hh) or entry <= hh:
        return None
    atr_s = atr_series(closed, settings.atr_len)
    atr = float(atr_s.iloc[-1])
    if not np.isfinite(atr) or atr <= 0:
        return None
    stop = entry - settings.atr_mult * atr
    stop_pct = (entry - stop) / entry * 100.0
    if stop_pct <= 0 or stop_pct < settings.min_stop_pct or stop_pct > settings.max_stop_pct:
        return None                                        # وقف غير منطقي
    return {
        "symbol": None,
        "entry_price": entry,
        "stop_price": stop,
        "atr": atr,
        "stop_pct": stop_pct,
        "breakout_high": hh,
        "breakout_pct": (entry / hh - 1.0) * 100.0,
        "bar_close_time": int(last["close_time"]),
        "bar_open_time": int(last["open_time"]),
    }


# ─────────────────────────── إدارة المركز ───────────────────────────
def update_trailing_stop(
    stop: float, high_since: float, atr_entry: float, atr_mult: float
) -> float:
    """وقف متحرك لا يتراجع: max(الوقف الحالي، أعلى سعر − mult×ATR)."""
    candidate = float(high_since) - float(atr_mult) * float(atr_entry)
    return float(max(float(stop), candidate))


def exit_reason(position: dict, price: float, gate_open: bool) -> str | None:
    """سبب الإغلاق: 'stop' عند لمس الوقف، 'gate' عند إغلاق بوابة السوق، وإلا None."""
    if price is not None and np.isfinite(price) and float(price) <= float(position["stop_price"]):
        return "stop"
    if not gate_open:
        return "gate"
    return None


def position_notional(
    equity: float, risk_pct: float, entry: float, stop: float, max_positions: int
) -> float:
    """قيمة المركز بالدولار: (الرصيد × المخاطرة%) ÷ مسافة الوقف%، بحد أقصى الرصيد/عدد المراكز."""
    stop_pct = (float(entry) - float(stop)) / float(entry)
    if stop_pct <= 0 or equity <= 0:
        return 0.0
    risk_amount = float(equity) * float(risk_pct) / 100.0
    notional = risk_amount / stop_pct
    cap = float(equity) / max(1, int(max_positions))
    return float(min(notional, cap))


def net_pnl_pct(entry: float, exit_price: float, commission_side_pct: float = 0.1) -> float:
    """صافي نسبة الصفقة بعد العمولة (ذهابًا وعودة)."""
    return (float(exit_price) / float(entry) - 1.0) * 100.0 - 2.0 * float(commission_side_pct)


def position_pnl_usd(position: dict, exit_price: float, commission_side_pct: float = 0.1) -> float:
    """ربح/خسارة المركز بالدولار."""
    notional = float(position["notional"])
    return notional * net_pnl_pct(position["entry_price"], exit_price, commission_side_pct) / 100.0
