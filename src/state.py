from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _default_state() -> dict[str, Any]:
    return {
        "last_processed_open_time": 0,
        "last_entry_bar_time": {},
        "last_exit_bar_time": {},
        "open_trades": {},
        "event_log": [],
        "daily_report": {
            "last_reported_for_date": ""
        },
        "daily_analysis": {
            "last_reported_for_date": ""
        },
        "weekly_report": {
            "last_reported_week_start": ""
        },
        "last_rejected_bar_time": {},
        "shadow_candidates": {},
    }


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _default_state()
        with self.path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        state = _default_state()
        state.update(data)
        return state

    def save(self, state: dict[str, Any]) -> None:
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        temp_path.replace(self.path)
