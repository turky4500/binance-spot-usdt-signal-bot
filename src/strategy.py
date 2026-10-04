from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(slots=True)
class StrategySettings:
    reversal_mode: str = "متوازن"
    ema_fast_len: int = 20
    ema_mid_len: int = 50
    ema_slow_len: int = 200
    rsi_len: int = 14
    rsi_oversold: float = 32.0
    rsi_overbought: float = 68.0
    stoch_len: int = 14
    stoch_oversold: float = 25.0
    stoch_overbought: float = 75.0
    bb_len: int = 20
    bb_mult: float = 2.0
    volume_len: int = 20
    min_hourly_quote_vol: float = 20000.0
    target_pct: float = 2.0
    commission_per_side_pct: float = 0.1
    atr_len: int = 14
    stop_buffer_atr: float = 0.20
    max_stop_pct: float = 2.5
    min_reward_risk: float = 0.75
    use_trend_filter: bool = True
    use_adx_filter: bool = True
    adx_len: int = 14
    adx_threshold: float = 25.0
    min_bars_between_pivots: int = 10

    @property
    def pivot_left(self) -> int:
        if self.reversal_mode == "مبكر":
            return 2
        if self.reversal_mode == "مؤكد":
            return 5
        return 3

    @property
    def pivot_right(self) -> int:
        if self.reversal_mode == "مبكر":
            return 1
        if self.reversal_mode == "مؤكد":
            return 3
        return 2

    @property
    def minimum_score(self) -> int:
        return 3 if self.reversal_mode == "مؤكد" else 2

    @property
    def min_rel_volume(self) -> float:
        if self.reversal_mode == "مبكر":
            return 0.55
        if self.reversal_mode == "مؤكد":
            return 1.10
        return 0.75


def rma(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False, min_periods=length).mean()


def rsi(series: pd.Series, length: int) -> pd.Series:
    delta = series.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = rma(gains, length)
    avg_loss = rma(losses, length)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    output = 100 - (100 / (1 + rs))
    output = output.where(avg_loss != 0, 100)
    return output


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


def stochastic(df: pd.DataFrame, length: int) -> pd.Series:
    lowest_low = df["low"].rolling(length).min()
    highest_high = df["high"].rolling(length).max()
    denom = (highest_high - lowest_low).replace(0, np.nan)
    raw = 100 * (df["close"] - lowest_low) / denom
    return raw.rolling(3).mean()


def bollinger(series: pd.Series, length: int, mult: float) -> tuple[pd.Series, pd.Series, pd.Series]:
    basis = series.rolling(length).mean()
    dev = series.rolling(length).std(ddof=0)
    upper = basis + mult * dev
    lower = basis - mult * dev
    return basis, upper, lower


