"""مختبر الأنظمة الكمية: يقيس 14 نظامًا جديدًا على نفس البيانات ونفس القواعد.

المقارنة تُشمل:
- خط الأساس: نبضة كل شمعة (`all_bars` من مختبر الزمن) ونظام البوت الحالي.
- تقسيم خارج العينة: النصف الأول تدريب، النصف الثاني اختبار.
- متانة عبر العملات: كم عملة تحسّنت مقابل خط الأساس.

الاستخدام:
    python -m tools.indicators_lab.run_quant_lab --bars 2000 --out reports/quant_lab
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from . import engine, systems  # noqa: E402
from .data import load_universe  # noqa: E402
from .quant_systems import QUANT_SYSTEMS, SIGNAL_EXIT_SYSTEMS, build_quant_systems  # noqa: E402

WARMUP_BARS = 210


def measure(net: np.ndarray) -> dict:
    n = len(net)
    if n == 0:
        return {"n": 0, "win_rate": float("nan"), "avg_net": float("nan"), "median": float("nan"), "t": float("nan")}
    mean = float(net.mean())
    std = float(net.std(ddof=1)) if n > 1 else float("nan")
    t = mean / (std / np.sqrt(n)) if std and not np.isnan(std) and std > 0 else float("nan")
    return {
        "n": n,
        "win_rate": float((net > 0).mean() * 100.0),
        "avg_net": mean,
        "median": float(np.median(net)),
        "t": float(t),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--bars", type=int, default=2000)
    parser.add_argument("--out", default="reports/quant_lab")
    parser.add_argument("--symbols-limit", type=int, default=0, help="0 = كل العملات")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("تحميل البيانات…", file=sys.stderr)
    data = load_universe(interval=args.interval, bars=args.bars)
    if args.symbols_limit:
        data = dict(sorted(data.items())[: args.symbols_limit])
    print(f"العملات: {len(data)}", file=sys.stderr)

    # خط الأساس: نبضة كل شمعة (دخول كل شمعة، نفس قواعد المحرك)
    baseline_name = "base_all_bars"
    bot_name = "bot_current"
    names = [baseline_name, bot_name] + QUANT_SYSTEMS

    collected: dict[str, list[pd.DataFrame]] = {name: [] for name in names}
    symbols = sorted(data.keys())
    for idx, symbol in enumerate(symbols, 1):
        df = data[symbol]
        if len(df) < WARMUP_BARS + 50:
            continue
        try:
            quant = build_quant_systems(df)
            base_systems = systems.build_all_systems(df)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {symbol}: {exc}", file=sys.stderr)
            continue
        atr_series = systems.ind.atr(df, 14)

        # خط الأساس: كل شمعة إشارة
        base_entry = pd.Series(True, index=df.index)
        base_spec = pd.DataFrame({"entry": base_entry})
        runs = {baseline_name: (base_spec, 1)}
        bot_spec = base_systems.get("bot_current_pivot_stop")
        if bot_spec is not None:
            runs[bot_name] = (bot_spec, 3)
        for qname in QUANT_SYSTEMS:
            spec = quant.get(qname)
            if spec is not None:
                runs[qname] = (spec, 1)

        for name, (spec, cooldown) in runs.items():
            exit_mode = "intrabar"
            exit_signal = None
            if name in SIGNAL_EXIT_SYSTEMS and "exit_signal" in spec.columns:
                exit_mode = "signal"
                exit_signal = spec["exit_signal"]
            stop_override = spec["stop_pct"] if "stop_pct" in spec.columns else None
            try:
                trades = engine.simulate(
                    df, spec["entry"], atr_series,
                    cooldown_bars=cooldown, stop_pct_series=stop_override,
                    exit_mode=exit_mode, exit_signal=exit_signal,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  {symbol}/{name} خطأ: {exc}", file=sys.stderr)
                continue
            if trades.empty:
                continue
            trades["symbol"] = symbol
            trades["system"] = name
            trades["entry_hour"] = ((trades["entry_time"] + 3 * 3_600_000) // 3_600_000 % 24).astype(int)
            collected[name].append(trades)

        if idx % 50 == 0:
            print(f"  {idx}/{len(symbols)}", file=sys.stderr)

    # ===== تجميع النتائج =====
    summary_rows = []
    per_symbol_rows = []
    all_frames: dict[str, pd.DataFrame] = {}
    for name, frames in collected.items():
        if not frames:
            continue
        merged = pd.concat(frames, ignore_index=True)
        all_frames[name] = merged
        net = merged["net_pct"].to_numpy(dtype=float)
        row = {"system": name, **measure(net)}
        # خارج العينة: النصفان
        mid = int(np.median(merged["entry_time"].to_numpy(dtype=np.int64)))
        train = merged[merged["entry_time"] <= mid]["net_pct"].to_numpy(dtype=float)
        test = merged[merged["entry_time"] > mid]["net_pct"].to_numpy(dtype=float)
        tr, te = measure(train), measure(test)
        row.update({
            "train_n": tr["n"], "train_avg": tr["avg_net"], "train_t": tr["t"],
            "test_n": te["n"], "test_avg": te["avg_net"], "test_t": te["t"], "test_win": te["win_rate"],
        })
        summary_rows.append(row)

        grouped = merged.groupby("symbol")["net_pct"].agg(["mean", "count"])
        grouped = grouped[grouped["count"] >= 10]
        for symbol, r in grouped.iterrows():
            per_symbol_rows.append({"system": name, "symbol": symbol, "avg_net": r["mean"], "n": int(r["count"])})

    summary = pd.DataFrame(summary_rows).sort_values("avg_net", ascending=False)
    summary.to_csv(out_dir / "quant_summary.csv", index=False)

    # خط الأساس لكل خلية (لكل عملة) لحساب الفرق
    base_by_symbol = None
    if baseline_name in all_frames:
        base_by_symbol = all_frames[baseline_name].groupby("symbol")["net_pct"].mean()

    robustness = []
    per_symbol = pd.DataFrame(per_symbol_rows)
    if not per_symbol.empty and base_by_symbol is not None:
        for name in per_symbol["system"].unique():
            sub = per_symbol[per_symbol["system"] == name].copy()
            sub["base"] = sub["symbol"].map(base_by_symbol)
            sub = sub.dropna(subset=["base"])
            if sub.empty:
                continue
            diff = (sub["avg_net"] - sub["base"]).to_numpy(dtype=float)
            robustness.append({
                "system": name,
                "symbols": len(sub),
                "better_share": float((diff > 0).mean() * 100.0),
                "avg_diff": float(diff.mean()),
                "t_diff": float(diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff)))) if len(diff) > 1 and diff.std(ddof=1) > 0 else float("nan"),
            })
    rob = pd.DataFrame(robustness).sort_values("avg_diff", ascending=False)
    rob.to_csv(out_dir / "quant_robustness.csv", index=False)

    # توزيع ساعات الدخول للنظام الأفضل
    hours_out = {}
    for name, merged in all_frames.items():
        h = merged.groupby("entry_hour")["net_pct"].agg(["mean", "count"])
        hours_out[name] = {int(k): {"avg": float(v["mean"]), "n": int(v["count"])} for k, v in h.iterrows()}
    (out_dir / "quant_hours.json").write_text(json.dumps(hours_out, ensure_ascii=False, indent=1), encoding="utf-8")

    # ===== الطباعة =====
    def show(df: pd.DataFrame, cols: list[str], title: str) -> None:
        print(f"\n{'=' * 90}\n{title}\n{'=' * 90}")
        with pd.option_context("display.width", 200, "display.max_columns", 50):
            print(df[cols].to_string(index=False, float_format=lambda v: f"{v:7.3f}"))

    show(summary, ["system", "n", "win_rate", "avg_net", "median", "t", "train_avg", "test_avg", "test_t", "test_win"],
         "كل الأنظمة (مرتّبة بالمتوسط) — train_avg/test_avg = النصف الأول/الثاني")
    if not rob.empty:
        show(rob, ["system", "symbols", "better_share", "avg_diff", "t_diff"], "المتانة عبر العملات (مقابل نبضة كل شمعة)")

    print(f"\nحُفظت النتائج في {out_dir}/")


if __name__ == "__main__":
    main()
