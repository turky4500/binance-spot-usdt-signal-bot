from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.halal_refresh_gate import decide, load_json, primary_digest
from tools.update_halal_academy import build_merge, primary_digest as updater_digest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)


def pair(age: timedelta = timedelta(minutes=1)):
    primary = {"BTC": 0, "BARD": 2, "MITO": 1}
    last = NOW - age
    meta = {
        "last_check_utc": last.isoformat().replace("+00:00", "Z"),
        "primary_sha256": primary_digest(primary),
    }
    return primary, meta


@pytest.mark.parametrize(
    ("event", "age", "refresh", "reason"),
    [
        ("workflow_run", timedelta(seconds=0), False, "snapshot_fresh"),
        ("workflow_run", timedelta(minutes=29, seconds=59), False, "snapshot_fresh"),
        ("workflow_run", timedelta(minutes=30), True, "refresh_due"),
        ("schedule", timedelta(minutes=29), False, "snapshot_fresh"),
        ("schedule", timedelta(minutes=45), True, "refresh_due"),
        ("workflow_dispatch", timedelta(minutes=1), True, "manual_dispatch"),
        ("workflow_run", timedelta(seconds=-1), True, "last_check_in_future"),
    ],
)
def test_age_gate_and_manual_force(event, age, refresh, reason):
    primary, meta = pair(age)
    before = (copy.deepcopy(primary), copy.deepcopy(meta))
    d = decide(primary, meta, event_name=event, now=NOW)
    assert (d.refresh, d.reason) == (refresh, reason)
    assert (primary, meta) == before


@pytest.mark.parametrize("primary", [None, {}, [], {"BTC": True}, {"BTC": 7}, {"BTC": "0"}, {"": 0}])
def test_invalid_primary_attempts_refresh_without_fabricating_values(primary):
    d = decide(primary, {}, event_name="workflow_run", now=NOW)
    assert d.refresh and d.reason == "primary_missing_or_invalid"


@pytest.mark.parametrize("meta", [None, [], "bad"])
def test_missing_metadata_attempts_refresh(meta):
    primary, _ = pair()
    d = decide(primary, meta, event_name="schedule", now=NOW)
    assert d.refresh and d.reason == "metadata_missing_or_invalid"


def test_missing_or_wrong_digest_does_not_allow_skip():
    primary, meta = pair()
    for recorded in (None, "", "0" * 64, 42):
        meta["primary_sha256"] = recorded
        d = decide(primary, meta, event_name="workflow_run", now=NOW)
        assert d.refresh and d.reason == "digest_missing_or_mismatch"


@pytest.mark.parametrize("stamp", [None, "", "not-a-date", "2026-11-05T11:59:00", 123])
def test_bad_or_timezone_naive_last_check_attempts_refresh(stamp):
    primary, meta = pair()
    meta["last_check_utc"] = stamp
    d = decide(primary, meta, event_name="schedule", now=NOW)
    assert d.refresh and d.reason == "last_check_missing_or_invalid"


def test_time_offset_is_converted_to_utc():
    primary, meta = pair()
    meta["last_check_utc"] = "2026-11-05T14:31:00+03:00"  # 11:31 UTC
    assert not decide(primary, meta, event_name="schedule", now=NOW).refresh
    meta["last_check_utc"] = "2026-11-05T14:30:00+03:00"
    assert decide(primary, meta, event_name="schedule", now=NOW).refresh


def test_future_or_naive_clock_is_not_treated_as_fresh():
    primary, meta = pair(timedelta(seconds=-10))
    d = decide(primary, meta, event_name="schedule", now=NOW)
    assert d.refresh and d.reason == "last_check_in_future"
    with pytest.raises(ValueError, match="timezone-aware"):
        decide(primary, meta, event_name="schedule", now=NOW.replace(tzinfo=None))


def test_gate_digest_matches_existing_updater():
    primary = {"BTC": 0, "ETH": 1, "عملَة": 2, "AAA": 0}
    assert primary_digest(primary) == updater_digest(primary)


def test_current_pinned_primary_and_metadata_have_matching_digests():
    primary = load_json(ROOT / "data/daily_trend/halal_academy.json")
    meta = load_json(ROOT / "data/daily_trend/halal_academy_meta.json")
    assert meta["primary_sha256"] == primary_digest(primary) == updater_digest(primary)


def test_missing_or_partial_files_are_not_treated_as_fresh(tmp_path):
    primary_path = tmp_path / "primary.json"
    meta_path = tmp_path / "meta.json"
    primary_path.write_text('{"BTC":', encoding="utf-8")
    meta_path.write_text('{"last_check_utc":', encoding="utf-8")
    assert load_json(primary_path) is None
    d = decide(load_json(primary_path), load_json(meta_path), event_name="workflow_run", now=NOW)
    assert d.refresh
    assert primary_path.read_text(encoding="utf-8") == '{"BTC":'


