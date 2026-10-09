"""تحليل أولي لدفتر صفقات Target Trend — python tools/analyze_trades.py"""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

DATA_FILE = Path("data/trades_log.csv")


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _avg(rows: list[dict[str, str]], key: str) -> float | None:
    vals = [_to_float(r.get(key, "")) for r in rows]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _top_symbols(rows: list[dict[str, str]], limit: int = 5) -> str:
    counts = Counter(r.get("symbol", "") for r in rows if r.get("symbol"))
    if not counts:
        return "—"
    return " | ".join(f"{sym} ({cnt})" for sym, cnt in counts.most_common(limit))


def _target_counts(rows: list[dict[str, str]]) -> str:
    t1 = sum(1 for r in rows if r.get("hit_target1") == "1")
    t2 = sum(1 for r in rows if r.get("hit_target2") == "1")
    t3 = sum(1 for r in rows if r.get("hit_target3") == "1")
    return f"T1: {t1} | T2: {t2} | T3: {t3}"


def main() -> None:
    if not DATA_FILE.exists():
        raise SystemExit("No data/trades_log.csv found yet.")

    with DATA_FILE.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    closed = [r for r in rows if r.get("outcome") in {"win", "loss"}]
    wins = [r for r in closed if r.get("outcome") == "win"]
    losses = [r for r in closed if r.get("outcome") == "loss"]

    print("# Target Trend — Trade Analysis Snapshot")
    print()
    print(f"- Total rows: {len(rows)}")
    print(f"- Closed trades: {len(closed)}")
    print(f"- Wins: {len(wins)}")
    print(f"- Losses: {len(losses)}")
    print(f"- Open trades: {len(rows) - len(closed)}")
    if closed:
        print(f"- Success rate: {len(wins) / len(closed) * 100:.1f}%")
    print(f"- Exit reasons: {dict(Counter(r.get('exit_reason', '') for r in closed))}")
    print()

    for label, group in (("wins", wins), ("losses", losses)):
        print(f"## Average metrics for {label}")
        for key in ["net_return_pct", "gross_return_pct", "duration_minutes", "targets_hit_count", "atr_at_entry"]:
            val = _avg(group, key)
            print(f"- {key}: {val:.3f}" if val is not None else f"- {key}: —")
        print(f"- top symbols: {_top_symbols(group)}")
        print()

    print("## Targets hit")
    print(f"- closed trades: {_target_counts(closed)}")
    print(f"- all rows: {_target_counts(rows)}")


if __name__ == "__main__":
    main()
