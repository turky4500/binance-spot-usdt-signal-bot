"""Nadaraya-Watson Smoothers [LuxAlgo] — faithful Python port.

Original indicator © LuxAlgo, licensed under CC BY-NC-SA 4.0:
https://creativecommons.org/licenses/by-nc-sa/4.0/
This file is a derivative work: the same estimator, ported to Python/NumPy.
Attribution is kept here and in README_NW.md.

Two modes, exactly as the Pine ``repaint`` input switches them:

* ``repaint = false`` → **Endpoint estimator** (causal, no look-ahead):
      out = sum(src[i] * gauss(i, h)) / sum(gauss(i, h)),  i = 0..499
  Pine plots ``out`` colored by ``out > out[1]``. No labels in this mode.

* ``repaint = true`` → **Full NW curve** recomputed on the last bar only:
      y[i] = sum(src[j] * gauss(i-j, h)) / sum(gauss(i-j, h)),  i,j = 0..499
  Pine draws segments and puts ▲ / ▼ labels wherever the slope d[i] = y[i]-y[i-1]
  changes sign (a local trough / peak of the smoothed curve).

IMPORTANT — honest caveat: the ``repaint = true`` curve is recomputed from the
whole 500-bar window on every new bar, so every historical y[i] AND every
historical ▲/▼ label can move or disappear retroactively. It is a *visual*
tool, not a causal signal. Use the endpoint mode (or an explicitly lagged
confirmation of the repaint mode) for anything live or backtested.

Defaults mirror the script: h = 8, window = 500 bars, source = close.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Sequence

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

NW_WINDOW = 500
NW_BANDWIDTH = 8.0
NW_INTERVAL = "1h"          # فريم العمل المطلوب من المستخدم
NW_SOURCE = "close"


@dataclass(frozen=True, slots=True)
class NWConfig:
    """ثوابت المؤشر — تُكتب مرة واحدة ويُقرأ منها النظام المستقل لاحقًا."""

    bandwidth: float = NW_BANDWIDTH
    window: int = NW_WINDOW
    interval: str = NW_INTERVAL
    source: str = NW_SOURCE
    mode: str = "endpoint"   # "endpoint" (آمن/لا يعيد الرسم) | "repaint" (بحثي فقط)

    def __post_init__(self) -> None:
        if self.window < 2:
            raise ValueError("window must be >= 2")
        if self.bandwidth <= 0:
            raise ValueError("bandwidth must be > 0")
        if self.mode not in {"endpoint", "repaint"}:
            raise ValueError("mode must be 'endpoint' or 'repaint'")


def gauss(x, h: float = NW_BANDWIDTH) -> np.ndarray:
    """Gaussian window — Pine: math.exp(-(x^2)/(h^2*2))."""
    x = np.asarray(x, dtype=float)
    return np.exp(-(np.square(x) / (h * h * 2.0)))


@lru_cache(maxsize=8)
def _endpoint_kernel(h: float, window: int) -> np.ndarray:
    return gauss(np.arange(window, dtype=float), h)


@lru_cache(maxsize=8)
def _curve_kernel(h: float, window: int) -> np.ndarray:
    i = np.arange(window, dtype=float)[:, None]
    j = np.arange(window, dtype=float)[None, :]
    return gauss(i - j, h)   # Toeplitz: W[i, j] = gauss(i-j, h)


def endpoint_estimator(
    closes: Sequence[float] | Iterable[float],
    h: float = NW_BANDWIDTH,
    window: int = NW_WINDOW,
    align: bool = True,
) -> np.ndarray:
    """``repaint = false``: Gaussian-weighted mean of the last `window` closes.

    `closes` is chronological (oldest → newest); Pine's ``src[i]`` is
    ``closes[-1-i]``. Returns values aligned 1:1 with `closes` (first
    ``window-1`` entries are NaN because Pine needs 500 bars of history).
    Purely causal: out[t] depends only on closes[0..t].
    """
    arr = np.asarray(list(closes) if not isinstance(closes, np.ndarray) else closes, dtype=float).ravel()
    if arr.size < window:
        raise ValueError(f"need at least {window} bars for NW endpoint, got {arr.size}")
    w = _endpoint_kernel(float(h), int(window))
    win = sliding_window_view(arr, int(window))[:, ::-1]   # الأحدث أولًا، مثل src[0]
    out = (win @ w) / w.sum()
    if not align:
        return out
    full = np.full(arr.size, np.nan, dtype=float)
    full[window - 1:] = out
    return full


def nw_curve(
    closes: Sequence[float] | Iterable[float],
    h: float = NW_BANDWIDTH,
    window: int = NW_WINDOW,
) -> np.ndarray:
    """``repaint = true``: the full NW curve on the FINAL bar.

    Returns y[i] for i = 0 (current bar) .. window-1 (oldest bar in window).
    Recomputed from scratch on every new bar ⇒ historical values repaint.
    """
    arr = np.asarray(list(closes) if not isinstance(closes, np.ndarray) else closes, dtype=float).ravel()
    if arr.size < window:
        raise ValueError(f"need at least {window} bars for NW curve, got {arr.size}")
    src = arr[-window:][::-1]                 # src[0] = أحدث إغلاق
    W = _curve_kernel(float(h), int(window))
    return (W @ src) / W.sum(axis=1)


def repaint_extremum_signals(
    y: Sequence[float] | np.ndarray,
    closes: Sequence[float] | np.ndarray | None = None,
) -> list[dict]:
    """▲/▼ labels of the Pine script.

    At iteration i+1 Pine tests ``d * y1_d < 0`` where
    ``d = y[i+1]-y[i]`` and ``y1_d = y[i]-y[i-1]``, then draws the label at
    bar ``i`` (i bars ago): '▲' (buy) if ``y1_d < 0`` (trough), '▼' (sell)
    otherwise (peak). ``closes`` — when given — is chronological and supplies
    the price at that bar (Pine uses ``src[i]`` for the label's y position).
    """
    y = np.asarray(y, dtype=float).ravel()
    diff = np.diff(y)          # diff[k] = y[k+1] - y[k]
    prices = None
    if closes is not None:
        prices = np.asarray(list(closes) if not isinstance(closes, np.ndarray) else closes, dtype=float).ravel()
    out: list[dict] = []
    for k in range(len(diff) - 1):
        if diff[k] == 0.0 or diff[k + 1] == 0.0:
            continue                        # Pine: 0 < 0 غير محقق
        if diff[k] * diff[k + 1] < 0:
            i = k + 1                       # موضع القمة/القاع = i شمعة ماضية
            sig = {
                "bars_ago": i,
                "kind": "buy" if diff[k] < 0 else "sell",
                "nw": float(y[i]),
                "slope_before": float(diff[k]),
                "slope_after": float(diff[k + 1]),
            }
            if prices is not None and prices.size > i:
                sig["price"] = float(prices[-1 - i])
                if prices.size - 1 - i >= 0:
                    sig["index"] = int(prices.size - 1 - i)
            out.append(sig)
    return out


def endpoint_flip_signals(
    out: Sequence[float] | np.ndarray,
    closes: Sequence[float] | np.ndarray | None = None,
) -> list[dict]:
    """Causal signals from the endpoint estimator: slope flip at a trough/peak.

    Signal is emitted at bar t (the first bar where it is knowable), when
    ``s[t-1] * s[t] < 0`` with ``s[t] = out[t]-out[t-1]``. The extremum itself
    is at ``t-1`` (1-bar confirmation lag). No future data is used.
    """
    arr = np.asarray(out, dtype=float).ravel()
    prices = None
    if closes is not None:
        prices = np.asarray(list(closes) if not isinstance(closes, np.ndarray) else closes, dtype=float).ravel()
    s = np.diff(arr)
    res: list[dict] = []
    for k in range(1, len(s)):
        a, b = s[k - 1], s[k]
        if not (np.isfinite(a) and np.isfinite(b)) or a == 0.0 or b == 0.0:
            continue
        if a * b < 0:
            t = k + 1                        # فهرس out/closes الذي ظهرت عنده الإشارة
            sig = {
                "index": t,
                "extremum_index": t - 1,
                "bars_ago_at_emit": 1,
                "kind": "buy" if b > 0 else "sell",
                "nw": float(arr[t]),
            }
            if prices is not None and prices.size > t:
                sig["price"] = float(prices[t])
                sig["extremum_price"] = float(prices[t - 1])
            res.append(sig)
    return res
