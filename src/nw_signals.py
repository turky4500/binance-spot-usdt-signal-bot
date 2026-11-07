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

from src.nw_strategy import (
    annotate_repaint_signals,
    endpoint_estimator,
    endpoint_flip_signals,
    nw_line_colors,
    nw_curve,
)
from src.nw_envelope import EnvelopeConfig, envelope_labels


@dataclass(frozen=True, slots=True)
class NWSignalConfig:
    bandwidth: float = 8.0        # h في مؤشر LuxAlgo
    window: int = 500             # نافذة الـ500 شمعة في الـPine
    # المصدر الحيّ الوحيد للتوصيات: Nadaraya-Watson Envelope [LuxAlgo] الذي
    # أرسله المستخدم (2026-11-06) — ``mode="envelope"`` يقرأ تسميات ▲/▼ كما
    # يطبعها الكود على الشارت بالحرف. ``build_nw_bot()`` والوركفلو يضبطانه
    # صراحةً؛ الافتراضي هنا يبقى ``repaint`` لحفاظ اختبارات النقل البحثي القديمة.
    mode: str = "repaint"          # "envelope" (الحيّ) | "endpoint" | "repaint"
    mult: float = 3.0             # input.float(3., ...) في الكود المرسل
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
        if self.mode not in {"envelope", "endpoint", "repaint"}:
            raise ValueError("mode must be 'envelope', 'endpoint' or 'repaint'")
        if self.mult < 0:
            raise ValueError("mult must be >= 0")


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
    if cfg.mode == "envelope":
        return _evaluate_envelope(o, c, t, cfg)
    if cfg.mode == "endpoint":
        return _evaluate_endpoint(o, c, t, cfg)
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


