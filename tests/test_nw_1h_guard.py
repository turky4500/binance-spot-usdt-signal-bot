"""اختبارات إصلاح جدولة فريم 1H (2026-11-09): تشغيل موثوق بلا تكرار."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows"


def _doc(name: str) -> dict:
    return yaml.safe_load((WF / name).read_text(encoding="utf-8"))


def _on(doc: dict) -> dict:
    return doc.get("on") or doc.get(True)


def test_guarded_kick_workflow_exists_and_is_isolated():
    guard = _doc("nw-bot-1h-guard.yml")
    on = _on(guard)
    assert on["workflow_run"]["workflows"] == ["Signal Bot Cron"]
    assert on["workflow_run"]["types"] == ["completed"]
    assert "push" not in on
    assert "workflow_dispatch" in on
    assert any("2,32 * * * *" in str(c["cron"]) for c in on["schedule"])
    assert guard["permissions"]["actions"] == "write"
    assert guard["concurrency"]["group"] == "nw-1h-guard"
    job = next(iter(guard["jobs"].values()))
    assert "NW_ENABLED" in str(job.get("if"))
    script = "\n".join(str(s.get("run", "")) for s in job["steps"])
    assert "nw-bot.yml/dispatches" in script
    assert '{"ref":"main"}' in script
    assert "date -u +%M" in script
    assert "in_progress" in script and "queued" in script
    assert "25" in script
    text = (WF / "nw-bot-1h-guard.yml").read_text(encoding="utf-8")
    assert "PAT" not in text and "ghp_" not in text
    assert "nw-bot-4h" not in text and "nw-bot-15m" not in text
    assert "data/state_nw.json" not in text


def test_1h_bot_has_singleton_guard_and_fresh_checkout():
    doc = _doc("nw-bot.yml")
    on = _on(doc)
    assert "push" not in on
    assert "workflow_dispatch" in on
    assert any("5 * * * *" in str(c["cron"]) for c in on["schedule"])
    assert doc["permissions"]["contents"] == "write"
    assert doc["permissions"]["actions"] == "read"
    assert doc["concurrency"]["group"] == "nw-1h-bot"
    job = doc["jobs"]["run-once"]
    assert job["if"] == "vars.NW_ENABLED != 'false'"
    steps = job["steps"]
    names = [str(s.get("name", "")) for s in steps]
    assert any("Guard against concurrent" in n for n in names)
    assert any("Refresh checkout to latest main" in n for n in names)
    runs = "\n".join(str(s.get("run", "")) for s in steps)
    assert "git reset --hard origin/main" in runs
    assert "GITHUB_OUTPUT" in runs
    assert "rebase -X theirs" not in runs
    assert "git rebase origin/main" in runs
    for s in steps:
        if "Run NW bot once" in str(s.get("name", "")) or "Commit NW state updates" in str(s.get("name", "")):
            assert s.get("if") == "steps.guard.outputs.proceed == 'true'"
    assert job["env"]["TELEGRAM_MUTE"] == "0"


def test_old_trigger_file_is_unchanged_and_no_parallel_dispatch():
    old = (WF / "nw-bot-trigger.yml").read_text(encoding="utf-8")
    assert "nw-bot.yml/dispatches" in old
    assert "nw-bot-1h-guard.yml" not in old
    doc = _doc("nw-bot-trigger.yml")
    assert doc["concurrency"]["group"] == "nw-1h-trigger"
