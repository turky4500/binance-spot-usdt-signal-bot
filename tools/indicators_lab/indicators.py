"""تنفيذ بايثون لمؤشرات مفتوحة المصدر من TradingView — كلها تُحسب على شموع مغلقة فقط (بلا إعادة رسم)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def rma(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False, min_periods=length).mean()


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length, min_periods=length).mean()


def smma(series: pd.Series, length: int) -> pd.Series:
    """Smoothed MA بأسلوب Pine's ta.smma: أول قيمة = SMA ثم متوسط متحرك أُسّي بسيط."""
    return series.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    return rma(true_range(df), length)


# ----------------------------------------------------------------------------
# SuperTrend (KivancOzbilgic)
# ----------------------------------------------------------------------------
def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    atr_ = atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2.0
    upper = (hl2 + multiplier * atr_).to_numpy(dtype=float)
    lower = (hl2 - multiplier * atr_).to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n = len(df)

    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    direction = np.ones(n)
    trend = np.full(n, np.nan)

    atr_arr = atr_.to_numpy(dtype=float) if hasattr(atr_, "to_numpy") else np.asarray(atr_)
    start = int(np.argmax(~np.isnan(atr_arr))) if not np.all(np.isnan(atr_arr)) else None
    if start is None or start >= n - 1:
        out = pd.DataFrame({"st_trend": trend, "st_dir": direction}, index=df.index)
        out["st_buy"] = False
        return out
    final_upper[start] = upper[start]
    final_lower[start] = lower[start]
    trend[start] = final_lower[start]
    direction[start] = 1

    for i in range(start + 1, n):
        if np.isnan(upper[i]):
            continue
        final_upper[i] = min(upper[i], final_upper[i - 1]) if close[i - 1] <= final_upper[i - 1] else upper[i]
        final_lower[i] = max(lower[i], final_lower[i - 1]) if close[i - 1] >= final_lower[i - 1] else lower[i]
        if close[i] > final_upper[i - 1]:
            direction[i] = 1
        elif close[i] < final_lower[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
        trend[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    out = pd.DataFrame({"st_trend": trend, "st_dir": direction}, index=df.index)
    out["st_buy"] = (out["st_dir"] == 1) & (out["st_dir"].shift(1) == -1)
    return out


# ----------------------------------------------------------------------------
# WaveTrend (LazyBear)
# ----------------------------------------------------------------------------
def wavetrend(df: pd.DataFrame, n1: int = 10, n2: int = 21, wt_ma_len: int = 4) -> pd.DataFrame:
    ap = (df["high"] + df["low"] + df["close"]) / 3.0
    esa = ema(ap, n1)
    d = ema((ap - esa).abs(), n1)
    ci = (ap - esa) / (0.015 * d.replace(0, np.nan))
    wt1 = ema(ci, n2)
    wt2 = sma(wt1, wt_ma_len)
    out = pd.DataFrame({"wt1": wt1, "wt2": wt2}, index=df.index)
    out["wt_cross_up"] = (wt1 > wt2) & (wt1.shift(1) <= wt2.shift(1))
    out["wt_oversold_cross"] = out["wt_cross_up"] & (wt1 < -30)
    return out


# ----------------------------------------------------------------------------
# Squeeze Momentum (LazyBear)
# ----------------------------------------------------------------------------
def squeeze_momentum(df: pd.DataFrame, bb_len: int = 20, bb_mult: float = 2.0, kc_len: int = 20, kc_mult: float = 1.5) -> pd.DataFrame:
    close = df["close"]
    basis = sma(close, bb_len)
    dev = bb_mult * close.rolling(bb_len, min_periods=bb_len).std(ddof=0)
    bb_upper, bb_lower = basis + dev, basis - dev

    tr = true_range(df)
    kc_basis = sma(close, kc_len)
    kc_range = sma(tr, kc_len)
    kc_upper = kc_basis + kc_mult * kc_range
    kc_lower = kc_basis - kc_mult * kc_range

    squeeze_on = (bb_lower > kc_lower) & (bb_upper < kc_upper)
    squeeze_released = (~squeeze_on) & squeeze_on.shift(1)

    # قيمة المومنتوم: انحدار خطي على (close - متوسط النطاق)
    dc_mid = (df["high"].rolling(kc_len, min_periods=kc_len).max()
              + df["low"].rolling(kc_len, min_periods=kc_len).min()) / 2.0
    avg = (dc_mid + sma(close, kc_len)) / 2.0
    delta = close - avg
    x = np.arange(bb_len)
    mom = delta.rolling(bb_len, min_periods=bb_len).apply(
        lambda y: np.polyfit(x[: len(y)], y, 1)[0] if len(y) == bb_len else np.nan, raw=True
    )

    out = pd.DataFrame({"sqz_on": squeeze_on, "sqz_released": squeeze_released, "sqz_mom": mom}, index=df.index)
    out["sqz_buy"] = out["sqz_released"] & (mom > 0)
    out["sqz_buy_rising"] = out["sqz_buy"] & (mom > mom.shift(1))
    return out


# ----------------------------------------------------------------------------
# Impulse MACD (LazyBear)
# ----------------------------------------------------------------------------
def impulse_macd(df: pd.DataFrame, length_ma: int = 34, length_signal: int = 9) -> pd.DataFrame:
    src = (df["high"] + df["low"] + df["close"]) / 3.0
    hi = smma(df["high"], length_ma)
    lo = smma(df["low"], length_ma)
    md = smma(src, length_ma)
    md = md.where(~((md > lo) & (md < hi)), md)
    md = np.where(md < lo, lo, np.where(md > hi, hi, md))
    md = pd.Series(md, index=df.index)
    impulse = md - sma(md, length_signal)
    signal_line = sma(impulse, length_signal)
    out = pd.DataFrame({"imp": impulse, "imp_signal": signal_line}, index=df.index)
    out["imp_buy"] = (impulse > signal_line) & (impulse.shift(1) <= signal_line.shift(1))
    out["imp_buy_pos"] = out["imp_buy"] & (impulse > 0)
    return out


# ----------------------------------------------------------------------------
# UT Bot Alerts (QuantNomad)
# ----------------------------------------------------------------------------
def ut_bot(df: pd.DataFrame, atr_period: int = 10, key_value: float = 1.0) -> pd.DataFrame:
    a = atr(df, atr_period).to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n = len(df)
    stop = np.full(n, np.nan)
    valid = np.where(~np.isnan(a))[0]
    if len(valid) == 0:
        out = pd.DataFrame({"ut_stop": stop}, index=df.index)
        out["ut_buy"] = False
        return out
    start = int(valid[0])
    stop[start] = close[start] - key_value * a[start]
    for i in range(start + 1, n):
        if np.isnan(a[i]) or np.isnan(stop[i - 1]):
            continue
        n_loss = key_value * a[i]
        prev = stop[i - 1]
        if close[i] > prev and close[i - 1] > prev:
            stop[i] = max(prev, close[i] - n_loss)
        elif close[i] < prev and close[i - 1] < prev:
            stop[i] = min(prev, close[i] + n_loss)
        elif close[i] > prev:
            stop[i] = close[i] - n_loss
        else:
            stop[i] = close[i] + n_loss
    stop_s = pd.Series(stop, index=df.index)
    close_s = pd.Series(close, index=df.index)
    out = pd.DataFrame({"ut_stop": stop_s}, index=df.index)
    out["ut_buy"] = (close_s > stop_s) & (close_s.shift(1) <= stop_s.shift(1))
    return out
