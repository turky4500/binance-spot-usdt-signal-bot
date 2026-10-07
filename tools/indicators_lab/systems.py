"""توليد إشارات الدخول لكل نظام: منطق البوت الحالي + المؤشرات المجانية المفتوحة المصدر."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.strategy import StrategySettings, prepare_strategy_frame  # noqa: E402

from . import indicators as ind  # noqa: E402


def linreg_slope(series: pd.Series, length: int) -> pd.Series:
    """انحدار خطي متحرك (نفس logic التربيع الأصغر) بحساب متجهي سريع."""
    y = series.to_numpy(dtype=float)
    n = len(y)
    out = np.full(n, np.nan)
    if n < length:
        return pd.Series(out, index=series.index)
    x = np.arange(length, dtype=float)
    x_bar = x.mean()
    weights = (x - x_bar)
    sxx = float((weights ** 2).sum())
    kernel = weights[::-1] / sxx
    valid = np.convolve(y, kernel, mode="valid")
    out[length - 1:] = valid[: n - length + 1]
    return pd.Series(out, index=series.index)


def _squeeze_momentum_fast(df: pd.DataFrame, bb_len: int = 20, bb_mult: float = 2.0, kc_len: int = 20, kc_mult: float = 1.5) -> pd.DataFrame:
    close = df["close"]
    basis = ind.sma(close, bb_len)
    dev = bb_mult * close.rolling(bb_len, min_periods=bb_len).std(ddof=0)
    bb_upper, bb_lower = basis + dev, basis - dev
    tr = ind.true_range(df)
    kc_basis = ind.sma(close, kc_len)
    kc_range = ind.sma(tr, kc_len)
    squeeze_on = (bb_lower > (kc_basis - kc_mult * kc_range)) & (bb_upper < (kc_basis + kc_mult * kc_range))
    squeeze_released = (~squeeze_on) & squeeze_on.shift(1)
    dc_mid = (df["high"].rolling(kc_len, min_periods=kc_len).max() + df["low"].rolling(kc_len, min_periods=kc_len).min()) / 2.0
    avg = (dc_mid + ind.sma(close, kc_len)) / 2.0
    mom = linreg_slope(close - avg, bb_len)
    out = pd.DataFrame({"sqz_released": squeeze_released, "sqz_mom": mom}, index=df.index)
    out["sqz_buy"] = out["sqz_released"] & (mom > 0)
    return out


def build_all_systems(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """يرجّع قاموس: اسم النظام → DataFrame فيه عمود entry (نعم/لا) واختياريًا stop_pct."""
    settings = StrategySettings()
    frame = prepare_strategy_frame(df, settings)

    rvol = frame["relative_volume"]
    di_ok = frame["plus_di"] > frame["minus_di"]
    vol_band = (rvol >= 1.2) & (rvol <= 2.5)

    systems: dict[str, pd.DataFrame] = {}

    # --- منطق البوت الحالي (قبل الفلاتر الجديدة) ---
    bot = pd.DataFrame(index=df.index)
    bot["entry"] = frame["buy_setup"].fillna(False)
    bot_stop_pct = ((frame["close"] - frame["pivot_stop_candidate"]) / frame["close"] * 100.0).clip(upper=settings.max_stop_pct)
    bot["stop_pct"] = bot_stop_pct
    systems["bot_current_pivot_stop"] = bot[["entry", "stop_pct"]]

    # --- منطق البوت الحالي بوقف موحّد (لعزل أثر قاعدة الخروج) ---
    systems["bot_current_uniform_stop"] = bot[["entry"]]

    # --- البوت + الفلاتر الجديدة (الإعداد الحالي على المستودع) ---
    gated = pd.DataFrame(index=df.index)
    gated["entry"] = frame["buy_setup"].fillna(False) & vol_band.fillna(False) & di_ok.fillna(False)
    systems["bot_gated"] = gated

    # --- المؤشرات المجانية ---
    st = ind.supertrend(df, period=10, multiplier=3.0)
    systems["supertrend_10_3"] = pd.DataFrame({"entry": st["st_buy"].fillna(False)}, index=df.index)

    st_fast = ind.supertrend(df, period=10, multiplier=2.0)
    systems["supertrend_10_2"] = pd.DataFrame({"entry": st_fast["st_buy"].fillna(False)}, index=df.index)

    wt = ind.wavetrend(df)
    systems["wavetrend_cross"] = pd.DataFrame({"entry": wt["wt_cross_up"].fillna(False)}, index=df.index)
    systems["wavetrend_oversold"] = pd.DataFrame({"entry": wt["wt_oversold_cross"].fillna(False)}, index=df.index)

    sqz = _squeeze_momentum_fast(df)
    systems["squeeze_momentum"] = pd.DataFrame({"entry": sqz["sqz_buy"].fillna(False)}, index=df.index)

    imp = ind.impulse_macd(df)
    systems["impulse_macd"] = pd.DataFrame({"entry": imp["imp_buy"].fillna(False)}, index=df.index)
    systems["impulse_macd_pos"] = pd.DataFrame({"entry": imp["imp_buy_pos"].fillna(False)}, index=df.index)

    ut = ind.ut_bot(df, atr_period=10, key_value=1.0)
    systems["ut_bot"] = pd.DataFrame({"entry": ut["ut_buy"].fillna(False)}, index=df.index)

    return systems


BOT_SYSTEMS = {"bot_current_pivot_stop", "bot_current_uniform_stop", "bot_gated"}
