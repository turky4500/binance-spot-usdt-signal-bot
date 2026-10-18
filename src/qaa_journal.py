"""دفتر صفقات نظام القاع CSV — ملف منفصل تمامًا عن دفتر Target Trend.

⚠️ العزل: TRADE_LOG_FILE هنا هو trades_log_qaa.csv — لا يُقرأ ولا يُكتب
   دفتر النظام الآخر (trades_log.csv) أبدًا.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

TRADE_LOG_FILE = "trades_log_qaa.csv"
FIELDNAMES = [
    "trade_id",
    "symbol",
    "strategy",
    "entry_time_ms",
    "entry_time_local",
    "entry_date_local",
    "entry_price",
    "pivot_stop",
    "entry_stop_dist_pct",
    "size_suggest_pct",
    "target_price",
    "peak_price",
    "peak_gain_pct",
    "stop_updates_count",
    "halal_verdict",
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
    if not path.exists():
        with path.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()


def _read_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except (OSError, csv.Error):
        return []


def append_qaa_entry(data_dir: str, record: dict[str, Any]) -> bool:
    """يضيف صفقة جديدة — متجاهلًا إذا كان trade_id موجودًا (دورة كل 10 دقائق لا تكرر الصف)."""
    path = _log_path(data_dir)
    _ensure_file(path)
    rows = _read_rows(path)
    if any(row.get("trade_id") == record.get("trade_id") for row in rows):
        return False
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writerow({key: record.get(key, "") for key in FIELDNAMES})
    return True


def update_qaa_exit(data_dir: str, trade_id: str, updates: dict[str, Any]) -> bool:
    """يحدّث صف الصفقة عند الإغلاق — يعيد كتابة الملف بالمخطط نفسه."""
    path = _log_path(data_dir)
    _ensure_file(path)
    rows = _read_rows(path)
    changed = False
    for row in rows:
        if row.get("trade_id") == trade_id:
            # حماية صارمة: مفتاح غير معرَّف لا يُسقط الدورة كلها (تصحيح حادثة peak_gain_pct)
            row.update({key: value for key, value in updates.items() if key in FIELDNAMES})
            changed = True
    if not changed:
        return False
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    return True
