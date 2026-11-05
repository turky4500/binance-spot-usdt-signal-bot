"""حالة NW: defaults، كتابة ذرية، وعزل المسار."""

from __future__ import annotations

import json

from src.nw_state import DEFAULT_STATE, NWStateStore, _cap, symbol_state


def test_defaults_are_isolated_and_marked(tmp_path):
    store = NWStateStore(str(tmp_path / "data" / "state_nw.json"))
    state = store.load()
    assert state["system"] == "nw-1h"
    assert state["symbols"] == {} and state["sent"] == []
    assert state["cycles"] == 0
    # لا يتشارك المرجع مع الثابت
    state["sent"].append("x")
    assert DEFAULT_STATE["sent"] == []


def test_roundtrip_arabic_and_atomic_write(tmp_path):
    path = tmp_path / "state.json"
    store = NWStateStore(str(path))
    state = store.load()
    state["symbols"]["BTCUSDT"] = {"position": {"exit_reason": "علامة حمراء"}}
    store.save(state)
    assert json.loads(path.read_text(encoding="utf-8"))["symbols"]["BTCUSDT"]["position"]["exit_reason"] == "علامة حمراء"
    assert not list(tmp_path.glob("*.tmp"))
    assert store.load()["symbols"]["BTCUSDT"]["position"]["exit_reason"] == "علامة حمراء"


def test_corrupt_state_falls_back_to_defaults(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ invalid", encoding="utf-8")
    assert NWStateStore(str(path)).load()["system"] == "nw-1h"


def test_cap_dedupes_and_limits():
    assert _cap([1, 1, 2, 3], limit=3) == [1, 2, 3]
    assert _cap(list(range(10)), limit=3) == [7, 8, 9]


def test_symbol_state_defaults():
    state = {"symbols": {}}
    item = symbol_state(state, "ETHUSDT")
    assert item["position"] is None and item["entry_count"] == 0
    assert state["symbols"]["ETHUSDT"] is item
