"""نظام «فوق قمة الأمس» (Previous-Day-High Breakout) — نظام مستقل بمسارَي قياس.

الفكرة (كما طلب المستخدم):
  - خط = **قمة اليوم السابق** (بالتوقيت العالمي UTC، نفس أيام شمعات Binance اليومية).
  - عندما تُغلق شمعة ساعة فوق الخط لأول مرة (وكانت الشمعة السابقة تحته) → إشارة شراء.

قواعد مُختبرة قبل التشغيل (tools/indicators_lab/run_pdh_breakout.py + run_pdh_filters.py):
  - الهدف الثابت 1–1.5% **سلبي تاريخيًا** (سقف 1.5% → −0.138%/صفقة).
  - أفضل تركيبة كانت خروجًا عند «النزول تحت الخط» مع فلتر اتجاه يومي، وهي قريبة من الصفر (−0.01%).
  - لذلك نسجّل **مسارين معًا** على نفس الإشارة لنحكم بالبيانات الحيّة لا بالرأي:
      المسار أ (الرئيسي): وقف 1.5×ATR • خروج عند إغلاق ساعة تحت قمة الأمس • مهلة 48 ساعة.
      المسار ب (مواصفة المستخدم): وقف 1.5×ATR • هدف +1.2% ثابت • مهلة 48 ساعة.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
TZ_OFFSET_MS = 3 * HOUR_MS  # توقيت الرياض للعرض فقط
FEE_ROUND_TRIP_PCT = 0.2    # 0.1% لكل جهة (نفس محرك المختبر)
ATR_MULT = 1.5
MIN_STOP_PCT, MAX_STOP_PCT = 1.2, 2.5
TARGET_B_PCT = 1.2          # مواصفة المستخدم: هدف صغير
MAX_HOLD_BARS = 48          # مهلة قصوى (ساعتان) لكلا المسارين
GAP_LIMIT_PCT = 1.0         # لا نلاحق اختراقًا متضخّمًا (تجاوز > 1% فوق الخط)

# ضبط الاسم والرسائل
SYSTEM_NAME = "فوق قمة الأمس"
SYSTEM_EMOJI = "🚀"

FIELDNAMES = [
    "signal_id", "symbol", "entry_bar_open_time", "entry_time_ms", "entry_time_local",
    "entry_date_local", "entry_hour_local", "level_price", "entry_price", "stop_price",
    "stop_pct", "target_price", "atr_at_entry", "gap_pct", "d1_trend_ok",
    "outcome_main", "exit_reason_main", "exit_time_ms_main", "exit_price_main",
    "net_main_pct", "duration_minutes_main",
    "outcome_b", "exit_reason_b", "exit_time_ms_b", "exit_price_b",
    "net_b_pct", "duration_minutes_b",
    "max_favorable_pct",
]


def _path(data_dir: str) -> Path:
    return Path(data_dir) / "strategy_pdh.csv"


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


def update_row(data_dir: str, signal_id: str, updates: dict) -> None:
    path = _path(data_dir)
    rows = load_rows(data_dir)
    changed = False
    for row in rows:
        if str(row.get("signal_id")) == str(signal_id):
            row.update({k: v for k, v in updates.items() if k in FIELDNAMES})
            changed = True
    if not changed:
        return
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in FIELDNAMES})


def atr_series(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """ATR(14) بنمط Wilder — بلا أي اعتماد على إطارات أخرى."""
    high, low, close = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def daily_tables(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """أيام UTC المكتملة: (أعلى/إغلاق لكل يوم) + اتجاه يومي صاعد بلا نظر للمستقبل."""
    open_ms = df["open_time"].astype("int64")
    day = open_ms // DAY_MS
    tmp = pd.DataFrame({"day": day.to_numpy(), "high": df["high"].astype(float).to_numpy(),
                        "close": df["close"].astype(float).to_numpy()})
    daily = tmp.groupby("day").agg(high=("high", "max"), close=("close", "last"))
    # اتجاه صاعد: إغلاق اليوم المكتمل > EMA20 حتى نفس اليوم (بلا استخدام أي يوم لاحق)
    ema20 = daily["close"].ewm(span=20, adjust=False, min_periods=8).mean()
    trend = daily["close"] > ema20
    return daily, trend


def prev_day_levels(df: pd.DataFrame) -> pd.Series:
    """قمة الأمس لكل شمعة (قيمة اليوم السابق المكتمل فقط)."""
    open_ms = df["open_time"].astype("int64")
    day = open_ms // DAY_MS
    tmp = pd.DataFrame({"day": day.to_numpy(), "high": df["high"].astype(float).to_numpy()})
    daily_high = tmp.groupby("day")["high"].max()
    return day.map(daily_high.shift(1)).astype(float)


def build_pdh_signal(df: pd.DataFrame, symbol: str, timezone_name: str = "Asia/Riyadh") -> dict | None:
    """هل الشمعة المغلقة الأخيرة = اختراق أول لقمة الأمس (مع فلتر الاتجاه والتضخّم)؟"""
    if df is None or len(df) < 60:
        return None
    df = df.reset_index(drop=True)
    levels = prev_day_levels(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]
    level = levels.iloc[-1]
    level_prev = levels.iloc[-2]
    if pd.isna(level) or pd.isna(level_prev):
        return None
    close, close_prev = float(last["close"]), float(prev["close"])
    # شرط الاختراق: أول إغلاق فوق قمة الأمس
    if not (close > level and close_prev <= level_prev):
        return None
    # فلتر التضخّم: لا نلاحق تجاوزًا كبيرًا
    gap_pct = (close - level) / level * 100.0
    if gap_pct >= GAP_LIMIT_PCT:
        return None
    # فلتر الاتجاه اليومي الصاعد (بلا نظر للمستقبل)
    daily, trend = daily_tables(df)
    last_day = int(df["open_time"].iloc[-1]) // DAY_MS
    if last_day not in daily.index:
        return None
    # يوم الإشارة نفسه لم يكتمل → نستخدم آخر يوم مكتمل (اليوم السابق)
    completed_days = [d for d in trend.index if d < last_day]
    if not completed_days:
        return None
    d1_day = completed_days[-1]
    if not bool(trend.loc[d1_day]):
        return None

    atr_val = float(atr_series(df).iloc[-1])
    if pd.isna(atr_val) or atr_val <= 0:
        return None
    entry = close
    stop_pct = float(min(max(ATR_MULT * atr_val / entry * 100.0, MIN_STOP_PCT), MAX_STOP_PCT))
    open_ms = int(last["open_time"])
    close_ms = int(last["close_time"])
    local = pd.Timestamp(close_ms + TZ_OFFSET_MS, unit="ms", tz="UTC")
    return {
        "signal_id": f"{symbol}-{open_ms}",
        "symbol": symbol,
        "entry_bar_open_time": open_ms,
        "entry_time_ms": close_ms,
        "entry_time_local": local.strftime("%Y-%m-%d %H:%M"),
        "entry_date_local": local.strftime("%Y-%m-%d"),
        "entry_hour_local": int(local.hour),
        "level_price": round(level, 10),
        "entry_price": round(entry, 10),
        "stop_price": round(entry * (1 - stop_pct / 100.0), 10),
        "stop_pct": round(stop_pct, 4),
        "target_price": round(entry * (1 + TARGET_B_PCT / 100.0), 10),
        "atr_at_entry": round(atr_val, 10),
        "gap_pct": round(gap_pct, 4),
        "d1_trend_ok": 1,
    }


def settle_record(record: dict, bar: dict, level_now: float, atr_now: float | None = None) -> dict:
    """يحدّث مسارَي القياس على شمعة مغلقة جديدة. يرجع تحديثات الصف (قد تكون فارغة)."""
    updates: dict = {}
    entry = float(record["entry_price"])
    stop = float(record["stop_price"])
    high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
    bar_open = int(bar["open_time"])
    exit_ms = int(bar["close_time"])
    age = bar_open - int(record["entry_bar_open_time"])
    peak = max(float(record.get("peak_price", entry)), high)
    record["peak_price"] = peak
    record["max_favorable_pct"] = round((peak / entry - 1.0) * 100.0, 6)
    updates["max_favorable_pct"] = record["max_favorable_pct"]

    net = lambda price: round((price / entry - 1.0) * 100.0 - FEE_ROUND_TRIP_PCT, 6)
    minutes = lambda: max(int((exit_ms - int(record["entry_time_ms"])) // 60000), 0)

    stop_hit = low <= stop
    for lane, prefix in (("main", "main"), ("b", "b")):
        if record.get(f"outcome_{prefix}"):
            continue
        outcome = reason = price = None
        if stop_hit:  # تحفّظ: الوقف أولًا دائمًا
            outcome, reason, price = "loss", "stop_loss", stop
        elif lane == "main":
            if not pd.isna(level_now) and close < float(level_now):
                outcome = "win" if close > entry else "loss"
                reason, price = "below_level", close
        else:  # المسار ب — مواصفة المستخدم: هدف +1.2%
            target = float(record["target_price"])
            if high >= target:
                outcome, reason, price = "target", "take_profit", target
        if outcome is None and age >= MAX_HOLD_BARS * HOUR_MS:
            outcome = "win" if close > entry else "loss"
            reason, price = "time_limit", close
        if outcome is None:
            continue
        record[f"outcome_{prefix}"] = outcome
        record[f"exit_reason_{prefix}"] = reason
        record[f"exit_time_ms_{prefix}"] = exit_ms
        record[f"exit_price_{prefix}"] = round(price, 10)
        record[f"net_{prefix}_pct"] = net(price)
        record[f"duration_minutes_{prefix}"] = minutes()
        updates.update({
            f"outcome_{prefix}": outcome, f"exit_reason_{prefix}": reason,
            f"exit_time_ms_{prefix}": exit_ms, f"exit_price_{prefix}": round(price, 10),
            f"net_{prefix}_pct": net(price), f"duration_minutes_{prefix}": minutes(),
        })
    return updates


def both_closed(record: dict) -> bool:
    return bool(record.get("outcome_main")) and bool(record.get("outcome_b"))


def build_entry_message(trade: dict) -> str:
    level = float(trade["level_price"])
    entry = float(trade["entry_price"])
    stop = float(trade["stop_price"])
    target = float(trade["target_price"])
    return (
        f"{SYSTEM_EMOJI} إشارة «{SYSTEM_NAME}»\n"
        f"الزوج: {trade['symbol']}\n"
        f"الفريم: 1H\n"
        f"الشرط: أغلقت شمعة الساعة فوق قمة الأمس ({level:g})\n"
        f"سعر الدخول: {entry:g}\n"
        f"الهدف المقترح: {target:g} (+{TARGET_B_PCT:.1f}%)\n"
        f"وقف الخسارة: {stop:g} (−{float(trade['stop_pct']):.2f}%)\n"
        f"قاعدة الخروج عندنا: إغلاق ساعة تحت قمة الأمس أو الهدف أو الوقف\n"
        f"وقت الإشارة: {trade['entry_time_local']} (السعودية)\n"
        f"─────────────\n"
        f"⚠️ نظام تجريبي: يدخل الحساب التلقائي (ظلّيًا) — القرار لك."
    )


def build_exit_message(trade: dict, lane_ar: str, net_pct: float, exit_price: float, reason_ar: str, duration_txt: str) -> str:
    return (
        f"🏁 خروج «{SYSTEM_NAME}»\n"
        f"الزوج: {trade['symbol']}\n"
        f"المسار: {lane_ar}\n"
        f"السبب: {reason_ar}\n"
        f"دخول {float(trade['entry_price']):g} → خروج {float(exit_price):g}\n"
        f"الصافي: {net_pct:+.3f}%\n"
        f"المدة: {duration_txt}"
    )


EXIT_REASONS_AR = {
    "stop_loss": "وقف الخسارة",
    "take_profit": "بلغ الهدف +1.2%",
    "below_level": "نزلت شمعة ساعة تحت قمة الأمس",
    "time_limit": "انتهت المهلة (48 ساعة)",
}
