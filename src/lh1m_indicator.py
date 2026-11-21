# SPDX-License-Identifier: MPL-2.0
# Trend Lines pivot-marker logic adapted from LonesomeTheBlue's Pine Script v4,
# "Trend Lines" (© LonesomeTheBlue), supplied by the user. MPL-2.0:
# https://mozilla.org/MPL/2.0/. This file implements only its H/L markers, not
# trend-line breakouts or trading. The 30-right-bar offset is never lookahead.
"""Confirmed 1m pivot labels from the supplied Trend Lines indicator.

The Pine code uses highestbars/lowestbars, not trend-line breakouts. Following
PineTS 0.13.1's equivalent Pine v5 implementation, equal extrema select the
NEWEST bar: an equal older extreme is allowed; an equal newer one vetoes the
candidate. The supplied Pine v4 was not independently run on TradingView.
The extra high[mb]/low[mb] check requires mb+1 bars, not mb.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

MINUTE_MS = 60_000


@dataclass(frozen=True)
class Bar:
    open_ms: int
    high: float
    low: float
    close: float
    volume: float = 0.0
    trades: int = 0


@dataclass(frozen=True)
class Label:
    kind: str               # L or H, NOT the line-angle condition
    pivot_open_ms: int      # where TradingView draws the label
    confirmation_open_ms: int
    confirmation_time_ms: int  # close of the confirmation candle
    confirmation_close: float  # reference price; not an executed price
    pivot_price: float
    confirmation_trades: int
    confirmation_volume: float


def normalize_klines(raw: Iterable[Any], server_now_ms: int) -> list[Bar]:
    """Validate Binance 1m klines and retain ONLY fully closed candles.

    Gaps, duplicates, nonfinite/invalid OHLC or non-1m alignment fail closed.
    No resampling, filling, or forward-filling is allowed.
    """
    bars: list[Bar] = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 9:
            raise ValueError("invalid kline shape")
        open_ms = int(row[0])
        if open_ms % MINUTE_MS:
            raise ValueError("unaligned candle")
        if open_ms + MINUTE_MS > int(server_now_ms):
            continue  # incomplete realtime candle; never infer an L/H from it
        high, low, close = float(row[2]), float(row[3]), float(row[4])
        if not all(math.isfinite(v) and v > 0 for v in (high, low, close)):
            raise ValueError("nonpositive/nonfinite price")
        if low > high or close < low or close > high:
            raise ValueError("inconsistent OHLC")
        volume, trades = float(row[5]), int(row[8])
        if not math.isfinite(volume) or volume < 0 or trades < 0:
            raise ValueError("invalid volume or trade count")
        if bars and open_ms - bars[-1].open_ms != MINUTE_MS:
            raise ValueError("duplicate, unsorted or missing minute candles")
        bars.append(Bar(open_ms, high, low, close, volume, trades))
    return bars


def confirmed_labels(bars: list[Bar], left: int = 30, right: int = 30,
                     after_open_ms: int = -1) -> list[Label]:
    """Return H/L only when their *confirmation candle* is closed.

    ``after_open_ms`` is a cursor on the confirmation candle, not the pivot.
    H is ordered before L when both become confirmed on the same bar so a
    long-only position is closed before any new entry. This ordering is an
    explicit execution convention; the supplied indicator does not specify it.
    """
    if left < 1 or right < 1:
        raise ValueError("left and right must be positive")
    width = left + right + 1  # mb in Pine
    result: list[Label] = []
    # Pine's `not na(high[mb])` / `not na(low[mb])` needs index >= width.
    for i in range(width, len(bars)):
        current = bars[i]
        if current.open_ms <= after_open_ms:
            continue
        p = i - right
        pivot = bars[p]
        older = bars[p-left:p]
        newer = bars[p+1:i+1]
        h = all(pivot.high >= b.high for b in older) and all(pivot.high > b.high for b in newer)
        l = all(pivot.low <= b.low for b in older) and all(pivot.low < b.low for b in newer)
        for kind, active, price in (("H", h, pivot.high), ("L", l, pivot.low)):
            if active:
                result.append(Label(
                    kind, pivot.open_ms, current.open_ms,
                    current.open_ms + MINUTE_MS, current.close, price,
                    current.trades, current.volume,
                ))
    return result
