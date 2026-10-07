"""تجربة ثانية: نفس الأنظمة لكن بقواعد خروج "تتبع الاتجاه" الأصلية لها (بلا هدف ثابت).

الهدف: اختبار فرضية أن الخروج الثابت (+2% هدف) هو ما يقتل الأنظمة الاتجاهية،
وليس الإشارات نفسها. هنا نخرج فقط عندما ينقلب الاتجاه، ونترك الربح يكبر.
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

from . import indicators as ind  # noqa: E402
from .data import load_universe  # noqa: E402

FEE_PER_SIDE_PCT = 0.1
WARMUP_BARS = 210


def simulate_trailing(df: pd.DataFrame, entries: pd.Series, exit_series: pd.Series, atr_series: pd.Series | None = None, atr_trail_mult: float = 2.0) -> pd.DataFrame:
    """دخول عند إغلاق شمعة الإشارة، خروج عند إغلاق أول شمعة يتحقق فيها شرط الخروج.

    exit_series: سلسلة منطقية (نعم = اخرج الآن بإغلاق هذه الشمعة)
    إن مُرّرت atr_series مع exit_series=None يُستخدم وقف متحرك بمسافة atr_trail_mult×ATR من أعلى سعر.
    """
    close = df["close"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    open_ms = df["open_time"].to_numpy(dtype=np.int64)
    sig = entries.fillna(False).to_numpy(dtype=bool)
    exit_sig = exit_series.fillna(False).to_numpy(dtype=bool) if exit_series is not None else None
    atr_v = atr_series.to_numpy(dtype=float) if atr_series is not None else None
    n = len(df)

    trades: list[dict] = []
    i = 0
    while i < n - 1:
        if not sig[i]:
            i += 1
            continue
        entry = close[i]
        peak = entry
        exit_index = None
        exit_price = None
        for j in range(i + 1, n):
            peak = max(peak, high[j])
            if exit_sig is not None and exit_sig[j]:
                exit_index, exit_price = j, close[j]
                break
            if exit_sig is None and atr_v is not None and not np.isnan(atr_v[j]):
                trail = peak - atr_trail_mult * atr_v[j]
                if close[j] <= trail:
                    exit_index, exit_price = j, close[j]
                    break
        if exit_index is None:
            exit_index, exit_price = n - 1, close[n - 1]
        gross = (exit_price / entry - 1.0) * 100.0
        trades.append({
            "entry_index": i,
            "entry_time": int(open_ms[i]),
            "exit_time": int(open_ms[exit_index]),
            "entry_price": entry,
            "exit_price": float(exit_price),
            "outcome": "win" if gross > 0 else "loss",
            "gross_pct": gross,
            "net_pct": gross - 2 * FEE_PER_SIDE_PCT,
            "bars_held": exit_index - i,
        })
        i = exit_index + 1

    return pd.DataFrame(trades)


def summarize_trailing(trades: pd.DataFrame, label: str) -> dict:
    if trades is None or trades.empty:
        return {"system": label, "trades": 0}
    net = trades["net_pct"].to_numpy(dtype=float)
    wins = net[net > 0]
    losses = net[net <= 0]
    equity = np.cumsum(net)
    running_max = np.maximum.accumulate(equity)
    return {
        "system": label,
        "trades": int(len(net)),
        "win_rate": round(float((net > 0).mean() * 100), 2),
        "avg_net_pct": round(float(net.mean()), 4),
        "total_net_pct": round(float(net.sum()), 2),
        "avg_win_pct": round(float(wins.mean()), 4) if len(wins) else 0.0,
        "avg_loss_pct": round(float(losses.mean()), 4) if len(losses) else 0.0,
        "best_trade_pct": round(float(net.max()), 2),
        "worst_trade_pct": round(float(net.min()), 2),
        "profit_factor": round(float(wins.sum() / -losses.sum()), 3) if len(losses) and losses.sum() < 0 else 999.0,
        "max_dd_pct": round(float(np.max(running_max - equity)), 2),
        "avg_bars_held": round(float(trades["bars_held"].mean()), 2),
        "t_stat": round(float(net.mean() / (net.std(ddof=1) / np.sqrt(len(net)))), 2) if len(net) > 2 and net.std(ddof=1) > 0 else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--bars", type=int, default=2000)
    parser.add_argument("--out", default="reports/lab_1h_trailing")
    args = parser.parse_args()

    data = load_universe(interval=args.interval, bars=args.bars)
    print(f"Loaded {len(data)} symbols", file=sys.stderr)

    settings = StrategySettings()
    collected: dict[str, list[pd.DataFrame]] = {}

    for count, (symbol, df) in enumerate(sorted(data.items()), 1):
        if len(df) < WARMUP_BARS + 50:
            continue
        atr_series = ind.atr(df, 14)

        st3 = ind.supertrend(df, 10, 3.0)
        st2 = ind.supertrend(df, 10, 2.0)
        ut = ind.ut_bot(df, 10, 1.0)
        imp = ind.impulse_macd(df)
        wt = ind.wavetrend(df)
        frame = prepare_strategy_frame(df, settings)

        variants = {
            "supertrend_10_3_trail": (st3["st_buy"], (st3["st_dir"] == -1) & (st3["st_dir"].shift(1) == 1), None),
            "supertrend_10_2_trail": (st2["st_buy"], (st2["st_dir"] == -1) & (st2["st_dir"].shift(1) == 1), None),
            "ut_bot_trail": (ut["ut_buy"], None, atr_series),
            "impulse_macd_trail": (imp["imp_buy"], None, atr_series),
            "bot_logic_atr_trail": (frame["buy_setup"].fillna(False), None, atr_series),
        }
        for name, (entries, exits, atr_ref) in variants.items():
            trades = simulate_trailing(df, entries, exits, atr_ref, atr_trail_mult=2.0)
            if not trades.empty:
                trades["symbol"] = symbol
                collected.setdefault(name, []).append(trades)

        if count % 100 == 0:
            print(f"  processed {count}/{len(data)}", file=sys.stderr, flush=True)

    rows = []
    for name, chunks in collected.items():
        trades = pd.concat(chunks, ignore_index=True)
        rows.append(summarize_trailing(trades, name))
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        trades.to_csv(out_dir / f"trades_{name}.csv", index=False)

    metrics = pd.DataFrame(rows).sort_values("avg_net_pct", ascending=False)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(out_dir / "metrics.csv", index=False)
    (out_dir / "meta.json").write_text(json.dumps({
        "interval": args.interval, "bars": args.bars, "symbols": len(data),
        "exit_rule": "trailing: indicator flip or 2xATR trail from peak; no fixed target",
        "fee_per_side_pct": FEE_PER_SIDE_PCT, "warmup_bars": WARMUP_BARS,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(metrics.to_string(index=False))
    print(f"\nSaved to {out_dir}")


if __name__ == "__main__":
    main()
