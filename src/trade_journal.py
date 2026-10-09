"""دفتر الصفقات CSV — مخطط مخصّص لمؤشر Target Trend."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

TRADE_LOG_FILE = "trades_log.csv"
FIELDNAMES = [
    "trade_id",
    "symbol",
    "entry_time_ms",
    "entry_time_local",
    "entry_date_local",
    "entry_weekday_ar",
    "entry_hour_local",
    "entry_price",
    "stop_price",
    "target1_price",
    "target2_price",
    "target3_price",
    "atr_at_entry",
    "trend_length",
    "hit_target1",
    "hit_target2",
    "hit_target3",
    "targets_hit_count",
    "outcome",
    "exit_reason",
    "exit_time_ms",
    "exit_time_local",
    "exit_price",
    "duration_minutes",
    "duration_text",
    "gross_return_pct",
    "net_return_pct",
]


def _log_path(data_dir: str) -> Path:
    return Path(data_dir) / TRADE_LOG_FILE


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


def append_trade_entry(data_dir: str, record: dict[str, Any]) -> bool:
    path = _log_path(data_dir)
    _ensure_file(path)

    rows = _read_rows(path)
    trade_id = str(record.get("trade_id") or "")
    if trade_id and any((row.get("trade_id") or "") == trade_id for row in rows):
        return False

    payload = {field: record.get(field, "") for field in FIELDNAMES}
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writerow(payload)
    return True


def update_trade_exit(data_dir: str, trade_id: str, updates: dict[str, Any]) -> bool:
    path = _log_path(data_dir)
    if not path.exists():
        return False

    rows = _read_rows(path)
    changed = False
    for row in rows:
        if (row.get("trade_id") or "") != trade_id:
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
