"""قرار إشارات نظام NW 1H — منطق صرف بلا شبكة ولا حالة.

شروط المستخدم (2026-11-06) بالحرف:
  1) تظهر العلامة الخضراء ▲ (قاع منحنى Nadaraya-Watson) على الشمعة B.
  2) «الشمعة التي تلت الشمعة التي ظهرت منها الإشارة» يجب أن تكون **خضراء**
     (الإغلاق > الافتتاح) — عندها فقط تُرسل التوصية.
  3) أحدث قطعة في المنحنى يجب أن تكون خضراء (`color_newest == "up"`) لحظة الإرسال.
  4) العلامة الحمراء ▼ (قمة المنحنى) = إشارة الخروج، بلا أهداف ولا وقف.
  5) تكرار العلامة الخضراء ⇒ رسالة «إشارة دخول ثانية» (يُطبَّق في nw_bot.py).

حماية سببية: لا تُعتمد إلا التسميات الواقعة على الشمعة السابقة مباشرةً
(``max_age_bars = 1``). التسميات الأقدم قد تكون ظهرت بأثر رجعي بسبب إعادة
الرسم، فتُسجَّل في ``ignored_retro`` ولا يُرسل منها شيء. هذه هي الحماية التي
تجعل الشرط (2) ممكنًا أصلًا: شمعة التأكيد تكون هي الشمعة الساعية المغلقة للتو.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.nw_strategy import annotate_repaint_signals, nw_line_colors, nw_curve


@dataclass(frozen=True, slots=True)
class NWSignalConfig:
    bandwidth: float = 8.0        # h في مؤشر LuxAlgo
    window: int = 500             # نافذة الـ500 شمعة في الـPine
    max_age_bars: int = 1         # لا إشارات بأثر رجعي
    require_newest_green: bool = True
    require_confirm_green: bool = True       # شمعة التأكيد الخضراء (شرط المستخدم)
    require_exit_confirm_green: bool = False  # الخروج بظهور ▼ فقط

    def __post_init__(self) -> None:
        if self.window < 10:
            raise ValueError("window must be >= 10")
        if self.max_age_bars < 1:
            raise ValueError("max_age_bars must be >= 1")


def evaluate_symbol(
    opens: np.ndarray | list[float],
    closes: np.ndarray | list[float],
    open_times_ms: np.ndarray | list[int],
    cfg: NWSignalConfig | None = None,
) -> dict:
    """يُقيّم زوجًا واحدًا على **شموع ساعية مغلقة فقط** (من الأقدم للأحدث).

    يُعيد: buy/sell (سجلات جاهزة للرسائل)، newest_color، ignored_retro،
    last_bar_time، وأي سبب عدم جاهزية. لا يلمس الشبكة ولا الحالة.
    """
    cfg = cfg or NWSignalConfig()
    o = np.asarray(opens, dtype=float).ravel()
    c = np.asarray(closes, dtype=float).ravel()
    t = np.asarray(open_times_ms, dtype=np.int64).ravel()
    if not (o.size == c.size == t.size):
        raise ValueError("opens/closes/open_times_ms must have the same length")
    base = {
        "ok": False,
        "buy": [],
        "sell": [],
        "ignored_retro": 0,
        "newest_color": None,
        "last_bar_time": int(t[-1]) if t.size else 0,
        "bars": int(c.size),
    }
    if c.size < cfg.window:
        base["reason"] = "not_enough_bars"
        return base

    y = nw_curve(c, h=cfg.bandwidth, window=cfg.window)
    labels = annotate_repaint_signals(y, c)
    colors = nw_line_colors(y)
    base["ok"] = True
    base["newest_color"] = colors[1] if len(colors) > 1 else None
    base["labels_total"] = len(labels)

    for s in labels:
        i = int(s["bars_ago"])
        if i < 1 or i > cfg.max_age_bars:
            base["ignored_retro"] += 1
            continue
        idx = int(s["index"])
        confirm_idx = idx + 1
        if confirm_idx >= c.size:
            continue                       # شمعة التأكيد لم تُغلق بعد
        confirm_green = bool(c[confirm_idx] > o[confirm_idx])
        rec = {
            "kind": s["kind"],                  # buy = ▲ | sell = ▼
            "bars_ago": i,
            "bar_time": int(t[idx]),            # زمن شمعة الإشارة
            "confirm_bar_time": int(t[confirm_idx]),
            "signal_price": float(c[idx]),
            "confirm_open": float(o[confirm_idx]),
            "confirm_close": float(c[confirm_idx]),
            "confirm_green": confirm_green,
            "confirm_pct": float((c[confirm_idx] / o[confirm_idx] - 1.0) * 100.0),
            "nw": float(s["nw"]),
            "color_at_signal": s["color_at_signal"],
            "color_newest": s["color_newest"],
            "newer_all_up": s["newer_all_up"],
        }
        if s["kind"] == "buy":
            if cfg.require_confirm_green and not confirm_green:
                continue
            if cfg.require_newest_green and s["color_newest"] != "up":
                continue
            base["buy"].append(rec)
        else:
            if cfg.require_exit_confirm_green and not confirm_green:
                continue
            base["sell"].append(rec)
    return base


def confirm_green(opens: float, closes: float) -> bool:
    """«الشمعة خضراء» بالمعنى المستخدم في TradingView: الإغلاق > الافتتاح."""
    return float(closes) > float(opens)


def candles_closed(df, now_ms: int):
    """يُعيد (opens, closes, open_times_ms) للشموع المغلقة فقط.

    Binance: close_time هو آخر ميلي ثانية في الشمعة ⇒ الإغلاق الفعلي عند
    close_time + 1. نستبعد أي شمعة لم ينتهِ وقتها بعد.
    """
    if df is None or getattr(df, "empty", True):
        return np.array([]), np.array([]), np.array([])
    closed = df[df["close_time"].astype("int64") < int(now_ms)]
    if closed.empty:
        return np.array([]), np.array([]), np.array([])
    return (
        closed["open"].to_numpy(dtype=float),
        closed["close"].to_numpy(dtype=float),
        closed["open_time"].to_numpy(dtype=np.int64),
    )
