"""نقل حرفي لـ «Nadaraya-Watson Envelope [LuxAlgo]» (Pine v5) الذي أرسله المستخدم.

الأصل © LuxAlgo، مرخّص CC BY-NC-SA 4.0:
https://creativecommons.org/licenses/by-nc-sa/4.0/

لا يوجد في هذا الملف أي اجتهاد: الثوابت والمعادلات وشروط التسميات منقولة كما هي
من الكود المرسل (h=8, mult=3, src=close, نافذة 500، repaint=true افتراضيًا).

Pine (وضع إعادة الرسم — وهو الظاهر على شارت المستخدم «Repainting Mode Enabled»):

    for i = 0 to min(499, n-1):
        nwe[i] = Σ_j src[j]·gauss(i-j,h) / Σ_j gauss(i-j,h)
    sae = Σ_i |src[i] - nwe[i]| / min(499, n-1) * mult      ← المقام 499 بالحرف
    ▲ على الشمعة n-i  إذا  src[i] < nwe[i]-sae  و  src[i+1] > nwe[i]-sae
    ▼ على الشمعة n-i  إذا  src[i] > nwe[i]+sae  و  src[i+1] < nwe[i]+sae

ملاحظة مطابقة مهمة: Pine يحسب هذا كله على ``barstate.islast``، أي أن ``src[0]``
هي الشمعة الحيّة. البوت يعمل على شموع مغلقة فقط (قاعدة المستخدم: «إذا أُغلقت
الشمعة وظهرت عليها إشارة...»)، فيُمرَّر ``src[0]`` = آخر شمعة مغلقة. الفرق بين
الحالتين شمعة حيّة واحدة، وهو موثّق هنا بدل أي تعديل على المعادلة.

Pine (وضع ``repaint=false``): endpoint estimator + ``mae``، وتُرسم الأسهم عبر
``ta.crossunder(close, out-mae)`` / ``ta.crossover(close, out+mae)``. منقول أيضًا
كما هو في ``endpoint_arrows``، لكنه يحتاج ≥1000 شمعة حتى يصبح ``mae`` صالحًا.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

ENVELOPE_H = 8.0
ENVELOPE_MULT = 3.0
ENVELOPE_WINDOW = 500
ENVELOPE_SAE_DIVISOR = 499          # math.min(499, n-1) في Pine بالحرف، لا 500


@dataclass(frozen=True, slots=True)
class EnvelopeConfig:
    bandwidth: float = ENVELOPE_H
    mult: float = ENVELOPE_MULT
    window: int = ENVELOPE_WINDOW
    repaint: bool = True            # input(true, 'Repainting Smoothing')


def gauss(x, h: float = ENVELOPE_H) -> np.ndarray:
    """Pine: math.exp(-(math.pow(x, 2)/(h * h * 2)))."""
    x = np.asarray(x, dtype=float)
    return np.exp(-(np.square(x) / (h * h * 2.0)))


@lru_cache(maxsize=8)
def _envelope_kernel(h: float, window: int) -> np.ndarray:
    i = np.arange(window, dtype=float)[:, None]
    j = np.arange(window, dtype=float)[None, :]
    return gauss(i - j, h)


def envelope_curve(src: np.ndarray, cfg: EnvelopeConfig | None = None) -> np.ndarray:
    """``nwe`` في Pine: src[0] = الأحدث، طول النافذة 500، نواة Toeplitz."""
    cfg = cfg or EnvelopeConfig()
    w = np.asarray(src, dtype=float).ravel()[:cfg.window]
    if w.size < cfg.window:
        raise ValueError(f"need {cfg.window} src values for the envelope, got {w.size}")
    K = _envelope_kernel(float(cfg.bandwidth), int(cfg.window))
    return (K @ w) / K.sum(axis=1)


def envelope_labels(src: np.ndarray, cfg: EnvelopeConfig | None = None) -> tuple[float, list[dict]]:
    """تسميات ▲/▼ كما يطبعها المؤشر على الشارت، بلا أي فلتر أو عتبة إضافية.

    ``src``: الأحدث أولًا (مثل ``src[i]`` في Pine)، ويجب أن يحتوي ``window+1``
    قيمة على الأقل لأن الشرط يقرأ ``src[i+1]`` عند i=499.
    يُعاد (sae, labels) وكل تسمية تحمل ``bars_ago`` = i و``price`` = src[i].
    """
    cfg = cfg or EnvelopeConfig()
    s = np.asarray(src, dtype=float).ravel()
    if s.size < cfg.window + 1:
        raise ValueError(f"need {cfg.window + 1} src values, got {s.size}")
    nwe = envelope_curve(s, cfg)
    w = s[:cfg.window]
    # sae := sae / math.min(499, n - 1) * mult   ← المقام 499 كما في الكود المرسل
    sae = float(np.abs(w - nwe).sum() / ENVELOPE_SAE_DIVISOR * cfg.mult)
    labels: list[dict] = []
    for i in range(cfg.window):
        if i + 1 >= s.size:
            break
        lower = float(nwe[i]) - sae
        upper = float(nwe[i]) + sae
        # label.new(n-i, src[i], '▲' ...)
        if s[i] < lower and s[i + 1] > lower:
            labels.append({"bars_ago": i, "kind": "buy", "price": float(s[i]),
                           "band": lower, "nwe": float(nwe[i]), "sae": sae})
        if s[i] > upper and s[i + 1] < upper:
            labels.append({"bars_ago": i, "kind": "sell", "price": float(s[i]),
                           "band": upper, "nwe": float(nwe[i]), "sae": sae})
    return sae, labels


# ── الفرع الثاني في الكود المرسل: repaint = false ────────────────────────────

def endpoint_estimator(closes: np.ndarray, cfg: EnvelopeConfig | None = None) -> np.ndarray:
    """``out`` في Pine: متوسط غاوسي سببي للـ500 شمعة الماضية، محاذى 1:1."""
    cfg = cfg or EnvelopeConfig()
    arr = np.asarray(closes, dtype=float).ravel()
    w = gauss(np.arange(cfg.window, dtype=float), cfg.bandwidth)
    win = sliding_window_view(arr, cfg.window)[:, ::-1]
    out = (win @ w) / w.sum()
    full = np.full(arr.size, np.nan, dtype=float)
    full[cfg.window - 1:] = out
    return full


def endpoint_arrows(
    opens: np.ndarray, closes: np.ndarray, cfg: EnvelopeConfig | None = None,
) -> list[dict]:
    """``plotshape`` في Pine: عبور السعر للشريطين السفلي/العلوي.

    mae = ta.sma(|src - out|, 499) * mult؛ upper/lower = out ± mae.
    ta.crossunder(close, lower) ⇒ ▲؛ ta.crossover(close, upper) ⇒ ▼.
    """
    cfg = cfg or EnvelopeConfig()
    c = np.asarray(closes, dtype=float).ravel()
    o = np.asarray(opens, dtype=float).ravel()
    out = endpoint_estimator(c, cfg)
    residual = np.abs(c - out)
    mae = np.full(c.size, np.nan, dtype=float)
    if c.size >= cfg.window + 498:
        win = sliding_window_view(residual, 499)
        mae[498:] = win.mean(axis=1) * cfg.mult
    lower = out - mae
    upper = out + mae
    res: list[dict] = []
    for t in range(1, c.size):
        if not (np.isfinite(lower[t]) and np.isfinite(lower[t - 1])):
            continue
        if c[t] < lower[t] and c[t - 1] >= lower[t - 1]:
            res.append({"index": t, "kind": "buy", "price": float(c[t]), "lower": float(lower[t])})
        if c[t] > upper[t] and c[t - 1] <= upper[t - 1]:
            res.append({"index": t, "kind": "sell", "price": float(c[t]), "upper": float(upper[t])})
    return res
