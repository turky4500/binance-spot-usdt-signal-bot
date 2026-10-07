from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

SHADOW_LOG_FILE = "rejected_candidates.csv"
FIELDNAMES = [
    "candidate_id",
    "symbol",
    "rejected_time_ms",
    "rejected_time_local",
    "rejected_date_local",
    "entry_bar_open_time",
    "reject_reason",
    "reject_reason_ar",
    "entry_price",
    "target_price",
    "stop_price",
    "strong_signal",
    "buy_score",
    "rsi",
    "stoch",
    "adx",
    "plus_di",
    "minus_di",
    "relative_volume",
    "reward_risk_ratio",
    "buy_risk_pct",
    "distance_from_ema200_pct",
    "outcome",
    "exit_reason",
    "exit_time_ms",
    "exit_time_local",
    "exit_price",
    "duration_minutes",
    "resolution_method",
]


def _log_path(data_dir: str) -> Path:
    return Path(data_dir) / SHADOW_LOG_FILE


def _ensure_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_shadow_candidate(data_dir: str, record: dict[str, Any]) -> bool:
    path = _log_path(data_dir)
    _ensure_file(path)

    rows = _read_rows(path)
    candidate_id = str(record.get("candidate_id") or "")
    if candidate_id and any((row.get("candidate_id") or "") == candidate_id for row in rows):
        return False

    payload = {field: record.get(field, "") for field in FIELDNAMES}
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writerow(payload)
    return True


def resolve_shadow_candidate(data_dir: str, candidate_id: str, updates: dict[str, Any]) -> bool:
    path = _log_path(data_dir)
    if not path.exists():
        return False

    rows = _read_rows(path)
    changed = False
    for row in rows:
        if (row.get("candidate_id") or "") != candidate_id:
            continue
        for key, value in updates.items():
            if key in FIELDNAMES:
                row[key] = "" if value is None else str(value)
        changed = True
        break

    if not changed:
        return False

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    return True