def test_cli_produces_github_output_without_touching_input_files(tmp_path):
    primary = {"BTC": 0, "ETH": 2}
    meta = {
        "last_check_utc": datetime.now(timezone.utc).isoformat(),
        "primary_sha256": primary_digest(primary),
    }
    p, m, output = tmp_path / "primary.json", tmp_path / "meta.json", tmp_path / "out"
    p.write_text(json.dumps(primary), encoding="utf-8")
    m.write_text(json.dumps(meta), encoding="utf-8")
    snapshot = (p.read_bytes(), m.read_bytes())
    env = {**os.environ, "GITHUB_EVENT_NAME": "workflow_run", "GITHUB_OUTPUT": str(output)}
    cmd = [sys.executable, str(ROOT / "tools/halal_refresh_gate.py"), "--primary", str(p), "--metadata", str(m)]
    subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
    assert "refresh=false\n" in output.read_text(encoding="utf-8")
    assert (p.read_bytes(), m.read_bytes()) == snapshot
    output.write_text("", encoding="utf-8")
    env["GITHUB_EVENT_NAME"] = "workflow_dispatch"
    subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
    assert "refresh=true\n" in output.read_text(encoding="utf-8")
    assert (p.read_bytes(), m.read_bytes()) == snapshot


def test_workflow_keeps_schedule_and_manual_adds_main_heartbeat_and_gate():
    yaml = pytest.importorskip("yaml")
    path = ROOT / ".github/workflows/halal-academy-refresh.yml"
    workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    events = workflow["on"]
    assert events["schedule"] == [{"cron": "*/30 * * * *"}]
    assert "workflow_dispatch" in events
    assert events["workflow_run"] == {
        "workflows": ["Signal Bot Cron"], "types": ["completed"], "branches": ["main"]
    }
    assert workflow["concurrency"]["group"] == "halal-academy-refresh"
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    steps = workflow["jobs"]["refresh"]["steps"]
    due = next(step for step in steps if step.get("id") == "due")
    install = next(step for step in steps if step.get("name") == "Install requests")
    fetch = next(step for step in steps if step.get("name") == "Fetch site rulings and merge")
    assert due["run"] == "python tools/halal_refresh_gate.py"
    assert install["if"] == fetch["if"] == "steps.due.outputs.refresh == 'true'"
    assert fetch["run"] == "python tools/update_halal_academy.py"
    assert steps.index(due) < steps.index(install) < steps.index(fetch)


def test_queued_pulse_skips_after_first_successful_refresh():
    old_primary, stale_meta = pair(timedelta(hours=4))
    assert decide(old_primary, stale_meta, event_name="workflow_run", now=NOW).refresh
    fresh_meta = {"last_check_utc": NOW.isoformat(), "primary_sha256": primary_digest(old_primary)}
    later = decide(old_primary, fresh_meta, event_name="workflow_run", now=NOW + timedelta(minutes=5))
    assert not later.refresh and later.reason == "snapshot_fresh"


def test_failed_refresh_remains_due_at_next_pulse():
    primary, meta = pair(timedelta(hours=2))
    assert decide(primary, meta, event_name="workflow_run", now=NOW).refresh
    # A failed updater leaves both persisted files unchanged; the next heartbeat
    # is allowed to retry rather than incorrectly recording a successful check.
    later = decide(primary, meta, event_name="workflow_run", now=NOW + timedelta(minutes=5))
    assert later.refresh and later.reason == "refresh_due"


def test_gate_and_existing_merge_preserve_rulings_and_add_new_directory_entry():
    primary = {"BTC": 0, "BARD": 2, "MITO": 1}
    old_meta = {
        "last_check_utc": (NOW - timedelta(hours=2)).isoformat(),
        "primary_sha256": primary_digest(primary),
    }
    original = copy.deepcopy(primary)
    assert decide(primary, old_meta, event_name="workflow_run", now=NOW).refresh
    rows = [
        {"symbol": "MON", "label": "حلال", "article": ""},
        {"symbol": "BARD", "label": "مشبوهة", "article": ""},
        {"symbol": "MITO", "label": "حرام", "article": ""},
    ]
    merged, meta, report = build_merge(
        primary, old_meta, {}, {}, rows, [], set(), NOW, supplemental={"BTC": 0}
    )
    assert primary == original
    assert all(merged[s] == v for s, v in original.items())
    assert merged["MON"] == 0 and report["added"] == [{"symbol": "MON", "value": 0}]
    assert report["changed"] == []
    assert meta["primary_sha256"] == primary_digest(merged)
    assert not decide(merged, meta, event_name="workflow_run", now=NOW + timedelta(minutes=5)).refresh