def macd(series: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    fast = ema(series, 12)
    slow = ema(series, 26)
    line = fast - slow
    signal = line.ewm(span=9, adjust=False, min_periods=9).mean()
    hist = line - signal
    return line, signal, hist


def dmi(df: pd.DataFrame, length: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    up_move = df["high"].diff()
    down_move = -df["low"].diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    tr_rma = rma(tr, length)
    plus_rma = rma(pd.Series(plus_dm, index=df.index), length)
    minus_rma = rma(pd.Series(minus_dm, index=df.index), length)

    plus_di = 100 * plus_rma / tr_rma.replace(0, np.nan)
    minus_di = 100 * minus_rma / tr_rma.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = rma(dx, length)
    return plus_di, minus_di, adx


def pivot_low_series(low: pd.Series, left: int, right: int) -> pd.Series:
    values = [np.nan] * len(low)
    arr = low.to_numpy(dtype=float)
    for t in range(left + right, len(arr)):
        candidate_idx = t - right
        window = arr[candidate_idx - left : candidate_idx + right + 1]
        if np.isnan(window).any():
            continue
        candidate = arr[candidate_idx]
        if candidate == np.min(window):
            values[t] = candidate
    return pd.Series(values, index=low.index)


def pivot_high_series(high: pd.Series, left: int, right: int) -> pd.Series:
    values = [np.nan] * len(high)
    arr = high.to_numpy(dtype=float)
    for t in range(left + right, len(arr)):
        candidate_idx = t - right
        window = arr[candidate_idx - left : candidate_idx + right + 1]
        if np.isnan(window).any():
            continue
        candidate = arr[candidate_idx]
        if candidate == np.max(window):
            values[t] = candidate
    return pd.Series(values, index=high.index)


def compute_entry_signal(
    df: pd.DataFrame,
    settings: StrategySettings,
    has_open_trade: bool,
    last_exit_bar_time: int | None,
) -> dict[str, Any] | None:
    if df.empty or len(df) < settings.ema_slow_len + settings.pivot_left + settings.pivot_right + 20:
        return None

    data = df.copy().reset_index(drop=True)

    data["ema_fast"] = ema(data["close"], settings.ema_fast_len)
    data["ema_mid"] = ema(data["close"], settings.ema_mid_len)
    data["ema_slow"] = ema(data["close"], settings.ema_slow_len)
    data["rsi"] = rsi(data["close"], settings.rsi_len)
    data["stoch"] = stochastic(data, settings.stoch_len)
    _, _, data["macd_hist"] = macd(data["close"])
    data["atr"] = atr(data, settings.atr_len)
    _, data["bb_upper"], data["bb_lower"] = bollinger(data["close"], settings.bb_len, settings.bb_mult)
    data["plus_di"], data["minus_di"], data["adx"] = dmi(data, settings.adx_len)

    data["prior_volume_avg"] = data["volume"].shift(1).rolling(settings.volume_len).mean()
    data["relative_volume"] = data["volume"] / data["prior_volume_avg"]
    data["quote_volume"] = data["close"] * data["volume"]
    data["liquidity_ok"] = data["quote_volume"] >= settings.min_hourly_quote_vol

    data["pivot_low"] = pivot_low_series(data["low"], settings.pivot_left, settings.pivot_right)
    data["pivot_high"] = pivot_high_series(data["high"], settings.pivot_left, settings.pivot_right)

    data["rsi_at_low_pivot"] = data["rsi"].shift(settings.pivot_right)
    data["rsi_at_high_pivot"] = data["rsi"].shift(settings.pivot_right)
    data["stoch_at_low_pivot"] = data["stoch"].shift(settings.pivot_right)
    data["stoch_at_high_pivot"] = data["stoch"].shift(settings.pivot_right)
    data["lower_band_at_low_pivot"] = data["bb_lower"].shift(settings.pivot_right)
    data["upper_band_at_high_pivot"] = data["bb_upper"].shift(settings.pivot_right)

    bullish_div = [False] * len(data)
    bearish_div = [False] * len(data)
    previous_low_pivot_price = np.nan
    previous_low_pivot_rsi = np.nan
    previous_high_pivot_price = np.nan
    previous_high_pivot_rsi = np.nan
    last_pivot_low_bar: int | None = None
    last_pivot_high_bar: int | None = None

    for i in range(len(data)):
        pivot_low = data.at[i, "pivot_low"]
        if not pd.isna(pivot_low):
            valid_gap = last_pivot_low_bar is None or (i - last_pivot_low_bar >= settings.min_bars_between_pivots)
            if valid_gap:
                current_rsi = data.at[i, "rsi_at_low_pivot"]
                bullish_div[i] = (
                    not pd.isna(previous_low_pivot_price)
                    and not pd.isna(previous_low_pivot_rsi)
                    and not pd.isna(current_rsi)
                    and pivot_low < previous_low_pivot_price
                    and current_rsi > previous_low_pivot_rsi
                )
                previous_low_pivot_price = pivot_low
                previous_low_pivot_rsi = current_rsi
                last_pivot_low_bar = i

        pivot_high = data.at[i, "pivot_high"]
        if not pd.isna(pivot_high):
            valid_gap = last_pivot_high_bar is None or (i - last_pivot_high_bar >= settings.min_bars_between_pivots)
            if valid_gap:
                current_rsi = data.at[i, "rsi_at_high_pivot"]
                bearish_div[i] = (
                    not pd.isna(previous_high_pivot_price)
                    and not pd.isna(previous_high_pivot_rsi)
                    and not pd.isna(current_rsi)
                    and pivot_high > previous_high_pivot_price
                    and current_rsi < previous_high_pivot_rsi
                )
                previous_high_pivot_price = pivot_high
                previous_high_pivot_rsi = current_rsi
                last_pivot_high_bar = i

    data["bullish_divergence"] = bullish_div
    data["bearish_divergence"] = bearish_div

    data["oversold_at_pivot"] = (
        data["pivot_low"].notna()
        & (
            (data["rsi_at_low_pivot"] <= settings.rsi_oversold)
            | (data["stoch_at_low_pivot"] <= settings.stoch_oversold)
            | (data["pivot_low"] <= data["lower_band_at_low_pivot"])
        )
    )
    data["overbought_at_pivot"] = (
        data["pivot_high"].notna()
        & (
            (data["rsi_at_high_pivot"] >= settings.rsi_overbought)
            | (data["stoch_at_high_pivot"] >= settings.stoch_overbought)
            | (data["pivot_high"] >= data["upper_band_at_high_pivot"])
        )
    )

    data["bullish_candle_now"] = (data["close"] > data["open"]) & (data["close"] >= data["close"].shift(1))
    data["bearish_candle_now"] = (data["close"] < data["open"]) & (data["close"] <= data["close"].shift(1))

    data["bullish_momentum_now"] = (
        (data["close"] > data["ema_fast"])
        | (data["macd_hist"] > data["macd_hist"].shift(1))
        | (data["rsi"] > data["rsi"].shift(1))
    )
    data["bearish_momentum_now"] = (
        (data["close"] < data["ema_fast"])
        | (data["macd_hist"] < data["macd_hist"].shift(1))
        | (data["rsi"] < data["rsi"].shift(1))
    )

    data["volume_confirm"] = data["relative_volume"] >= settings.min_rel_volume
    data["buy_score"] = (
        data["oversold_at_pivot"].astype(int)
        + data["bullish_divergence"].astype(int)
        + data["bullish_candle_now"].astype(int)
        + data["bullish_momentum_now"].astype(int)
        + data["volume_confirm"].astype(int)
    )

    data["pivot_low_atr"] = data["atr"].shift(settings.pivot_right)
    data["pivot_stop_candidate"] = data["pivot_low"] - (data["pivot_low_atr"] * settings.stop_buffer_atr)
    data["buy_risk_pct"] = ((data["close"] - data["pivot_stop_candidate"]) / data["close"]) * 100.0
    data["reward_risk_ratio"] = max(settings.target_pct - (2.0 * settings.commission_per_side_pct), 0.0) / data["buy_risk_pct"]
    data["buy_risk_ok"] = (
        data["buy_risk_pct"].notna()
        & (data["buy_risk_pct"] > 0)
        & (data["buy_risk_pct"] <= settings.max_stop_pct)
        & data["reward_risk_ratio"].notna()
        & (data["reward_risk_ratio"] >= settings.min_reward_risk)
    )

    enough_history_threshold = settings.ema_slow_len + settings.pivot_left + settings.pivot_right + 10
    data["enough_history"] = (
        data.index > enough_history_threshold
    ) & data["rsi"].shift(settings.pivot_right).notna() & data["atr"].shift(settings.pivot_right).notna()

    data["bullish_trend_ok"] = data["close"] > data["ema_slow"]
    data["adx_buy_ok"] = (data["adx"] < settings.adx_threshold) | (data["plus_di"] > data["minus_di"])

    data["buy_setup"] = (
        data["enough_history"]
        & data["liquidity_ok"]
        & data["pivot_low"].notna()
        & (data["buy_score"] >= settings.minimum_score)
        & data["buy_risk_ok"]
        & data["bullish_trend_ok"]
        & data["adx_buy_ok"]
    )

    last = data.iloc[-1]
    current_bar_open_time = int(last["open_time"])

    if has_open_trade:
        return None

    if last_exit_bar_time is not None:
        bars_since_exit = (current_bar_open_time - int(last_exit_bar_time)) // (60 * 60 * 1000)
        cooldown_ok = bars_since_exit > 2
    else:
        cooldown_ok = True

    if not cooldown_ok or not bool(last["buy_setup"]):
        return None

    entry_price = float(last["close"])
    stop_price = float(last["pivot_stop_candidate"])
    target_price = entry_price * (1.0 + settings.target_pct / 100.0)
    strong = bool(last["buy_score"] >= settings.minimum_score + 1)

    return {
        "symbol": str(last.get("symbol", "")),
        "bar_open_time": current_bar_open_time,
        "bar_close_time": int(last["close_time"]),
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "strong": strong,
        "buy_score": int(last["buy_score"]),
        "mode": settings.reversal_mode,
    }
