"""دفتر صفقات ZigZag++ (1H) — CSV منفصل تمامًا عن بقية الأنظمة.

⚠️ العزل: الملف `trades_log_zigzag.csv` لا يُقرأ ولا يُكتب أي دفتر آخر
   (`trades_log.csv` لـ Target Trend، `trades_log_qaa.csv` لنظام القاع،
   `trades_log_sa.csv` للسوق السعودي).
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

TRADE_LOG_FILE = "trades_log_zigzag.csv"
FIELDNAMES = [
    "trade_id",
    "symbol",
    "strategy",
    "entry_time_ms",
    "entry_time_local",
    "entry_bar_open_time_ms",
    "entry_bar_open_time_local",
    "entry_price",
    "target_price",
    "stop_price",
    "bottom_bar_open_time_ms",
    "bottom_bar_open_time_local",
    "bottom_low",
    "halal_verdict",
    "max_price",
    "max_gain_pct",
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


def append_zigzag_entry(data_dir: str, record: dict[str, Any]) -> bool:
    """يضيف صفقة جديدة — لا يكرر trade_id (دورة كل 5 دقائق)."""
    path = _log_path(data_dir)
    _ensure_file(path)
    rows = _read_rows(path)
    if any(row.get("trade_id") == record.get("trade_id") for row in rows):
        return False
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writerow({key: record.get(key, "") for key in FIELDNAMES})
    return True


def load_zigzag_rows(data_dir: str) -> list[dict[str, str]]:
    """يقرأ كل صفوف دفتر ZigZag (قراءة فقط — لا يلمس أي دفتر آخر)."""
    return _read_rows(_log_path(data_dir))


def update_zigzag_exit(data_dir: str, trade_id: str, updates: dict[str, Any]) -> bool:
    """يحدّث صف الصفقة عند الإغلاق — يعيد كتابة الملف بالمخطط نفسه."""
    path = _log_path(data_dir)
    _ensure_file(path)
    rows = _read_rows(path)
    changed = False
    for row in rows:
        if row.get("trade_id") == trade_id:
            # حماية صارمة: مفتاح غير معرَّف لا يُسقط الكتابة كلها
            row.update({key: value for key, value in updates.items() if key in FIELDNAMES})
            changed = True
    if not changed:
        return False
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    return True
