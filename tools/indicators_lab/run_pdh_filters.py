"""المرحلة 2: فلترة استراتيجية «اختراق قمة/إغلاق الأمس».

نجرّب خطين (قمة الأمس PDH / إغلاق الأمس PDC) × طريقتين للخروج
(هدف 1.2% • خروج تحت الخط) × ست مجموعات فلاتر — ونحكم بقاعدة النصفين.
الفلاتر تُطبَّق على صفقات مُحاكاة مسبقًا (تصفية لاحقة للخصائص وقت الدخول)،
لذلك تُقرأ كنتائج استكشافية لا نهائية.
المخرج: reports/pdh_filters.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.strategy import StrategySettings, prepare_strategy_frame  # noqa: E402

CACHE = ROOT / "tools" / "indicators_lab" / "data_cache" / "1h"
FEE, ATR_MULT, MIN_STOP, MAX_STOP = 0.1, 1.5, 1.2, 2.5
MAX_HOLD_BARS = 48
TZ_OFFSET_MS = 3 * 3_600_000
FOCUS_HOURS = {0, 5, 6}


def day_levels(open_ms: np.ndarray, high: np.ndarray, close: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    day = open_ms // 86_400_000
    df = pd.DataFrame({"day": day, "high": high, "close": close})
    daily = df.groupby("day").agg(h=("high", "max"), c=("close", "last"))
    prev = daily.shift(1)
    return prev["h"].reindex(day).to_numpy(), prev["c"].reindex(day).to_numpy()


def sim(df, atr, level: str, exit_kind: str, tgt: float = 1.2) -> list[dict]:
    close = df["close"].to_numpy(float); high = df["high"].to_numpy(float); low = df["low"].to_numpy(float)
    open_ms = df["open_time"].to_numpy(np.int64); vol = df["volume"].to_numpy(float)
    pdh, pdc = day_levels(open_ms, high, close)
    lvl = pdh if level == "pdh" else pdc
    vma = pd.Series(vol).rolling(20, min_periods=10).mean().to_numpy()
    n = len(df); out = []; last_exit = -10**9
    for i in range(1, n - 1):
        if np.isnan(lvl[i]) or np.isnan(lvl[i - 1]) or np.isnan(atr[i]) or (i - last_exit) < 1:
            continue
        if not (close[i] > lvl[i] and close[i - 1] <= lvl[i - 1]):
            continue
        entry = close[i]
        stop_pct = float(np.clip(ATR_MULT * atr[i] / entry * 100.0, MIN_STOP, MAX_STOP))
        stop = entry * (1 - stop_pct / 100.0)
        target = entry * (1 + tgt / 100.0)
        outcome = exit_price = exit_i = reason = None
        for j in range(i + 1, min(i + 1 + MAX_HOLD_BARS, n)):
            if low[j] <= stop:
                outcome, exit_price, exit_i, reason = "stop", stop, j, "stop"; break
            if exit_kind == "target" and high[j] >= target:
                outcome, exit_price, exit_i, reason = "target", target, j, "target"; break
            if exit_kind == "below_level" and close[j] < lvl[j]:
                outcome = "signal_win" if close[j] > entry else "signal_loss"
                exit_price, exit_i, reason = float(close[j]), j, "below_level"; break
        if outcome is None:
            j = min(i + MAX_HOLD_BARS, n - 1)
            outcome = "timeout_win" if close[j] > entry else "timeout_loss"
            exit_price, exit_i, reason = float(close[j]), j, "timeout"
        net = (exit_price / entry - 1.0) * 100.0 - 2 * FEE
        out.append({
            "entry_time": int(open_ms[i]), "net_pct": net, "outcome": outcome, "exit_reason": reason,
            "hour": int(((open_ms[i] + TZ_OFFSET_MS) // 3_600_000) % 24),
            "rvol": float(vol[i] / vma[i]) if vma[i] and not np.isnan(vma[i]) else np.nan,
            "gap_pct": float((close[i] - lvl[i]) / lvl[i] * 100.0),  # كم تجاوز الإغلاق الخط
            "stop_pct": stop_pct,
        })
        last_exit = exit_i
    return out


def main() -> None:
    settings = StrategySettings()
    rows_by_key: dict[tuple[str, str], list[pd.DataFrame]] = {}
    files = sorted(CACHE.glob("*.csv.gz"))
    for n, path in enumerate(files, 1):
        symbol = path.name.replace(".csv.gz", "")
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        if len(df) < 500:
            continue
        atr = prepare_strategy_frame(df, settings)["atr"].to_numpy(float)
        # اتجاه يومي صاعد (إغلاق أمس فوق EMA20 لأمس) لحظة كل صفقة
        close = df["close"].to_numpy(float); open_ms = df["open_time"].to_numpy(np.int64)
        day = open_ms // 86_400_000
        daily = pd.DataFrame({"day": day, "close": close}).groupby("day")["close"].last()
        # ⚠️ إصلاح نظر-للمستقبل: نستخدم **إغلاق اليوم السابق** مقابل EMA حتى اليوم السابق فقط
        d_ema = daily.ewm(span=20, adjust=False, min_periods=10).mean().shift(1)
        trend_map = (daily.shift(1) > d_ema).reindex(day).to_numpy()
        for level in ("pdh", "pdc"):
            for exit_kind in ("target", "below_level"):
                tr = sim(df, atr, level, exit_kind)
                if not tr:
                    continue
                t = pd.DataFrame(tr)
                t["symbol"] = symbol
                # اتجاه يومي لكل صفقة عند لحظة دخولها (بلا نظر للمستقبل)
                t["_entry_i"] = t["entry_time"].map(lambda ms: int(np.searchsorted(open_ms, ms)))
                t["d1_up"] = [bool(trend_map[min(i, len(trend_map) - 1)]) if not np.isnan(trend_map[min(i, len(trend_map) - 1)]) else False for i in t["_entry_i"]]
                t = t.drop(columns=["_entry_i"])
                rows_by_key.setdefault((level, exit_kind), []).append(t)
        if n % 120 == 0:
            print(f"  ... {n}/{len(files)}")

    filt_sets = {
        "بلا فلتر": lambda d: pd.Series(True, index=d.index),
        "اتجاه يومي صاعد": lambda d: d["d1_up"],
        "الساعات المركّزة 0/5/6": lambda d: d["hour"].isin(FOCUS_HOURS),
        "حجم نسبي ≥ 1.2": lambda d: d["rvol"] >= 1.2,
        "تجاوز صغير (<1%)": lambda d: d["gap_pct"] < 1.0,
        "اتجاه صاعد + ساعات مركّزة": lambda d: d["d1_up"] & d["hour"].isin(FOCUS_HOURS),
        "ساعات مركّزة + تجاوز صغير": lambda d: d["hour"].isin(FOCUS_HOURS) & (d["gap_pct"] < 1.0),
        "اتجاه صاعد + تجاوز صغير": lambda d: d["d1_up"] & (d["gap_pct"] < 1.0),
    }

    results = []
    for (level, exit_kind), parts in rows_by_key.items():
        t = pd.concat(parts, ignore_index=True)
        t["entry_dt"] = pd.to_datetime(t["entry_time"], unit="ms", utc=True)
        mid = t["entry_dt"].quantile(0.5, interpolation="nearest")
        for fname, fmask in filt_sets.items():
            sub = t[fmask(t).fillna(False)]
            h1, h2 = sub[sub["entry_dt"] < mid], sub[sub["entry_dt"] >= mid]
            results.append({
                "الخط": "قمة الأمس (PDH)" if level == "pdh" else "إغلاق الأمس (PDC)",
                "الخروج": "هدف 1.2% + وقف" if exit_kind == "target" else "تحت الخط + وقف",
                "الفلتر": fname, "n": len(sub),
                "نجاح%": (sub["net_pct"] > 0).mean() * 100 if len(sub) else np.nan,
                "متوسط%": sub["net_pct"].mean() if len(sub) else np.nan,
                "نصف1%": h1["net_pct"].mean() if len(h1) else np.nan,
                "نصف2%": h2["net_pct"].mean() if len(h2) else np.nan,
            })
    res = pd.DataFrame(results).sort_values("متوسط%", ascending=False)
    res.to_csv(ROOT / "reports" / "pdh_filters.csv", index=False, float_format="%.3f")
    pos_both = res[(res["نصف1%"] > 0) & (res["نصف2%"] > 0) & (res["n"] >= 200)]
    print("=== أعلى 12 تركيبة ===")
    print(res.head(12).to_string(index=False, float_format=lambda x: f"{x:+.3f}"))
    print(f"\nإيجابية في النصفين (n≥200): {len(pos_both)}")
    if len(pos_both):
        print(pos_both.to_string(index=False, float_format=lambda x: f"{x:+.3f}"))


if __name__ == "__main__":
    main()
