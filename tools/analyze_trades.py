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


def main() -> None:
    if not DATA_FILE.exists():
        raise SystemExit("No data/trades_log.csv found yet.")

    with DATA_FILE.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    closed = [r for r in rows if r.get("outcome") in {"target", "stop"}]
    wins = [r for r in closed if r.get("outcome") == "target"]
    losses = [r for r in closed if r.get("outcome") == "stop"]

    print("# Trade Analysis Snapshot")
    print()
    print(f"- Total rows: {len(rows)}")
    print(f"- Closed trades: {len(closed)}")
    print(f"- Wins: {len(wins)}")
    print(f"- Losses: {len(losses)}")
    print(f"- Open trades: {len(rows) - len(closed)}")
    if closed:
        print(f"- Success rate: {len(wins) / len(closed) * 100:.1f}%")
    print()

    print("## Average metrics for wins")
    for key in ["buy_score", "rsi", "stoch", "adx", "relative_volume", "reward_risk_ratio", "distance_from_ema200_pct", "duration_minutes"]:
        val = _avg(wins, key)
        print(f"- {key}: {val:.3f}" if val is not None else f"- {key}: —")
    print(f"- top symbols: {_top_symbols(wins)}")
    print()

    print("## Average metrics for losses")
    for key in ["buy_score", "rsi", "stoch", "adx", "relative_volume", "reward_risk_ratio", "distance_from_ema200_pct", "duration_minutes"]:
        val = _avg(losses, key)
        print(f"- {key}: {val:.3f}" if val is not None else f"- {key}: —")
    print(f"- top symbols: {_top_symbols(losses)}")


if __name__ == "__main__":
    main()
