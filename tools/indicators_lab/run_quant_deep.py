"""استكشاف عميق لأفضل عائلة أنظمة: الارتداد للمتوسط.

السؤال: هل يمكن تحويل أنظمة الارتداد من «أقل سوءًا» إلى «موجبة فعلًا»؟
المتغيّرات المُختبرة (لكل نظام): هدف {0.75, 1.0, 1.5, 2.0}% • وقف ATR {0.75, 1.0, 1.5} • فلتر الساعات الجيدة.

القاعدة: لا نعتمد أي تركيبة قبل أن تصمد في النصف الثاني (خارج العينة).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from . import engine  # noqa: E402
from .data import load_universe  # noqa: E402
from .quant_systems import GOOD_HOURS, build_quant_systems  # noqa: E402

CANDIDATE_SYSTEMS = [
    "qd_d1h1_pullback", "qd_d1h1_deep_pullback", "qd_pullback_rsi", "qd_pullback_stoch",
    "qd_pullback_rsi2", "qd_rsi2_extreme",
]
TARGETS = [1.5, 2.0]
STOP_MULTS = [1.0, 1.5]
HOUR_MODES = ["all", "good"]


def measure(net: np.ndarray) -> dict:
    n = len(net)
    if n == 0:
        return {"n": 0, "avg": float("nan"), "win": float("nan"), "t": float("nan")}
    mean, std = float(net.mean()), (float(net.std(ddof=1)) if n > 1 else 0.0)
    return {
        "n": n, "avg": mean, "win": float((net > 0).mean() * 100.0),
        "t": mean / (std / np.sqrt(n)) if std > 0 else float("nan"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=2000)
    parser.add_argument("--symbols-limit", type=int, default=150)
    parser.add_argument("--out", default="reports/quant_deep")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    data = load_universe(interval="1h", bars=args.bars)
    data = dict(sorted(data.items())[: args.symbols_limit] if args.symbols_limit else data)
    print(f"عملات: {len(data)}", file=sys.stderr)

    rows = []
    symbols = sorted(data.keys())
    for idx, symbol in enumerate(symbols, 1):
        df = data[symbol]
        if df["close"].isna().all() or len(df) < 260:
            continue
        try:
            from . import systems as base_systems
            quant = build_quant_systems(df)
            atr_series = base_systems.ind.atr(df, 14)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {symbol}: {exc}", file=sys.stderr)
            continue

        hour = ((df["open_time"].astype("int64") + 3 * 3_600_000) // 3_600_000 % 24)

        for name in CANDIDATE_SYSTEMS:
            spec = quant.get(name)
            if spec is None:
                continue
            base_entry = spec["entry"].fillna(False)
            exit_signal = spec.get("exit_signal")
            for target in TARGETS:
                for mult in STOP_MULTS:
                    for hmode in HOUR_MODES:
                        entry = base_entry if hmode == "all" else (base_entry & hour.isin(GOOD_HOURS))
                        if int(entry.sum()) == 0:
                            continue
                        trades = engine.simulate(
                            df, entry, atr_series, cooldown_bars=1,
                            exit_mode="intrabar", target_pct=target, atr_stop_mult=mult,
                        )
                        if trades.empty:
                            continue
                        trades["symbol"] = symbol
                        rows.append({
                            "system": name, "target": target, "stop_mult": mult, "hours": hmode,
                            "n": len(trades),
                            "net_sum": float(trades["net_pct"].sum()),
                            "net_mean": float(trades["net_pct"].mean()),
                            "wins": int((trades["net_pct"] > 0).sum()),
                            "entry_ts": trades["entry_time"].to_numpy(dtype=np.int64),
                            "net": trades["net_pct"].to_numpy(dtype=float),
                        })
        if idx % 25 == 0:
            print(f"  {idx}/{len(symbols)}", file=sys.stderr)

    # تجميع لكل تركيبة
    summary = []
    keys = {(r["system"], r["target"], r["stop_mult"], r["hours"]) for r in rows}
    for key in sorted(keys):
        sub = [r for r in rows if (r["system"], r["target"], r["stop_mult"], r["hours"]) == key]
        net = np.concatenate([r["net"] for r in sub])
        ts = np.concatenate([r["entry_ts"] for r in sub])
        st = measure(net)
        # خارج العينة: النصف الثاني زمنيًا
        mid = int(np.median(ts))
        m2 = measure(net[ts > mid])
        m1 = measure(net[ts <= mid])
        summary.append({
            "system": key[0], "target": key[1], "stop_mult": key[2], "hours": key[3],
            "n": st["n"], "win": st["win"], "avg": st["avg"], "t": st["t"],
            "train_avg": m1["avg"], "test_avg": m2["avg"], "test_t": m2["t"], "test_n": m2["n"],
            "symbols": len(sub),
        })

    df_summary = pd.DataFrame(summary).sort_values("avg", ascending=False)
    df_summary.to_csv(out_dir / "deep_grid.csv", index=False)

    with pd.option_context("display.width", 220, "display.max_columns", 40, "display.max_rows", 200):
        print(df_summary.head(25).to_string(index=False, float_format=lambda v: f"{v:8.3f}"))
    print(f"\nأفضل 25 من {len(df_summary)} تركيبة — محفوظة في {out_dir}/deep_grid.csv")


if __name__ == "__main__":
    main()