def _evaluate_envelope(o: np.ndarray, c: np.ndarray, t: np.ndarray,
                       cfg: "NWSignalConfig") -> dict:
    """تسميات ▲/▼ من «Nadaraya-Watson Envelope [LuxAlgo]» كما أرسلها المستخدم.

    لا يوجد هنا أي مقدّر أو فلتر أو عتبة من عندنا: نستدعي ``envelope_labels``
    (النقل الحرفي لـ ``label.new`` في Pine) ثم نُطبّق **شرط المستخدم فقط**:

        ظهرت ▲ على شمعة مغلقة B ⇒ الشمعة التالية B+1 خضراء ⇒ تُرسل التوصية
        (بسعر إغلاق B+1 و🕒 وقتها)؛ حمراء ⇒ تُلغى نهائيًا؛ ▼ ⇒ خروج.

    **صدفة البرودة (repaint):** الكود المرسل يحسب التسميات من نافذة 500 شمعة
    عند كل لحظة؛ ``src[0]`` = أحدث شمعة مغلقة. كل دورة جديدة تُعدّل المنحنى
    ``nwe`` والمقام ``sae`` ويُمكن أن تلغي التسمية القديمة (إعادة الرسم).
    نتحقق فورًا أن التسمية على ``bars_ago == 1`` **تظل محسوبة حتى بعد إضافة
    شمعة جديدة** في محاكاة الدورة التالية (ندخل الشمعة الجديدة في مقدمة النافذة).
    إن لم تكن باقية في الحساب المنقح → لا تُرسل (المستخدم لن يرى الأسهم الأخضر، فالتوصية عبثية).

    التوقيت من صيغة الشرط نفسها، لا من فلتر مضاف: «انتظر الشمعة التالية،
    إذا أغلقت خضراء أرسل» ⇒ القرار يحين **مرة واحدة فقط**، عند إغلاق تلك
    الشمعة. في ترتيب الشموع المغلقة يكون ``src[0]`` آخر شمعة مغلقة، فالتسمية
    الوحيدة التي أغلقت شمعتها التالية للتو هي ``bars_ago == 1``. أي تسمية أقدم
    أُغلقت شمعة تأكيدها في دورة سابقة ⇒ لا تُرسل اليوم (هذا هو خطأ WLDUSDT
    الذي أبلغ عنه المستخدم: آخر ▲ لها 30 أكتوبر وأُرسلت توصية في 6 نوفمبر).
    ``bars_ago == 0`` شمعتها التالية لم تُغلق بعد ⇒ تُنتظر. لا مقارنة لقطات،
    ولا حدّ عمر، ولا عتبة ضجيج، ولا تصحيح حافة: التسميات من ``label.new`` كما هي.
    """
    base = {
        "ok": False, "buy": [], "sell": [], "cancelled_buy": [],
        "ignored_retro": 0, "ignored_shallow": 0, "newest_color": None,
        "last_bar_time": int(t[-1]) if t.size else 0, "bars": int(c.size),
        "mode": "envelope",
    }
    if c.size < cfg.window + 2:
        base["reason"] = "not_enough_bars"
        return base

    ecfg = EnvelopeConfig(bandwidth=cfg.bandwidth, mult=cfg.mult,
                          window=cfg.window, repaint=True)
    src = c[::-1]                       # src[0] = أحدث شمعة مغلقة
    sae, labels = envelope_labels(src, ecfg)
    n = int(c.size)
    # التسمية التي يحين قرارها في هذه الدورة فقط: على الشمعة قبل الأخيرة،
    # وشمعة التأكيد هي آخر شمعة مغلقة. لا تسميات تاريخية تُقيَّم من جديد.
    due = [(n - 1 - int(L["bars_ago"]), L) for L in labels
           if int(L["bars_ago"]) == 1]
    base["ok"] = True
    base["sae"] = sae
    base["labels_total"] = len(labels)
    base["labels_due"] = len(due)

    # صدفة البرودة: محاكاة دورة تالية بإدخال شمعة جديدة في مقدمة النافذة (src[0])
    # مع خروج أقدم شمعة منها. إن التسمية لم تعد موجودة في النسخة المحاكاة ⇒
    # ستنحذف من الشارت لاحقًا (إعادة رسم) ⇒ لا تُرسل التوصية الآن.
    shifted = np.concatenate([np.asarray([src[0]], dtype=float), src])
    _, refreshed_labels = envelope_labels(shifted, ecfg)
    refreshed_keys = {(str(L["kind"]), float(L["price"])) for L in refreshed_labels}

    for idx, L in due:
        confirm_idx = idx + 1
        if confirm_idx >= n:            # شمعة التأكيد لم تُغلق بعد
            continue
        kind = str(L["kind"])
        confirm_green_ = bool(c[confirm_idx] > o[confirm_idx])
        rec = {
            "kind": kind,
            "bars_ago": int(L["bars_ago"]),
            "bar_time": int(t[idx]),
            "confirm_bar_time": int(t[confirm_idx]),
            "signal_price": float(c[idx]),
            "confirm_open": float(o[confirm_idx]),
            "confirm_close": float(c[confirm_idx]),
            "confirm_green": confirm_green_,
            "confirm_pct": float((c[confirm_idx] / o[confirm_idx] - 1.0) * 100.0),
            "nw": float(L["nwe"]),
            "band": float(L["band"]),
            "sae": float(sae),
            "color_at_signal": "up" if kind == "buy" else "down",
            "color_newest": None,
            "newer_all_up": False,
        }
        if kind == "buy":
            if (kind, float(L["price"])) not in refreshed_keys:
                continue   # ▲/▼ اختفت ⇒ ستنحذف من الشارت (إعادة رسم) ⇒ لا تُرسل
            if cfg.require_confirm_green and not confirm_green_:
                base["cancelled_buy"].append({
                    "bar_time": int(t[idx]),
                    "confirm_bar_time": int(t[confirm_idx]),
                })
                continue
            # لون الخط ليس شرطًا بأمر المستخدم، ولا وجود لون خط في Envelope.
            base["buy"].append(rec)
        else:
            if (kind, float(L["price"])) not in refreshed_keys:
                continue   # ▼ اختفت ⇒ ستنحذف من الشارت (إعادة رسم) ⇒ لا تُرسل
            if cfg.require_exit_confirm_green and not confirm_green_:
                continue
            base["sell"].append(rec)
    return base


