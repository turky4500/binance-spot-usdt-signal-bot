"""يشغّل مختبر المؤشرات: يقارن منطق البوت الحالي بمؤشرات مفتوحة المصدر على نفس البيانات والقواعد.

الاستخدام:
    python -m tools.indicators_lab.run_lab --interval 1h --bars 2000 --out reports/lab_1h
    python -m tools.indicators_lab.run_lab --interval 1d --bars 1000 --out reports/lab_1d
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

RANDOM_SEEDS = (11, 22, 33)
WARMUP_BARS = 210


def run_universe(data: dict[str, pd.DataFrame], system_names: list[str], same_bar_rule: str, exit_mode: str, progress_every: int = 50) -> dict:
    collected: dict[str, list[pd.DataFrame]] = {name: [] for name in system_names}
    collected_random: dict[str, dict[int, list[pd.DataFrame]]] = {
        name: {seed: [] for seed in RANDOM_SEEDS} for name in system_names
    }

    symbols = sorted(data.keys())
    for idx, symbol in enumerate(symbols, 1):
        df = data[symbol]
        if len(df) < WARMUP_BARS + 50:
            continue
        try:
            all_systems = systems.build_all_systems(df)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {symbol}: {exc}", file=sys.stderr)
            continue
        atr_series = systems.ind.atr(df, 14)

        for name in system_names:
            spec = all_systems.get(name)
            if spec is None:
                continue
            cooldown = 3 if name.startswith("bot_") else 1
            stop_override = spec["stop_pct"] if "stop_pct" in spec.columns else None
            trades = engine.simulate(
                df, spec["entry"], atr_series,
                cooldown_bars=cooldown, stop_pct_series=stop_override, same_bar_rule=same_bar_rule, exit_mode=exit_mode,
            )
            if not trades.empty:
                trades["symbol"] = symbol
                trades["system"] = name
                collected[name].append(trades)

            count = int(spec["entry"].fillna(False).sum())
            if count > 0:
                for seed in RANDOM_SEEDS:
                    rnd_entries = engine.random_entries_like(df, count, seed=seed, warmup=WARMUP_BARS)
                    rnd_trades = engine.simulate(
                        df, rnd_entries, atr_series,
                        cooldown_bars=cooldown, stop_pct_series=stop_override, same_bar_rule=same_bar_rule, exit_mode=exit_mode,
                    )
                    if not rnd_trades.empty:
                        rnd_trades["symbol"] = symbol
                        rnd_trades["system"] = name
                        collected_random[name][seed].append(rnd_trades)

        if idx % progress_every == 0:
            print(f"  processed {idx}/{len(symbols)} symbols", file=sys.stderr, flush=True)

    system_trades = {
        name: (pd.concat(collected[name], ignore_index=True) if collected[name] else pd.DataFrame())
        for name in system_names
    }
    random_trades = {
        name: {
            seed: (pd.concat(rows, ignore_index=True) if rows else pd.DataFrame())
            for seed, rows in per_seed.items()
        }
        for name, per_seed in collected_random.items()
    }
    return {"systems": system_trades, "random": random_trades}


def average_summaries(summaries: list[dict], label: str) -> dict:
    keys = ["trades", "win_rate", "avg_net_pct", "total_net_pct", "avg_win_pct", "avg_loss_pct",
            "profit_factor", "max_dd_pct", "avg_bars_held", "t_stat", "stop_avg_pct"]
    rows = [s for s in summaries if s.get("trades", 0) > 0]
    if not rows:
        return {k: 0 for k in keys} | {"system": label, "kind": "random_control", "trades": 0}
    out = {"system": label, "kind": "random_control"}
    for key in keys:
        out[key] = round(float(np.mean([r.get(key, 0) for r in rows])), 4)
    out["seeds"] = len(rows)
    return out


def buy_and_hold(data: dict[str, pd.DataFrame]) -> dict:
    returns = []
    for symbol, df in data.items():
        if len(df) < WARMUP_BARS + 10:
            continue
        start = float(df["close"].iloc[WARMUP_BARS])
        end = float(df["close"].iloc[-1])
        if start <= 0:
            continue
        returns.append((end / start - 1.0) * 100.0 - 2 * engine.FEE_PER_SIDE_PCT)
    if not returns:
        return {"system": "buy_and_hold", "trades": 0}
    arr = np.array(returns)
    return {
        "system": "buy_and_hold",
        "trades": len(arr),
        "win_rate": round(float((arr > 0).mean() * 100), 2),
        "avg_net_pct": round(float(arr.mean()), 4),
        "total_net_pct": round(float(arr.sum()), 2),
        "avg_win_pct": round(float(arr[arr > 0].mean()), 4) if (arr > 0).any() else 0.0,
        "avg_loss_pct": round(float(arr[arr <= 0].mean()), 4) if (arr <= 0).any() else 0.0,
        "profit_factor": 0.0,
        "max_dd_pct": 0.0,
        "avg_bars_held": 0.0,
        "t_stat": round(float(arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr)))), 2) if len(arr) > 2 and arr.std(ddof=1) > 0 else 0.0,
        "stop_avg_pct": 0.0,
        "open_trades": 0,
        "kind": "baseline",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--bars", type=int, default=2000)
    parser.add_argument("--out", default="reports/lab_1h")
    parser.add_argument("--limit-symbols", type=int, default=0)
    parser.add_argument("--same-bar-rule", default="stop_first", choices=["stop_first", "target_first", "skip"])
    parser.add_argument("--exit-mode", default="intrabar", choices=["intrabar", "close"])
    args = parser.parse_args()

    print(f"Loading universe {args.interval} bars={args.bars}", file=sys.stderr)
    data = load_universe(interval=args.interval, bars=args.bars)
    if args.limit_symbols:
        data = dict(sorted(data.items())[: args.limit_symbols])
    print(f"Loaded {len(data)} symbols", file=sys.stderr)

    sample_df = next(iter(data.values()))
    system_names = list(systems.build_all_systems(sample_df).keys())
    print(f"Systems: {system_names}", file=sys.stderr)

    result = run_universe(data, system_names, same_bar_rule=args.same_bar_rule, exit_mode=args.exit_mode)
    system_trades = result["systems"]
    random_trades = result["random"]

    rows = []
    random_rows = []
    for name in system_names:
        summary = engine.summarize(system_trades[name], name)
        summary["kind"] = "system"
        rows.append(summary)
        random_rows.append(average_summaries(
            [engine.summarize(random_trades[name][seed], f"{name}__random") for seed in RANDOM_SEEDS],
            f"{name}__random_control",
        ))

    rows.extend(random_rows)
    rows.append(buy_and_hold(data))

    metrics = pd.DataFrame(rows)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(out_dir / "metrics.csv", index=False)
    for name in system_names:
        if not system_trades[name].empty:
            system_trades[name].to_csv(out_dir / f"trades_{name}.csv", index=False)

    first_symbol = next(iter(data))
    meta = {
        "interval": args.interval,
        "bars": args.bars,
        "symbols_loaded": len(data),
        "same_bar_rule": args.same_bar_rule,
        "exit_mode": args.exit_mode,
        "systems": system_names,
        "rules": {
            "target_pct": engine.TARGET_PCT,
            "atr_stop_mult": engine.ATR_STOP_MULT,
            "min_stop_pct": engine.MIN_STOP_PCT,
            "max_stop_pct": engine.MAX_STOP_PCT,
            "fee_per_side_pct": engine.FEE_PER_SIDE_PCT,
            "warmup_bars": WARMUP_BARS,
        },
        "period": {
            "first_bar_open_time": int(data[first_symbol]["open_time"].iloc[0]),
            "last_bar_open_time": int(data[first_symbol]["open_time"].iloc[-1]),
        },
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(metrics.to_string(index=False))
    print(f"\nSaved to {out_dir}")


if __name__ == "__main__":
    main()
