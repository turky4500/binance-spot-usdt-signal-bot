"""تحقق متقدم من الأثر الزمني: متانة عبر العملات + تحقق عكسي + مع قواعد الخروج الثلاث."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.strategy import StrategySettings, prepare_strategy_frame  # noqa: E402

from . import engine  # noqa: E402
from .data import load_universe  # noqa: E402
from .indicators import atr as compute_atr  # noqa: E402

WARMUP = 210
COOLDOWN = 3
TZ_OFFSET = 3
GOOD_HOURS = [0, 2, 5, 6, 14, 21, 23]  # مختارة من النصف الأول فقط (خارج العينة)


def local_hour(open_time_ms: np.ndarray) -> np.ndarray:
    return ((open_time_ms // 3_600_000) + TZ_OFFSET) % 24


def summarize(arr: np.ndarray, label: str = "") -> dict:
    arr = arr[~np.isnan(arr)]
    if len(arr) < 5:
        return {"label": label, "n": len(arr), "win_rate": 0.0, "avg_net": 0.0, "t": 0.0, "median": 0.0}
    t = float(arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr)))) if arr.std(ddof=1) > 0 else 0.0
    return {
        "label": label, "n": int(len(arr)),
        "win_rate": round(float((arr > 0).mean() * 100), 2),
        "avg_net": round(float(arr.mean()), 4),
        "median": round(float(np.median(arr)), 4),
        "t": round(t, 2),
    }


def main() -> None:
    data = load_universe(interval="1h", bars=2000)
    print(f"loaded {len(data)} symbols", file=sys.stderr)
    out_dir = Path("reports/time_edge")
    out_dir.mkdir(parents=True, exist_ok=True)

    fixed_rows, hybrid_rows = [], []
    for i, (symbol, df) in enumerate(sorted(data.items()), 1):
        if len(df) < WARMUP + 60:
            continue
        atr_series = compute_atr(df, 14)
        open_ms = df["open_time"].to_numpy(dtype=np.int64)
        hours = local_hour(open_ms)

        eligible = pd.Series(True, index=df.index)
        eligible.iloc[:WARMUP] = False

        # 1) هدف ثابت 2% (نفس النبضة السابقة) — لكل الساعات ولكل الهدف
        t_all = engine.simulate(df, eligible, atr_series, cooldown_bars=COOLDOWN)
        if not t_all.empty:
            t_all["entry_hour"] = hours[t_all["entry_index"].to_numpy()]
            t_all["symbol"] = symbol
            fixed_rows.append(t_all)

            # 2) نفس الدخول لكن خروج تتبع 2×ATR (بلا هدف)
            t_trail = engine.simulate(df, eligible, atr_series, cooldown_bars=COOLDOWN, exit_mode="trailing_atr")
            if not t_trail.empty:
                t_trail["entry_hour"] = hours[t_trail["entry_index"].to_numpy()]
                t_trail["symbol"] = symbol
                hybrid_rows.append(t_trail)

        if i % 100 == 0:
            print(f"  {i}/{len(data)}", file=sys.stderr, flush=True)

    fixed = pd.concat(fixed_rows, ignore_index=True)
    trailing = pd.concat(hybrid_rows, ignore_index=True)

    # ---------- المتانة عبر العملات ----------
    print("\n================ المتانة عبر العملات (هدف ثابت 2%) ================")
    per = fixed.groupby(["symbol", "entry_hour"])["net_pct"].agg(["mean", "count"]).reset_index()
    per = per[per["count"] >= 15]
    piv = per.pivot(index="symbol", columns="entry_hour", values="mean")
    good_cols = [c for c in GOOD_HOURS if c in piv.columns]
    bad_cols = [c for c in piv.columns if c not in GOOD_HOURS]
    g = piv[good_cols].mean(axis=1).dropna()
    b = piv[bad_cols].mean(axis=1).dropna()
    common = g.index.intersection(b.index)
    diff = (g[common] - b[common])
    t_paired = float(diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff)))) if len(diff) > 2 else 0.0
    print(f"عدد العملات المقارنة: {len(common)}")
    print(f"عملات نتائجها أفضل في الساعات الجيدة: {(diff > 0).mean() * 100:.1f}%")
    print(f"متوسط الفرق لكل عملة (جيدة − سيئة): {diff.mean():+.4f}% | وسيط {diff.median():+.4f}% | t = {t_paired:+.2f}")

    # ---------- التحقق العكسي: اختيار الساعات من النصف الثاني ----------
    print("\n================ التحقق العكسي (اختيار الساعات من النصف الثاني) ================")
    cutoff = int(fixed["entry_time"].median())
    second = fixed[fixed["entry_time"] > cutoff]
    hours_second = second.groupby("entry_hour")["net_pct"].agg(["mean", "count"])
    hours_second = hours_second[hours_second["count"] >= 120]
    reverse_good = sorted(int(h) for h, r in hours_second.iterrows() if r["mean"] > 0)
    print(f"الساعات المختارة من النصف الثاني: {reverse_good}")
    overlap = sorted(set(reverse_good) & set(GOOD_HOURS))
    print(f"التقاطع مع القائمة الأصلية: {overlap} ({len(overlap)} ساعة من {len(GOOD_HOURS)})")

    # ---------- أداء الساعات الجيدة مع قاعدة الوقف المتحرك ----------
    print("\n================ الساعات مع قاعدة خروج التتبع 2×ATR ================")
    rows = []
    for label, series, col in (("هدف ثابت", fixed, "entry_hour"), ("وقف تتبع", trailing, "entry_hour")):
        for kind, mask in (("كل الساعات", None), ("الساعات الجيدة", GOOD_HOURS)):
            sub = series if mask is None else series[series[col].isin(mask)]
            s = summarize(sub["net_pct"].to_numpy(dtype=float), f"{label} — {kind}")
            rows.append(s)
    table = pd.DataFrame(rows)
    print(table.to_string(index=False))
    table.to_csv(out_dir / "exit_vs_time.csv", index=False)

    # ---------- الجلسات ----------
    print("\n================ الأداء حسب جلسة التداول ================")
    sessions = {
        "آسيا (02:00-10:00 الرياض)": [2, 3, 4, 5, 6, 7, 8, 9],
        "لندن (10:00-18:00 الرياض)": [10, 11, 12, 13, 14, 15, 16, 17],
        "أمريكا (18:00-02:00 الرياض)": [18, 19, 20, 21, 22, 23, 0, 1],
    }
    rows = []
    for name, hrs in sessions.items():
        sub = fixed[fixed["entry_hour"].isin(hrs)]
        rows.append(summarize(sub["net_pct"].to_numpy(dtype=float), name))
    sess = pd.DataFrame(rows)
    print(sess.to_string(index=False))
    sess.to_csv(out_dir / "sessions.csv", index=False)

    # ---------- أفضل/أسوأ نافذة متصلة ----------
    print("\n================ نوافذ متصلة (3 ساعات) ================")
    rows = []
    for start in range(24):
        hrs = [(start + k) % 24 for k in range(3)]
        sub = fixed[fixed["entry_hour"].isin(hrs)]
        s = summarize(sub["net_pct"].to_numpy(dtype=float), f"{hrs[0]:02d}:00–{hrs[-1]:02d}:00")
        rows.append(s)
    windows = pd.DataFrame(rows).sort_values("avg_net", ascending=False)
    print(windows.head(6).to_string(index=False))
    print("...")
    print(windows.tail(4).to_string(index=False))
    windows.to_csv(out_dir / "windows_3h.csv", index=False)

    (out_dir / "robustness.json").write_text(json.dumps({
        "good_hours": GOOD_HOURS,
        "reverse_good_hours": reverse_good,
        "overlap": overlap,
        "per_symbol_better_pct": round(float((diff > 0).mean() * 100), 2),
        "per_symbol_mean_diff": round(float(diff.mean()), 4),
        "per_symbol_paired_t": round(t_paired, 2),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