def _evaluate_endpoint(o: np.ndarray, c: np.ndarray, t: np.ndarray,
                       cfg: "NWSignalConfig") -> dict:
    """``repaint = false`` من LuxAlgo — الخط السببي (endpoint estimator).

    توثيق LuxAlgo الرسمي: عند تعطيل «Repainting Smoothing» يُرسم خط سببي
    يشبه المتوسط المتحرك، ولا يُرسم أي مثلث ▲/▼. نُطبّق هنا **نفس قاعدة
    المثلثات** في Pine (``d * y1_d < 0``، ▲ عند انقلاب الميل من هابط إلى صاعد)
    على هذا الخط السببي:

      d[B] = out[B] - out[B-1] < 0  و  d[B+1] = out[B+1] - out[B] > 0
      ⇒ ▲ على الشمعة B، وتُعرف لحظة إغلاق الشمعة B+1.

    أي أن «الشمعة التالية» في شرط المستخدم هي بالضبط الشمعة التي تُثبت القاع،
    فيمكن فحص لونها فور إغلاقها بلا أي بيانات مستقبلية. ``out[0..B+1]`` لا
    يتغير أبدًا بإضافة شموع لاحقة ⇒ التسمية لا تتحرك ولا تُعاد رسمها، ولا توجد
    قيعان طرفية وهمية (لا نافذة ثنائية الجانب)، فلا حاجة لتصحيح الحافة.
    """
    base = {
        "ok": False, "buy": [], "sell": [], "cancelled_buy": [],
        "ignored_retro": 0, "ignored_shallow": 0, "newest_color": None,
        "last_bar_time": int(t[-1]) if t.size else 0, "bars": int(c.size),
        "mode": "endpoint",
    }
    # أول قيمة صالحة للخط تظهر عند الشمعة رقم `window`، ويحتاج انقلاب الميل
    # ثلاث قيم متتالية على الأقل. بـ501 شمعة مغلقة لا توجد سوى قيمة أو قيمتين
    # ⇒ صفر انعطافات «بصمت». نُعلن عدم الجاهزية بدل أن يبدو «لا توجد إشارة».
    if c.size < cfg.window + 2:
        base["reason"] = "not_enough_bars"
        return base

    out = endpoint_estimator(c, h=cfg.bandwidth, window=cfg.window)
    flips = endpoint_flip_signals(out, c)
    base["ok"] = True
    base["newest_color"] = "up" if out[-1] > out[-2] else "down"
    base["labels_total"] = len(flips)

    for s in flips:
        # سُمح بها فقط إن ظهرت على إحدى آخر max_age_bars شمعة مغلقة. في الوضع
        # السببي لا وجود لـ«إشارة بأثر رجعي»؛ الأقدم مجرد تاريخ لا يُعاد تقييمه.
        age = int(c.size) - 1 - int(s["index"])
        if age < 0 or age >= cfg.max_age_bars:
            continue
        idx = int(s["extremum_index"])       # شمعة ▲ / ▼ = B
        confirm_idx = idx + 1                # الشمعة التالية = B+1
        if idx < 1 or confirm_idx >= c.size:
            continue
        kind = str(s["kind"])
        left, mid, right = float(out[idx - 1]), float(out[idx]), float(out[idx + 1])
        depth = (min(left, right) - mid) if kind == "buy" else (mid - max(left, right))
        depth_pct = depth / float(c[idx]) * 100.0
        if cfg.min_trough_pct > 0 and depth_pct < cfg.min_trough_pct:
            base["ignored_shallow"] += 1
            continue
        confirm_green_ = bool(c[confirm_idx] > o[confirm_idx])
        rec = {
            "kind": kind,
            "bars_ago": 1,                       # ▲ على الشمعة السابقة مباشرةً
            "bar_time": int(t[idx]),
            "confirm_bar_time": int(t[confirm_idx]),
            "signal_price": float(c[idx]),
            "confirm_open": float(o[confirm_idx]),
            "confirm_close": float(c[confirm_idx]),
            "confirm_green": confirm_green_,
            "confirm_pct": float((c[confirm_idx] / o[confirm_idx] - 1.0) * 100.0),
            "nw": float(s["nw"]),
            "depth_pct": float(depth_pct),
            "color_at_signal": "up" if right > mid else "down",
            "color_newest": base["newest_color"],
            "newer_all_up": False,
        }
        if kind == "buy":
            if cfg.require_confirm_green and not confirm_green_:
                base["cancelled_buy"].append({
                    "bar_time": int(t[idx]),
                    "confirm_bar_time": int(t[confirm_idx]),
                })
                continue
            if cfg.require_newest_green and rec["color_newest"] != "up":
                continue
            base["buy"].append(rec)
        else:
            if cfg.require_exit_confirm_green and not confirm_green_:
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
