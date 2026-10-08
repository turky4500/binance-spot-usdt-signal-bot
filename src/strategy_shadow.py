"""الوضع الظلّي لنظام «ارتداد الاتجاه اليومي» — تسجيل بلا رسائل، لقياس الجودة الحقيقية.

النظام المرشح (من مختبر الكمي، `reports/quant_lab_results.md`):
    1) اتجاه يومي صاعد: إغلاق الأمس > متوسط 50 يومًا (يوم مكتمل، بلا نظر للمستقبل)
    2) أول إغلاق ساعي تحت متوسط 20 ساعة
    3) ساعة الدخول ضمن [0,2,5,6,14,21,23] بتوقيت الرياض
    4) الخروج: هدف +2% • وقف 1.5×ATR(14) مقيّد 1.2–2.5% • انتهاء مهلة 7 أيام

القياس السابق: +0.057%/صفقة (t=+2.52)، النصف الثاني +0.126% (t=+3.95) — **غير مُثبت**،
لذلك يُسجَّل هنا ظلّيًا بلا أي رسالة تيليجرام، ويُقاس على بيانات حيّة جديدة.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

from src.strategy import StrategySettings, ema, prepare_strategy_frame, rsi

SHADOW_STRATEGY_FILE = "strategy_shadow.csv"
GOOD_HOURS = {0, 2, 5, 6, 14, 21, 23}
# مسار مركّز: الساعات التي ظلّت موجبة في **نصفي** العينة (0 و5 و6)
# يُسجَّل كوسم على كل إشارة لقياس المسارين معًا على البيانات الحيّة دون أي انتقاء لاحق
FOCUS_HOURS = {0, 5, 6}
TARGET_PCT = 2.0
STOP_ATR_MULT = 1.5
MIN_STOP_PCT = 1.2
MAX_STOP_PCT = 2.5
MAX_HOLD_HOURS = 24 * 7
TZ_OFFSET_MS = 3 * 3_600_000

FIELDNAMES = [
    "shadow_id", "symbol", "entry_bar_open_time", "entry_time_ms", "entry_time_local", "entry_date_local",
    "entry_hour_local", "in_focus_hours", "entry_price", "target_price", "stop_price", "stop_pct",
    "atr_at_entry", "rsi2", "rsi14", "distance_from_ema20_pct", "d1_close", "d1_sma50",
    "d1_gap_pct", "outcome", "exit_reason", "exit_time_ms", "exit_price",
    "duration_minutes", "net_return_pct", "max_favorable_pct",
    "ibs", "lane",  # ibs = قوة الشمعة الداخلية | lane: pullback (الافتراضي) | ibs
]


def _path(data_dir: str) -> Path:
    return Path(data_dir) / SHADOW_STRATEGY_FILE


def load_rows(data_dir: str) -> list[dict]:
    path = _path(data_dir)
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def append_row(data_dir: str, row: dict) -> None:
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with open(path, "a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in FIELDNAMES})


def update_row(data_dir: str, shadow_id: str, updates: dict) -> None:
    """يحدّث صفًا مغلقًا (يُستخدم عند تسوية الظلّي)."""
    path = _path(data_dir)
    rows = load_rows(data_dir)
    changed = False
    for row in rows:
        if str(row.get("shadow_id")) == str(shadow_id):
            row.update({k: v for k, v in updates.items() if k in FIELDNAMES})
            changed = True
    if not changed:
        return
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def pullback_mask(df: pd.DataFrame, settings: StrategySettings | None = None) -> pd.Series:
    """شرط الدخول الساعي: أول إغلاق تحت متوسط 20 ساعة (بُنية سببية بلا نظر للمستقبل)."""
    settings = settings or StrategySettings()
    frame = prepare_strategy_frame(df, settings)
    close = frame["close"]
    ema20 = ema(close, 20)
    prev_below = close.shift(1) >= ema20.shift(1)
    return (close < ema20) & prev_below


def build_candidate(df_closed: pd.DataFrame, symbol: str, daily_trend_ok: bool) -> dict | None:
    """يبني مرشح الظلّي من شمعة مغلقة — أو None إن لم تتحقق الشروط."""
    if df_closed.empty or len(df_closed) < 60:
        return None
    settings = StrategySettings()
    mask = pullback_mask(df_closed, settings)
    last = df_closed.iloc[-1]
    if not bool(mask.iloc[-1]):
        return None
    open_ms = int(last["open_time"])
    hour = ((open_ms + TZ_OFFSET_MS) // 3_600_000) % 24
    if hour not in GOOD_HOURS:
        return None
    if not daily_trend_ok:
        return None

    entry = float(last["close"])
    frame = prepare_strategy_frame(df_closed, settings)
    atr_value = frame["atr"].iloc[-1]
    if pd.isna(atr_value) or entry <= 0:
        return None
    stop_pct = float(min(max(STOP_ATR_MULT * float(atr_value) / entry * 100.0, MIN_STOP_PCT), MAX_STOP_PCT))
    ema20 = float(ema(df_closed["close"], 20).iloc[-1])
    rsi2 = float(rsi(df_closed["close"], 2).iloc[-1])
    rsi14 = float(frame["rsi"].iloc[-1])
    return {
        "symbol": symbol,
        "entry_time_ms": open_ms + 3_600_000 - 1,   # لحظة إغلاق شمعة الإشارة (نفس توقيت البوت)
        "in_focus_hours": 1 if int(hour) in FOCUS_HOURS else 0,
        "entry_hour_local": int(hour),
        "entry_price": round(entry, 10),
        "target_price": round(entry * (1 + TARGET_PCT / 100.0), 10),
        "stop_price": round(entry * (1 - stop_pct / 100.0), 10),
        "stop_pct": round(stop_pct, 6),
        "atr_at_entry": round(float(atr_value), 10),
        "rsi2": round(rsi2, 4) if pd.notna(rsi2) else "",
        "rsi14": round(rsi14, 4) if pd.notna(rsi14) else "",
        "distance_from_ema20_pct": round((entry - ema20) / ema20 * 100.0, 6) if ema20 else "",
        "lane": "pullback",
        "outcome": "",
        "exit_reason": "",
    }


def ibs_value(df_closed: pd.DataFrame) -> float | None:
    """قوة الشمعة الداخلية لنطاق اليوم الجاري: (إغلاق − أدنى اليوم) / (أعلى اليوم − أدنى اليوم).

    تُحسب من الشمعات **المغلقة** فقط من نفس اليوم (UTC) — بلا أي نظر للمستقبل.
    """
    if df_closed.empty:
        return None
    last = df_closed.iloc[-1]
    day = int(last["open_time"]) // 86_400_000
    opens = df_closed["open_time"].astype("int64") // 86_400_000
    today = df_closed[opens == day]
    if today.empty:
        return None
    hi, lo = float(today["high"].max()), float(today["low"].min())
    close = float(last["close"])
    if hi <= lo:
        return None
    return (close - lo) / (hi - lo)


def build_ibs_candidate(df_closed: pd.DataFrame, symbol: str, daily_trend_ok: bool) -> dict | None:
    """مرشح مسار IBS (من دفعة استراتيجيات يوتيوب): IBS<0.2 + اتجاه يومي صاعد + ساعات 0/5/6."""
    if df_closed.empty or len(df_closed) < 60 or not daily_trend_ok:
        return None
    settings = StrategySettings()
    last = df_closed.iloc[-1]
    open_ms = int(last["open_time"])
    hour = ((open_ms + TZ_OFFSET_MS) // 3_600_000) % 24
    if hour not in FOCUS_HOURS:
        return None
    ibs = ibs_value(df_closed)
    if ibs is None or ibs >= 0.2:
        return None
    entry = float(last["close"])
    frame = prepare_strategy_frame(df_closed, settings)
    atr_value = frame["atr"].iloc[-1]
    if pd.isna(atr_value) or entry <= 0:
        return None
    stop_pct = float(min(max(STOP_ATR_MULT * float(atr_value) / entry * 100.0, MIN_STOP_PCT), MAX_STOP_PCT))
    ema20 = float(ema(df_closed["close"], 20).iloc[-1])
    rsi2 = float(rsi(df_closed["close"], 2).iloc[-1])
    rsi14 = float(frame["rsi"].iloc[-1])
    return {
        "symbol": symbol,
        "entry_time_ms": open_ms + 3_600_000 - 1,
        "in_focus_hours": 1,
        "entry_hour_local": int(hour),
        "entry_price": round(entry, 10),
        "target_price": round(entry * (1 + TARGET_PCT / 100.0), 10),
        "stop_price": round(entry * (1 - stop_pct / 100.0), 10),
        "stop_pct": round(stop_pct, 6),
        "atr_at_entry": round(float(atr_value), 10),
        "rsi2": round(rsi2, 4) if pd.notna(rsi2) else "",
        "rsi14": round(rsi14, 4) if pd.notna(rsi14) else "",
        "distance_from_ema20_pct": round((entry - ema20) / ema20 * 100.0, 6) if ema20 else "",
        "ibs": round(ibs, 6),
        "lane": "ibs",
        "outcome": "",
        "exit_reason": "",
    }


def daily_trend_from_daily_klines(daily: pd.DataFrame) -> tuple[bool, float, float]:
    """اتجاه يومي: إغلاق آخر يوم مكتمل > متوسط 50 يومًا. لا نستخدم اليوم الجاري."""
    if daily is None or daily.empty or len(daily) < 51:
        return False, float("nan"), float("nan")
    closes = daily["close"].astype(float)
    completed = closes.iloc[:-1] if len(closes) > 51 else closes  # نتجاهل آخر يوم (قد يكون جاريًا)
    sma50 = completed.rolling(50, min_periods=50).mean()
    last_close = float(completed.iloc[-1])
    last_sma = float(sma50.iloc[-1]) if pd.notna(sma50.iloc[-1]) else float("nan")
    return (last_close > last_sma), last_close, last_sma
