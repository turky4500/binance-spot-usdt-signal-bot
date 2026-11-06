"""قرار إشارات نظام NW 1H — منطق صرف بلا شبكة ولا حالة.

شروط المستخدم (2026-11-06) بالحرف — **بعد تعديل 2026-11-06 (إلغاء شرط الخط)**:
  1) تظهر العلامة الخضراء ▲ (قاع منحنى Nadaraya-Watson) على الشمعة B.
  2) «الشمعة التي تلت الشمعة التي ظهرت منها الإشارة» يجب أن تكون **خضراء**
     (الإغلاق > الافتتاح) — عند تحقق الشرطين تُرسل التوصية فورًا.
  3) ~~الخط أخضر~~ **مُلغي بأمر المستخدم**: لون خط المؤشر لم يعد شرطًا ولا يُذكر في الرسالة.
     السبب الرياضي: عند أي قاع ▲ تكون ``colors[1]`` خضراء لكن ``colors[2]``
     (الخط الظاهر عند الإشارة) **حمراء بالتعريف**؛ فالشرط كان يفحص قطعة غير المقصودة.
     أُبقي المتغيّر ``require_newest_green`` كخيار اختياري (افتراضيًا ``False``).
  4) العلامة الحمراء ▼ (قمة المنحنى) = إشارة الخروج، بلا أهداف ولا وقف.
  5) تكرار العلامة الخضراء ⇒ رسالة «إشارة دخول ثانية» (يُطبَّق في nw_bot.py).

حماية سببية: لا تُعتمد إلا التسميات الواقعة على الشمعة السابقة مباشرةً
(``max_age_bars = 1``). التسميات الأقدم قد تكون ظهرت بأثر رجعي بسبب إعادة
الرسم، فتُسجَّل في ``ignored_retro`` ولا يُرسل منها شيء. هذه هي الحماية التي
تجعل الشرط (2) ممكنًا أصلًا: شمعة التأكيد تكون هي الشمعة الساعية المغلقة للتو.

**توضيح المستخدم 2026-11-06 (TSTUSDT):** «إذا أُغلقت الشمعة وظهرت عليها إشارة
دخول، انتظر الشمعة التالية؛ إن أُغلقت خضراء أرسل التوصية». أي أن الشمعة التالية
الحمراء **تُلغي تلك الإشارة نهائيًا** ويجب انتظار ▲ جديدة. لذلك تُعاد التسميات
المرفوضة في ``cancelled_buy`` ليُجمّد ``nw_bot.py`` قرارها في الحالة، فلا تُبعث
مرة أخرى إن أعاد المؤشر رسمها.

كذلك ``boundary_correction`` يمنع القيعان الوهمية عند طرف النافذة: نواة Pine
أحادية الجانب عند i=0 كانت تُنتج ▲ زائفة على ``bars_ago=1`` لكل رمز تقريبًا
(عمق القاع ~0.001% من السعر)، وهو بالضبط ما ولّد توصيات 09:00/10:00 الخاطئة.
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
    require_newest_green: bool = False   # مُلغي بأمر المستخدم — لون الخط ليس شرطًا
    require_confirm_green: bool = True       # شمعة التأكيد الخضراء (شرط المستخدم)
    require_exit_confirm_green: bool = False  # الخروج بظهور ▼ فقط
    # منع ▲ الوهمية عند طرف نافذة الـ500 (تحيّز النواة الأحادية الجانب).
    # الافتراضي False للمطابقة الحرفية مع Pine؛ النظام الحيّ يفعّله بـ NW_BOUNDARY_CORRECT=1.
    boundary_correction: bool = False
    # عتبة ضجيج: أقل عمق مقبول للقاع/القمة كنسبة مئوية من السعر. القيعان الوهمية
    # عند الحافة ~0.00003–0.00005%، والقيعان الحقيقية ~0.0002–0.0100%.
    # الافتراضي 0 (معطّل) للمطابقة الحرفية؛ الحيّ يضبطه بـ NW_MIN_TROUGH_PCT=0.0001.
    min_trough_pct: float = 0.0

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
        "cancelled_buy": [],   # ▲ أُغلقت شمعتها التالية حمراء ⇒ تُلغى نهائيًا
        "ignored_retro": 0,
        "ignored_shallow": 0,  # قيعان/قمم تحت عتبة الضجيج (لا تظهر على الشارت)
        "newest_color": None,
        "last_bar_time": int(t[-1]) if t.size else 0,
        "bars": int(c.size),
    }
    if c.size < cfg.window:
        base["reason"] = "not_enough_bars"
        return base

    y = nw_curve(c, h=cfg.bandwidth, window=cfg.window,
                 boundary_correction=cfg.boundary_correction)
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
        if cfg.min_trough_pct > 0 and 0 < i < y.size - 1:
            left, mid, right = float(y[i - 1]), float(y[i]), float(y[i + 1])
            depth = (min(left, right) - mid) if s["kind"] == "buy" else (mid - max(left, right))
            if depth / float(c[idx]) * 100.0 < cfg.min_trough_pct:
                base["ignored_shallow"] += 1
                continue                   # ارتجاف رقمي، لا ▲/▼ مرئية
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
                # قاعدة المستخدم: إن أُغلقت الشمعة التالية حمراء تُلغى الإشارة
                # نهائيًا وننتظر ▲ جديدة. نُبلّغ الحالة لتجميد القرار، حتى لو
                # أعاد المؤشر رسم التسمية لاحقًا.
                base["cancelled_buy"].append({
                    "bar_time": int(t[idx]),
                    "confirm_bar_time": int(t[confirm_idx]),
                })
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
