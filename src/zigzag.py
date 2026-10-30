"""نظام ZigZag++ (فريم الساعة) — تحويل منطق القيعان إلى pandas.

المرجع: مؤشر ZigZag++ [LD] للمطور DevLucem، المبني على MT4 ZigZag
(`Depth=12`, `Deviation=5`, `Backstep=2`). مكتبة ZigLib غير منشورة،
لكن النسخة v4 من المؤشر تحتوي الخوارزمية كاملة، وهي المعتمدة هنا.

تعريف القاع المعتمد (باختيار المستخدم):
    القاع = شمعة 1H مغلقة يكون أدنى سعرها **أدنى من كل** الشموع الـ11 السابقة
    (أي أدنى قاع في آخر Depth=12 شمعة). يُعرف القاع عند إغلاق شمعته نفسها،
    ثم ننتظر الشمعة المغلقة التالية مباشرة.

قواعد الإشارة:
    - الشمعة التالية للقاع خضراء (إغلاق > افتتاح) وغير دوجي ⇒ دخول بإغلاقها.
    - حمراء أو دوجي (جسم ≤ 0.05% من السعر) ⇒ يُلغى القاع وننتظر قاعًا آخر.
    - الهدف: +2% من سعر الدخول — يُحتسب بمجرد **لمس** السعر للهدف (أعلى سعر).
    - الخسارة: **إغلاق** شمعة ساعة تحت أدنى سعر لشمعة القاع (ليس اللمس).
    - عند التعارض داخل الشمعة نفسها: لمس الهدف يسبق إغلاق الخسارة.

⚠️ العزل: هذا الملف مستقل تمامًا. لا يقرأ ولا يكتب أي ملف يخص
   Target Trend أو نظام القاع أو السوق السعودي.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

HOUR_MS = 3_600_000
MINUTE_MS = 60_000
STRATEGY_LABEL = "ZigZag++ (ساعة)"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class ZigZagSettings:
    """إعدادات النظام — كلها قابلة للتعديل من متغيرات البيئة ZZ_*."""

    depth: int = _env_int("ZZ_DEPTH", 12)                       # نافذة أدنى قاع (شموع 1H)
    target_pct: float = _env_float("ZZ_TARGET_PCT", 2.0)        # نسبة الهدف من سعر الدخول %
    # هدف بمضاعف المخاطرة: target = entry + R × (entry − stop). 0 = تعطيل (يُستخدم target_pct).
    # مناسب عندما يكون القاع بعيدًا (وقف مكلف) كما في محاور ZigZag++ [LD].
    target_r: float = _env_float("ZZ_TARGET_R", 0.0)
    doji_body_pct: float = _env_float("ZZ_DOJI_BODY_PCT", 0.05)  # حد الدوجي: جسم ≤ هذه النسبة
    lookback_bars: int = _env_int("ZZ_LOOKBACK_BARS", 6)        # أقصى عمر للقاع عند البحث
    max_entry_age_hours: float = _env_float("ZZ_MAX_ENTRY_AGE_HOURS", 2.0)  # رفض الإشارات المتأخرة
    commission_side_pct: float = _env_float("ZZ_COMMISSION_SIDE_PCT", 0.1)  # العمولة لكل جهة %
    # فلتر اختياري: أدنى مسافة بين الدخول وقاع الإشارة %.
    # 0 = معطّل (الافتراضي، وهو القرار المعتمد حاليًا). تشغيله من الوركفلو
    # وحده (ZZ_MIN_STOP_PCT=1.5) يقلّل الرسائل ويرفع نسبة الفوز بلا تعديل كود.
    min_stop_pct: float = _env_float("ZZ_MIN_STOP_PCT", 0.0)
    # ── فلتر المتوسط 200 (2026-10-30، طلب المستخدم) ──
    # لا إشارة إلا إذا كان إغلاق شمعة الدخول **فوق** EMA(ema_len).
    # السبب: في الهبوط العام تكون كل الأزواج تحت المتوسط، فتصدر مئات الإشارات
    # المتزامنة (246 إشارة في ثانية واحدة في 2026-10-30) ثم تُوقف كلها في نفس
    # الشمعة. الفلتر يحصر الإشارات في الاتجاه الصاعد فقط.
    # 0 = تعطيل الفلتر. عند نقص البيانات (أقل من ema_len شمعة) تُرفض الإشارة
    # احتياطًا (لا نُصدر إشارة على متوسط غير مكتمل).
    ema_filter: int = _env_int("ZZ_EMA_FILTER", 1)
    ema_len: int = _env_int("ZZ_EMA_LEN", 200)
    # فريم المتوسط: "1h" (الشمعة نفسها) أو "4h" (اتجاه أطول — أفضل بالاختبار: 81% فوز)
    ema_interval: str = os.getenv("ZZ_EMA_INTERVAL", "1h").strip().lower()
    # ── تعريف القاع (2026-10-30) ──
    # "low12"    : أدنى قاع في Depth شمعة (الوضع السابق — افتراضي حتى اعتماد الأرقام)
    # "zigzag_ld": محاور ZigZag++ [LD] الحقيقية (Depth/Deviation/Backstep) — يطابق
    #              المؤشر على TradingView شمعة بشمعة (تحقق JPMBUSDT: HH 346.16، HL 342.76)
    bottom_mode: str = os.getenv("ZZ_BOTTOM_MODE", "low12").strip().lower()
    # أقصى مسافة وقف مقبولة % (0 = معطّل). يهمّ في وضع المحاور لأن قاع ZigZag قد
    # يكون بعيدًا جدًا (خسارة −6.4% مقابل ربح +1.8% في الباك-تست بلا حد).
    max_stop_pct: float = _env_float("ZZ_MAX_STOP_PCT", 0.0)
    # مصدر القاع في وضع المحاور:
    #   live      = الوسم الجاري على الشارت لحظة ظهوره (repaint) ⇒ الدخول على الشمعة التالية
    #   confirmed = المحور المؤكد فقط (بعد Backstep) ⇒ متأخر شمعتين عن الشارت
    pivot_source: str = os.getenv("ZZ_PIVOT_SOURCE", "live").strip().lower()
    # موضع شمعة القاع: extreme = شمعة أدنى سعر فعلي (يظهر الوسم عليها) |
    #                  detect  = شمعة الاكتشاف (حيث يؤكد MT4 المحور، عادةً بعدها بساعة)
    bottom_bar: str = os.getenv("ZZ_BOTTOM_BAR", "extreme").strip().lower()
    deviation: int = _env_int("ZZ_DEVIATION", 5)      # Deviation (بنقاط السعر: × tickSize)
    backstep: int = _env_int("ZZ_BACKSTEP", 2)        # Backstep (شموت تأكيد المحور)

    @property
    def pivot_mode(self) -> bool:
        return self.bottom_mode in ("zigzag_ld", "pivot", "zigzag")

    @property
    def ema_enabled(self) -> bool:
        return self.ema_filter != 0 and self.ema_len > 1

    def describe(self) -> str:
        ema_part = (
            f" • فوق EMA{self.ema_len}({self.ema_interval})" if self.ema_enabled else ""
        )
        bottom_part = "محاور ZigZag++ [LD]" if self.pivot_mode else f"أدنى قاع {self.depth} شمعة"
        return (
            f"{STRATEGY_LABEL}: {bottom_part} • "
            + (f"هدف {self.target_r:g}R (لمس) • " if self.target_r > 0 else f"هدف +{self.target_pct:g}% (لمس) • ")
            + "خسارة بإغلاق تحت القاع • "
            f"دوجي ≤ {self.doji_body_pct:g}% • "
            f"أدنى وقف {self.min_stop_pct:g}%{ema_part}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# اكتشاف القاع
# ─────────────────────────────────────────────────────────────────────────────
def find_latest_bottom(
    closed_df: pd.DataFrame,
    settings: ZigZagSettings,
    point: float | None = None,
) -> dict | None:
    """آخر قاع مغلق: أدنى سعر في آخر `depth` شمعة (بلا نظر للمستقبل).

    نفحص فقط آخر `lookback_bars` شمعة — القيعان الأقدم تُهمَل لأنها لم تعد
    قابلة للتنفيذ. يعيد dict أو None.
    """
    n = len(closed_df)
    if n < settings.depth:
        return None

    if settings.pivot_mode:
        return _latest_pivot_low(closed_df, settings, point)

    lows = closed_df["low"].to_numpy(dtype="float64")
    start = max(settings.depth - 1, n - settings.lookback_bars)
    for i in range(n - 1, start - 1, -1):
        window = lows[i - settings.depth + 1 : i]  # الشموع السابقة فقط
        if window.size == 0:
            continue
        if not np.isfinite(lows[i]) or not np.isfinite(window).all():
            continue
        if lows[i] < float(window.min()):  # أدنى من كل الشموع السابقة في النافذة
            row = closed_df.iloc[i]
            return {
                "bar_index": i,
                "bar_open_time": int(row["open_time"]),
                "bar_close_time": int(row["close_time"]),
                "low": float(row["low"]),
            }
    return None


def _latest_pivot_low(
    closed_df: pd.DataFrame, settings: ZigZagSettings, point: float | None
) -> dict | None:
    """آخر **قاع محوري مؤكد** بحسب ZigZag++ [LD] (Depth/Deviation/Backstep).

    يُرجع نفس بنية القاع القديمة + حقول إضافية:
        pivot_label: "HL" أو "LL" (وسم المؤشر)
        confirm_open_time: وقت فتح الشمعة التي صار عندها المحور مؤكدًا
                           (bar_index + backstep) — البوت لا يدخل قبلها (بلا نظر للمستقبل)
    """
    from src.zigzag_ld import mt4_zigzag

    high = closed_df["high"].to_numpy(dtype="float64")
    low = closed_df["low"].to_numpy(dtype="float64")
    pivots = mt4_zigzag(
        high, low,
        depth=settings.depth,
        deviation=settings.deviation,
        backstep=settings.backstep,
        point=float(point) if point else 0.01,
    )
    if settings.pivot_source == "live":
        # قاعدة المستخدم: «إشارة القاع» هي الوسم (HL/LL) الذي يرسمه المؤشر، والدخول على
        # **الشمعة التالية** إن أغلقت خضراء (وإن كانت حمراء/دوجي تُلغى الإشارة).
        # الحساب على الشموع المغلقة كلها (بلا نظر للمستقبل): المحور يُقرأ من البادئة الحالية،
        # و«شمعة القرار» هي الشمعة التالية لشمعة القاع — ولا تُقيَّم إلا إذا كانت آخر شمعة مغلقة.
        pv = mt4_zigzag(
            high, low,
            depth=settings.depth,
            deviation=settings.deviation,
            backstep=settings.backstep,
            point=float(point) if point else 0.01,
        )
        if not pv or pv[-1]["kind"] != "L":
            return None                       # آخر محور ليس قاعًا ⇒ لا إشارة قاع قائمة
        pivot = pv[-1]
        # شمعة القاع: أدنى سعر فعلي في نافذة العمق (حيث يرسم المؤشر الوسم)
        #           أو شمعة الاكتشاف نفسها (وضع detect)
        bar_idx = int(pivot["bar"])
        if settings.bottom_bar == "extreme":
            lo = max(0, bar_idx - settings.depth + 1)
            seg = low[lo:bar_idx + 1]
            bar_idx = lo + int(seg.argmin())
        decision_idx = bar_idx + 1
        if decision_idx >= len(closed_df):
            return None                       # لم تُغلق شمعة القرار بعد — ننتظر
        prev_lows = [x for x in pv[:-1] if x["kind"] == "L"]
        label = "LL" if (prev_lows and pivot["price"] < prev_lows[-1]["price"]) else "HL"
        row = closed_df.iloc[bar_idx]
        return {
            "bar_index": bar_idx,
            "bar_open_time": int(row["open_time"]),
            "bar_close_time": int(row["close_time"]),
            "low": float(pivot["price"]),
            "pivot_label": label,
            "entry_open_time": int(closed_df.iloc[decision_idx]["open_time"]),
            "confirm_open_time": int(closed_df.iloc[decision_idx - 1]["close_time"]),
        }

    lows = [p for p in pivots if p["kind"] == "L"]
    if not lows:
        return None
    pivot = lows[-1]
    idx = int(pivot["bar"])
    confirm_idx = min(idx + settings.backstep, len(closed_df) - 1)
    previous_low = lows[-2]["price"] if len(lows) > 1 else None
    label = "LL" if (previous_low is not None and pivot["price"] < previous_low) else "HL"
    row = closed_df.iloc[idx]
    return {
        "bar_index": idx,
        "bar_open_time": int(row["open_time"]),
        "bar_close_time": int(row["close_time"]),
        "low": float(pivot["price"]),
        "pivot_label": label,
        "confirm_open_time": int(closed_df.iloc[confirm_idx]["open_time"]),
    }


def next_closed_bar(closed_df: pd.DataFrame, bottom_open_time: int) -> pd.Series | None:
    """الشمعة المغلقة التالية مباشرة لشمعة القاع (بفارق ساعة واحدة بالضبط)."""
    expected = int(bottom_open_time) + HOUR_MS
    match = closed_df[closed_df["open_time"] == expected]
    if match.empty:
        return None
    return match.iloc[-1]


def _note_rejection(rejected: dict | None, reason: str) -> None:
    """يسجّل سبب رفض الإشارة (تعداد في التقرير اليومي) — لا يغيّر أي منطق."""
    if isinstance(rejected, dict):
        rejected["reason"] = reason


def ema_series(closes: pd.Series, length: int) -> pd.Series:
    """EMA مطابق لـ ta.ema في Pine: ewm(span=length, adjust=False)."""
    return closes.astype("float64").ewm(span=int(length), adjust=False).mean()


def ema_at_bar_from_candles(
    candles: pd.DataFrame | None, length: int, as_of_close_ms: int
) -> float | None:
    """EMA من شموع فريم آخر (مثل 4h)، باستخدام **الشموع المغلقة فقط** قبل لحظة القرار.

    بلا نظر للمستقبل: تُهمَل أي شمعة لم تُغلق بعد عند `as_of_close_ms`.
    """
    if candles is None or candles.empty:
        return None
    closed = candles[candles["close_time"].astype("int64") <= int(as_of_close_ms)]
    if len(closed) < max(20, length // 4):
        return None
    return float(ema_series(closed["close"].astype(float), length).iloc[-1])


def ema_at_bar(closed_df: pd.DataFrame | None, length: int, bar_open_time: int) -> float | None:
    """قيمة EMA عند شمعة محددة، محسوبة من الإغلاقات حتى تلك الشمعة فقط.

    بلا نظر للمستقبل: لا تُستخدم أي شمعة بعد شمعة الدخول (نفس انضباط القاع).
    تُرجع None إذا لم توجد الشمعة أو كانت الشموع المتاحة أقل من `length`
    (متوسط غير مكتمل ⇒ لا تُبنى عليه إشارة).
    """
    if closed_df is None or closed_df.empty or int(length) <= 1:
        return None
    times = closed_df["open_time"].to_numpy()
    matches = np.nonzero(times == int(bar_open_time))[0]
    if matches.size == 0:
        return None
    pos = int(matches[-1])
    if pos + 1 < int(length):
        return None
    value = float(ema_series(closed_df["close"].iloc[: pos + 1], int(length)).iloc[-1])
    return value if np.isfinite(value) else None


def evaluate_entry_candle(
    bottom: dict,
    next_row: pd.Series,
    settings: ZigZagSettings,
    ema_value: float | None = None,
    rejected: dict | None = None,
) -> dict | None:
    """تقييم الشمعة التالية للقاع: خضراء وغير دوجي ⇒ إشارة دخول.

    الحماية الإضافية: إغلاق الشمعة يجب أن يكون فوق قاع الإشارة، وإلا فالصفقة
    خاسرة منذ لحظة الدخول (فجوة سفلية) — تُتجاهل.
    """
    open_price = float(next_row["open"])
    close_price = float(next_row["close"])
    if not np.isfinite(open_price) or not np.isfinite(close_price) or open_price <= 0:
        return None

    body_pct = abs(close_price - open_price) / open_price * 100.0
    if close_price <= open_price:                      # حمراء
        _note_rejection(rejected, "red")
        return None
    if body_pct <= settings.doji_body_pct:             # دوجي
        _note_rejection(rejected, "doji")
        return None
    if close_price <= float(bottom["low"]):            # إغلاق تحت القاع
        _note_rejection(rejected, "below_bottom")
        return None

    # ── فلتر المتوسط 200: تحت المتوسط ⇒ لا إشارة ──
    if settings.ema_enabled:
        if ema_value is None or not np.isfinite(ema_value):
            _note_rejection(rejected, "ema_unknown")
            return None
        if close_price <= float(ema_value):
            _note_rejection(rejected, "below_ema")
            return None

    entry_price = close_price
    stop_price = float(bottom["low"])
    stop_dist_pct = (entry_price - stop_price) / entry_price * 100.0
    if settings.min_stop_pct > 0 and stop_dist_pct < settings.min_stop_pct:
        _note_rejection(rejected, "stop_too_narrow")   # قاع قريب جدًا — فلتر اختياري
        return None
    if settings.max_stop_pct > 0 and stop_dist_pct > settings.max_stop_pct:
        _note_rejection(rejected, "stop_too_wide")      # قاع بعيد جدًا (وقف مكلف)
        return None
    if settings.target_r > 0:
        target_price = entry_price + settings.target_r * (entry_price - stop_price)
    else:
        target_price = entry_price * (1.0 + settings.target_pct / 100.0)

    return {
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "bottom_low": stop_price,
        "bottom_bar_open_time": int(bottom["bar_open_time"]),
        "entry_bar_open_time": int(next_row["open_time"]),
        "entry_time": int(next_row["close_time"]),
        "doji_body_pct": body_pct,
        "ema_value": float(ema_value) if (settings.ema_enabled and ema_value is not None) else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# متابعة الهدف والخسارة
# ─────────────────────────────────────────────────────────────────────────────
def evaluate_path_1m(
    klines_1m: pd.DataFrame | None,
    target_price: float,
    stop_price: float,
) -> dict | None:
    """يمشي على شموع الدقيقة بالترتيب الزمني: لمس الهدف أولًا، ثم إغلاق الساعة.

    - الهدف: أي شمعة دقيقة لامس أعلاها الهدف ⇒ نجاح فوري (بلا انتظار إغلاق).
    - الخسارة: إغلاق **شمعة الساعة** تحت قاع الإشارة ⇒ خسارة عند ذلك الإغلاق.
    - داخل الشمعة نفسها: الهدف يسبق الخسارة (قرار المستخدم).
    """
    if klines_1m is None or klines_1m.empty:
        return None

    max_high = float("-inf")
    for row in klines_1m.itertuples(index=False):
        high = float(row.high)
        close = float(row.close)
        open_price = float(row.open)
        close_time = int(row.close_time)
        if np.isfinite(high) and high > max_high:
            max_high = high

        if high >= target_price:
            fill = max(float(target_price), open_price) if np.isfinite(open_price) else float(target_price)
            return {
                "outcome": "win",
                "reason": "target",
                "fill_price": fill,
                "event_time": close_time,
                "max_price": max(max_high, high),
            }

        # آخر دقيقة في الساعة: (close_time + 1) يقبل القسمة على 3,600,000
        if (close_time + 1) % HOUR_MS == 0 and close < stop_price:
            return {
                "outcome": "loss",
                "reason": "close_below_bottom",
                "fill_price": close,
                "event_time": close_time,
                "max_price": max_high,
            }

    return None


def evaluate_path_1h(
    closed_df: pd.DataFrame | None,
    entry_bar_open_time: int,
    target_price: float,
    stop_price: float,
) -> dict | None:
    """مسار احتياطي عند تعذّر جلب شموع الدقيقة — نفس القواعد على شموع الساعة."""
    if closed_df is None or closed_df.empty:
        return None

    rows = closed_df[closed_df["open_time"] > int(entry_bar_open_time)]
    max_high = float("-inf")
    for row in rows.itertuples(index=False):
        high = float(row.high)
        close = float(row.close)
        open_price = float(row.open)
        if np.isfinite(high) and high > max_high:
            max_high = high
        if high >= target_price:
            fill = max(float(target_price), open_price) if np.isfinite(open_price) else float(target_price)
            return {
                "outcome": "win",
                "reason": "target",
                "fill_price": fill,
                "event_time": int(row.close_time),
                "max_price": max(max_high, high),
            }
        if close < stop_price:
            return {
                "outcome": "loss",
                "reason": "close_below_bottom",
                "fill_price": close,
                "event_time": int(row.close_time),
                "max_price": max_high,
            }
    return None
