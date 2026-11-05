"""دفتر صفقات نظام NW 1H — data/trades_log_nw.csv (مستقل عن كل الدفاتر الأخرى).

النظام يرسل **توصيات** لا ينفّذ صفقات؛ الدفتر توثيق إشارات فقط، ونتيجته
معلوماتية بحتة (لا يُبنى عليه أي ادعاء ربح).
"""

from __future__ import annotations

import csv
from pathlib import Path

COLUMNS = [
    "symbol",
    "entry_time",
    "entry_price",
    "exit_time",
    "exit_price",
    "gross_pct",
    "net_pct",
    "entries_count",
    "exit_reason",
    "signal_bar_time",
    "confirm_bar_time",
]


def append_trade(path: str, row: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new_file = not p.exists()
    with p.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in COLUMNS})


def read_trades(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))
