"""حالة نظام NW 1H — معزولة تمامًا في data/state_nw.json.

لا يقرأ ولا يكتب أي ملف حالة للأنظمة الخمسة الأخرى. الكتابة ذرية (tmp + replace)
مثل src/state.py، حتى لا تُترك حالة ناقصة إن انقطعت المهمة.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_STATE: dict[str, Any] = {
    "version": 1,
    "system": "nw-1h",
    "symbols": {},          # sym → {position, entry_count, last_buy_bar, last_entry_price}
    "prev_buy_bars": {},    # sym → أزمنة شموع ▲ التي كانت ظاهرة في الدورة السابقة
    "prev_sell_bars": {},   # sym → أزمنة شموع ▼ التي كانت ظاهرة في الدورة السابقة
    "buy_history": {},      # sym → أزمنة شموع ▲ التي أُعلنت (لرصد عودة الظهور)
    "sent": [],             # مفاتيح الرسائل المرسلة (منع التكرار عند تكرار التشغيل)
    "cycles": 0,
    "messages_sent": 0,
    "last_cycle_ms": 0,
}


class NWStateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        state = json.loads(json.dumps(DEFAULT_STATE))   # نسخة عميقة بلا مشاركة مرجعية
        if not self.path.exists():
            return state
        try:
            with self.path.open("r", encoding="utf-8") as f:
                state.update(json.load(f))
        except (OSError, ValueError):
            return state
        return state

    def save(self, state: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)


def symbol_state(state: dict[str, Any], sym: str) -> dict[str, Any]:
    """حالة زوج واحد مع قيم افتراضية — تُستخدم داخليًا فقط."""
    item = state["symbols"].get(sym)
    if not isinstance(item, dict):
        item = {"position": None, "entry_count": 0, "last_buy_bar": 0, "last_entry_price": 0.0}
        state["symbols"][sym] = item
    return item


def _cap(items: list, limit: int = 60) -> list:
    return list(dict.fromkeys(items))[-limit:]
