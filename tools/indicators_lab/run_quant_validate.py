"""الاختبار الحاسم: هل منطق الارتداد يضيف شيئًا فوق الحافة الزمنية، أم أن الحافة كلها من الساعات؟

المنهج (مقارنة تفاح بتفاح):
- كل الأنظمة تُقيَّد بنفس الساعات الجيدة.
- خط الأساس = نبضة كل شمعة داخل نفس الساعات (لا منطق).
- إذا لم يتفوّق نظام الارتداد على الخط الأساس → فالمنطق لا يضيف شيئًا.

ويشمل: تقسيم رباعي (اتساق زمني) + نسبة العملات التي تفوّقت.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from . import engine, systems  # noqa: E402
from .data import load_universe  # noqa: E402
from .quant_systems import GOOD_HOURS, build_quant_systems  # noqa: E402

TARGET = 2.0
STOP_MULT = 1.5
NAMES = {
    "base_all_bars_good": None,           # نبضة كل شمعة (خط الأساس)
    "qd_d1h1_pullback": "qd_d1h1_pullback",
    "qd_pullback_rsi2": "qd_pullback_rsi2",
    "qd_rsi2_extreme": "qd_rsi2_extreme",
    "qd_bb_lower_revert": "qd_bb_lower_revert",
}


def main() -> None:
    data = load_universe(interval="1h", bars=2000)
    print(f"عملات: {len(data)}", file=sys.stderr)

    collected: dict[str, list[pd.DataFrame]] = {n: [] for n in NAMES}
    for i, symbol in enumerate(sorted(data), 1):
        df = data[symbol]
        if len(df) < 260:
            continue
        try:
            quant = build_quant_systems(df)
            atr_series = systems.ind.atr(df, 14)
        except Exception:  # noqa: BLE001
            continue
        hour = ((df["open_time"].astype("int64") + 3 * 3_600_000) // 3_600_000 % 24)
        good = hour.isin(GOOD_HOURS)
        for name, qname in NAMES.items():
            if qname is None:
                entry = good
            else:
                spec = quant.get(qname)
                if spec is None:
                    continue
                entry = spec["entry"].fillna(False) & good
            if int(entry.sum()) == 0:
                continue
            trades = engine.simulate(df, entry, atr_series, cooldown_bars=1, target_pct=TARGET, atr_stop_mult=STOP_MULT)
            if trades.empty:
                continue
            trades["symbol"] = symbol
            collected[name].append(trades)
        if i % 100 == 0:
            print(f"  {i}/{len(data)}", file=sys.stderr)

    print(f"\n{'=' * 100}\nهل يتفوّق منطق الارتداد على«نبضة داخل الساعات الجيدة»؟ (هدف {TARGET}% • وقف {STOP_MULT}×ATR)\n{'=' * 100}")
    frames = {}
    for name, chunks in collected.items():
        if not chunks:
            continue
        df = pd.concat(chunks, ignore_index=True)
        frames[name] = df
        net = df["net_pct"].to_numpy(dtype=float)
        mean, std = net.mean(), net.std(ddof=1)
        t = mean / (std / np.sqrt(len(net)))
        ts = df["entry_time"].to_numpy(dtype=np.int64)
        q = np.quantile(ts, [0.25, 0.5, 0.75])
        quarters = [net[ts <= q[0]], net[(ts > q[0]) & (ts <= q[1])], net[(ts > q[1]) & (ts <= q[2])], net[ts > q[2]]]
        q_txt = " | ".join(f"{x.mean():+.3f}" for x in quarters)
        per_symbol = df.groupby("symbol")["net_pct"].mean()
        print(f"{name:<22} n={len(net):>7} | نجاح {100 * (net > 0).mean():5.1f}% | متوسط {mean:+.4f} | t={t:+6.2f}")
        print(f"{'':<22} الأرباع: {q_txt} | عملات رابحة: {100 * (per_symbol > 0).mean():.1f}% من {len(per_symbol)}")

    # المقارنة المباشرة
    if "base_all_bars_good" in frames:
        base = frames["base_all_bars_good"].groupby("symbol")["net_pct"].mean()
        print(f"\n{'=' * 100}\nالفرق مقابل خط الأساس الزمني (لكل عملة)\n{'=' * 100}")
        for name, df in frames.items():
            if name == "base_all_bars_good":
                continue
            per_symbol = df.groupby("symbol")["net_pct"].agg(["mean", "count"])
            per_symbol = per_symbol[per_symbol["count"] >= 5]
            common = per_symbol.index.intersection(base.index)
            diff = (per_symbol.loc[common, "mean"] - base.loc[common]).to_numpy(dtype=float)
            if len(diff) < 5:
                continue
            t = diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff)))
            verdict = "✅ يتفوّق" if diff.mean() > 0 and t > 2 else ("❌ أدنى" if diff.mean() < 0 else "≈ محايد")
            print(f"{name:<22} فرق {diff.mean():+.4f}% | أفضل من الأساس في {100 * (diff > 0).mean():.1f}% من العملات | t={t:+5.2f} → {verdict}")

    # حفظ
    out = Path("reports/quant_validate")
    out.mkdir(parents=True, exist_ok=True)
    for name, df in frames.items():
        df.to_csv(out / f"{name}.csv.gz", index=False, compression="gzip")
    print(f"\nمحفوظ في {out}/")


if __name__ == "__main__":
    main()
