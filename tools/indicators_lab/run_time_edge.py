"""مختبر الدخول الذكي: قياس الأثر الحقيقي لكل مكوّن (الزمني، الزخم، المتوسطات) على فريم الساعة.

المنهجية:
1. نبضة أساس: دخول في كل شمعة (مع تهدئة) → تجميع النتائج حسب ساعة الدخول بتوقيت الرياض.
   هذا يقيس "أثر الوقت" وحده لأن كل شمعة مرشحة للدخول.
2. تفكيك المكونات: كل فلتر على حدة، ثم تركيبات.
3. تحقق خارج العينة: اختيار الساعات الجيدة من النصف الأول فقط، واختبارها على النصف الثاني.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.strategy import StrategySettings, prepare_strategy_frame  # noqa: E402

from . import engine  # noqa: E402
from .data import load_universe  # noqa: E402

WARMUP = 210
COOLDOWN = 3
TIMEZONE_OFFSET_HOURS = 3  # Asia/Riyadh


def local_hour(open_time_ms: np.ndarray) -> np.ndarray:
    return ((open_time_ms // 3_600_000) + TIMEZONE_OFFSET_HOURS) % 24


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    settings = StrategySettings()
    frame = prepare_strategy_frame(df, settings)
    close = frame["close"]

    feats = pd.DataFrame(index=df.index)

    # --- المتوسطات ---
    feats["ema_stack"] = (
        (close > frame["ema_slow"])
        & (frame["ema_fast"] > frame["ema_mid"])
        & (close > frame["ema_fast"])
    )
    # ارتداد إلى المتوسط السريع داخل اتجاه صاعد (شراء التراجع)
    distance_fast = (close - frame["ema_fast"]).abs() / frame["ema_fast"] * 100.0
    feats["ema_pullback"] = (
        (close > frame["ema_slow"])
        & (frame["ema_mid"] > frame["ema_slow"])
        & (distance_fast <= 1.0)
    )
    # تقاطع صاعد
    feats["ema_cross"] = (frame["ema_fast"] > frame["ema_mid"]) & (frame["ema_fast"].shift(1) <= frame["ema_mid"].shift(1))

    # --- الزخم ---
    rsi_rising = frame["rsi"] > frame["rsi"].shift(1)
    feats["momentum_rsi"] = (frame["rsi"] > 50) & (frame["rsi"] < 70) & rsi_rising
    macd_rising = frame["macd_hist"] > frame["macd_hist"].shift(1)
    feats["momentum_macd"] = (frame["macd_hist"] > 0) & macd_rising
    feats["momentum_stoch"] = (frame["stoch"] > frame["stoch"].shift(1)) & (frame["stoch"] > 20) & (frame["stoch"] < 80)
    feats["momentum_any"] = feats["momentum_rsi"] | feats["momentum_macd"]
    feats["momentum_all"] = feats["momentum_rsi"] & feats["momentum_macd"] & feats["momentum_stoch"]

    # --- الزخم + الاتجاه ---
    feats["mom_plus_trend"] = feats["momentum_any"] & (close > frame["ema_slow"])
    feats["mom_plus_stack"] = feats["momentum_any"] & feats["ema_stack"]
    feats["pullback_plus_momentum"] = feats["ema_pullback"] & feats["momentum_rsi"]
    return feats


def run_symbol(df: pd.DataFrame, feats: pd.DataFrame, entries: pd.Series, tag: str) -> pd.DataFrame:
    atr_series = engine.atr(df, 14) if hasattr(engine, "atr") else None
    from .indicators import atr as compute_atr

    atr_series = compute_atr(df, 14)
    trades = engine.simulate(df, entries, atr_series, cooldown_bars=COOLDOWN)
    if trades.empty:
        return trades
    trades["entry_hour"] = local_hour(df["open_time"].to_numpy(dtype=np.int64)[trades["entry_index"].to_numpy()])
    trades["strategy"] = tag
    return trades


def summarize(arr: np.ndarray) -> dict:
    arr = arr[~np.isnan(arr)]
    if len(arr) < 5:
        return {"n": len(arr), "win_rate": 0.0, "avg_net": 0.0, "t": 0.0, "median": 0.0}
    wins = (arr > 0)
    t = float(arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr)))) if arr.std(ddof=1) > 0 else 0.0
    return {
        "n": int(len(arr)),
        "win_rate": round(float(wins.mean() * 100), 2),
        "avg_net": round(float(arr.mean()), 4),
        "median": round(float(np.median(arr)), 4),
        "t": round(t, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--bars", type=int, default=2000)
    parser.add_argument("--out", default="reports/time_edge")
    args = parser.parse_args()

    data = load_universe(interval=args.interval, bars=args.bars)
    print(f"loaded {len(data)} symbols", file=sys.stderr)

    all_trades: list[pd.DataFrame] = []
    for i, (symbol, df) in enumerate(sorted(data.items()), 1):
        if len(df) < WARMUP + 60:
            continue
        try:
            feats = build_features(df)
        except Exception:
            continue

        # نبضة أساس: كل شمعة مؤهلة → نُجمّع حسب الساعة
        eligible = pd.Series(True, index=df.index)
        eligible.iloc[:WARMUP] = False
        base = run_symbol(df, feats, eligible, "all_bars")
        if not base.empty:
            base["symbol"] = symbol
            all_trades.append(base)

        combos = {
            "ema_stack": feats["ema_stack"].fillna(False),
            "ema_pullback": feats["ema_pullback"].fillna(False),
            "ema_cross": feats["ema_cross"].fillna(False),
            "momentum_rsi": feats["momentum_rsi"].fillna(False),
            "momentum_macd": feats["momentum_macd"].fillna(False),
            "momentum_any": feats["momentum_any"].fillna(False),
            "momentum_all": feats["momentum_all"].fillna(False),
            "mom_plus_trend": feats["mom_plus_trend"].fillna(False),
            "mom_plus_stack": feats["mom_plus_stack"].fillna(False),
            "pullback_plus_momentum": feats["pullback_plus_momentum"].fillna(False),
        }
        for name, mask in combos.items():
            mask = mask.copy()
            mask.iloc[:WARMUP] = False
            t = run_symbol(df, feats, mask, name)
            if not t.empty:
                t["symbol"] = symbol
                all_trades.append(t)

        if i % 100 == 0:
            print(f"  {i}/{len(data)}", file=sys.stderr, flush=True)

    trades = pd.concat(all_trades, ignore_index=True)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    trades.to_csv(out_dir / "all_trades.csv.gz", index=False, compression="gzip")

    # ---------- 1) جدول ساعة الدخول على النبضة الأساس ----------
    base = trades[trades["strategy"] == "all_bars"]
    rows = []
    for h in range(24):
        arr = base[base["entry_hour"] == h]["net_pct"].to_numpy(dtype=float)
        rows.append({"hour_local": h, **summarize(arr)})
    hours = pd.DataFrame(rows)
    overall = summarize(base["net_pct"].to_numpy(dtype=float))
    hours["vs_all"] = (hours["avg_net"] - overall["avg_net"]).round(4)
    hours.to_csv(out_dir / "hour_of_day.csv", index=False)

    print("\n================ أثر ساعة الدخول (توقيت الرياض) ================")
    print(f"{'الساعة':>7}{'صفقات':>9}{'نجاح%':>9}{'متوسط%':>10}{'وسيط%':>9}{'t':>8}{'فرق عن المتوسط':>16}")
    for _, r in hours.iterrows():
        mark = " ★" if r["avg_net"] > overall["avg_net"] and r["n"] >= 300 else (" ▁" if r["avg_net"] < overall["avg_net"] and r["n"] >= 300 else "")
        print(f"{int(r['hour_local']):>7}{int(r['n']):>9}{r['win_rate']:>9.1f}{r['avg_net']:>10.3f}{r['median']:>9.3f}{r['t']:>8.2f}{r['vs_all']:>16.3f}{mark}")
    print(f"{'الإجمالي':>7}{overall['n']:>9}{overall['win_rate']:>9.1f}{overall['avg_net']:>10.3f}{overall['median']:>9.3f}{overall['t']:>8.2f}")

    # ---------- 2) مقارنة الاستراتيجيات ----------
    rows = []
    for name in sorted(trades["strategy"].unique()):
        sub = trades[trades["strategy"] == name]
        s = summarize(sub["net_pct"].to_numpy(dtype=float))
        s["strategy"] = name
        rows.append(s)
    table = pd.DataFrame(rows)[["strategy", "n", "win_rate", "avg_net", "median", "t"]].sort_values("avg_net", ascending=False)
    table.to_csv(out_dir / "strategies.csv", index=False)
    print("\n================ مقارنة الاستراتيجيات ================")
    print(table.to_string(index=False))

    # ---------- 3) تحقق خارج العينة للساعات ----------
    base = base.sort_values("entry_time")
    mid_time = int(base["entry_time"].median())
    train = base[base["entry_time"] <= mid_time]
    test = base[base["entry_time"] > mid_time]

    train_hours = train.groupby("entry_hour")["net_pct"].agg(["mean", "count"])
    train_hours = train_hours[train_hours["count"] >= 150]
    good_hours = [int(h) for h, r in train_hours.iterrows() if r["mean"] > 0]
    bad_hours = [int(h) for h, r in train_hours.iterrows() if r["mean"] <= 0]

    def subset(sub: pd.DataFrame, hs: list[int]) -> np.ndarray:
        return sub[sub["entry_hour"].isin(hs)]["net_pct"].to_numpy(dtype=float)

    oos = {
        "train_period": {
            "all": summarize(train["net_pct"].to_numpy(dtype=float)),
            "good_hours": summarize(subset(train, good_hours)),
            "bad_hours": summarize(subset(train, bad_hours)),
        },
        "test_period": {
            "all": summarize(test["net_pct"].to_numpy(dtype=float)),
            "good_hours": summarize(subset(test, good_hours)),
            "bad_hours": summarize(subset(test, bad_hours)),
        },
        "good_hours_selected": good_hours,
    }
    (out_dir / "oos_hours.json").write_text(json.dumps(oos, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n================ تحقق خارج العينة (اختيار الساعات من النصف الأول) ================")
    print(f"الساعات الجيدة المختارة من النصف الأول: {good_hours}")
    for period in ("train_period", "test_period"):
        print(f"\n--- {period} ---")
        for label in ("all", "good_hours", "bad_hours"):
            s = oos[period][label]
            print(f"  {label:12} n={s['n']:>7} نجاح {s['win_rate']:5.1f}% متوسط {s['avg_net']:+.4f}% t={s['t']:+.2f}")

    # ---------- 4) تركيبات الوقت + الزخم + المتوسطات ----------
    print("\n================ تركيبات (النصف الثاني فقط = خارج العينة) ================")
    test_all = trades[trades["entry_time"] > mid_time]
    rows = []
    for name in sorted(test_all["strategy"].unique()):
        sub = test_all[test_all["strategy"] == name]
        s = summarize(sub["net_pct"].to_numpy(dtype=float))
        s["strategy"] = name
        rows.append(s)
        # مع فلتر الساعات الجيدة
        s2 = summarize(subset(sub, good_hours))
        s2["strategy"] = f"{name} + ساعات جيدة"
        rows.append(s2)
    combo = pd.DataFrame(rows)[["strategy", "n", "win_rate", "avg_net", "median", "t"]].sort_values("avg_net", ascending=False)
    combo.to_csv(out_dir / "combos_oos.csv", index=False)
    print(combo.to_string(index=False))

    print(f"\nالمخرجات: {out_dir}")


if __name__ == "__main__":
    main()
