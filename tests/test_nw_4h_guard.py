"""اختبارات تأمين جدولة فريم 4H (2026-11-09): يعمل تلقائيًا بلا تكرار."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows"


def _doc(name: str) -> dict:
    return yaml.safe_load((WF / name).read_text(encoding="utf-8"))


def _on(doc: dict) -> dict:
    return doc.get("on") or doc.get(True)


def test_4h_guard_kicks_bot_and_report_with_barriers():
    guard = _doc("nw-bot-4h-guard.yml")
    on = _on(guard)
    assert on["workflow_run"]["workflows"] == ["Signal Bot Cron"]
    assert on["workflow_run"]["types"] == ["completed"]
    assert "push" not in on
    assert "workflow_dispatch" in on
    assert any("3,8,13,18 * * * *" in str(c["cron"]) for c in on["schedule"])
    assert guard["permissions"]["actions"] == "write"
    assert guard["permissions"]["contents"] == "read"
    assert guard["concurrency"]["group"] == "nw-4h-guard"
    # مهمة واحدة بقرار واحد: لا تلغي مهمةٌ مهمةً أخرى عند تزامن النبضات.
    assert guard["concurrency"]["cancel-in-progress"] is False
    assert list(guard["jobs"]) == ["kick"]
    job = guard["jobs"]["kick"]
    # بوابة بسيطة: لا يُفقد أي نبضة بسبب conclusion النبض المُحفِّز.
    assert job["if"] == "vars.NW4H_ENABLED != 'false'"
    assert "workflow_run.conclusion" not in str(job.get("if"))
    script = "\n".join(str(s.get("run", "")) for s in job["steps"])

    assert "nw-bot-4h.yml/dispatches" in script or "dispatch(\"nw-bot-4h.yml\")" in script
    assert "nw-bot-4h-report.yml/dispatches" in script or "dispatch(\"nw-bot-4h-report.yml\")" in script
    assert '{"ref":"main"}' in script
    assert "h % 4 == 0" in script and "3 <= m < 20" in script  # نوافذ الإشارات
    assert 'h == 21' in script and 'm < 20' in script          # نافذة التقرير
    assert "window_start" in script and "h // 4" in script      # نجاح واحد/شمعة
    assert "last_report_day" in script                          # منع تقرير مكرر
    assert "data/state_nw4h.json" in script
    assert "age_h < 20" in script
    assert "in_progress" in script and "queued" in script
    assert "conclusion" in script and "success" in script       # فاشل لا يحجب
    assert "⏭" in script and "🚀" in script

    text = (WF / "nw-bot-4h-guard.yml").read_text(encoding="utf-8")
    assert "PAT" not in text and "ghp_" not in text
    assert "nw-bot.yml/dispatches" not in text
    assert "nw-bot-15m" not in text
    assert "data/state_nw.json" not in text
    assert "data/state_nw15.json" not in text


def test_4h_bot_has_singleton_fresh_checkout_and_safe_wrapper():
    doc = _doc("nw-bot-4h.yml")
    on = _on(doc)
    assert "push" not in on and "workflow_run" not in on
    assert "workflow_dispatch" in on
    assert any("3 0,4,8,12,16,20" in str(c["cron"]) for c in on["schedule"])
    assert doc["permissions"]["contents"] == "write"
    assert doc["permissions"]["actions"] == "read"
    assert doc["concurrency"]["group"] == "nw-4h-bot"
    job = doc["jobs"]["run-once"]
    assert job["if"] == "vars.NW4H_ENABLED != 'false'"
    assert job["env"]["TELEGRAM_MUTE"] == "0"
    assert job["env"]["NW_STATE_FILE"] == "data/state_nw4h.json"
    assert job["env"]["NW_TRADE_LOG"] == "data/trades_log_nw4h.csv"

    steps = job["steps"]
    names = [str(s.get("name", "")) for s in steps]
    assert any("Guard against concurrent 4H" in n for n in names)
    assert any("Refresh checkout to latest main" in n for n in names)
    runs = "\n".join(str(s.get("run", "")) for s in steps)
    assert "git reset --hard origin/main" in runs
    assert "GITHUB_OUTPUT" in runs and "proceed" in runs
    assert "rebase -X theirs" not in runs
    assert "git rebase origin/main" in runs
    assert "run_nw4h_once_safe.py" in runs
    assert "run_nw4h_once.py" not in runs
    assert "for attempt in 1 2 3" in runs
    for s in steps:
        if s.get("name") == "Run NW 4H bot once":
            assert s.get("if") == "steps.guard.outputs.proceed == 'true'"
        if s.get("name") == "Commit NW 4H state updates":
            assert "always()" in str(s.get("if"))


def test_4h_report_uses_fresh_checkout_and_no_conflict_hiding():
    doc = _doc("nw-bot-4h-report.yml")
    on = _on(doc)
    assert "push" not in on and "workflow_run" not in on
    assert any("10 21 * * *" in str(c["cron"]) for c in on["schedule"])
    assert doc["concurrency"]["group"] == "nw-4h-report"
    job = doc["jobs"]["send-daily-report"]
    assert job["if"] == "vars.NW4H_ENABLED != 'false'"
    assert job["env"]["TELEGRAM_MUTE"] == "0"
    runs = "\n".join(str(s.get("run", "")) for s in job["steps"])
    assert "git reset --hard origin/main" in runs
    assert "git rebase origin/main" in runs
    assert "rebase -X theirs" not in runs
    assert "run_nw4h_report_once.py" in runs
    assert "run_nw4h_once.py" not in runs


def test_4h_safe_wrapper_persists_after_each_send_and_dedupes_journal():
    text = (ROOT / "run_nw4h_once_safe.py").read_text(encoding="utf-8")
    assert "build_nw_4h_bot()" in text
    assert "bot.store.save(state)" in text
    assert "nw_bot.append_trade = _append_trade_once" in text
    assert "_row_exists" in text
    assert "run_nw4h_once.py" not in text
    assert "data/state_nw.json" not in text
    assert "data/state_nw15.json" not in text
    assert "data/state_nw4h.json" in text


def test_1h_and_15m_workflows_are_not_touched_by_4h_scheduling():
    """تأمين 4H لا يُعيد تفعيل التريغر القديم ولا يمسّ 15M."""
    old_trigger = (WF / "nw-bot-trigger.yml").read_text(encoding="utf-8")
    assert "nw-bot.yml/dispatches" in old_trigger
    assert "nw-bot-4h" not in old_trigger
    for name in ("nw-bot-15m.yml", "nw-bot-15m-trigger.yml"):
        t = (WF / name).read_text(encoding="utf-8")
        assert "nw-bot-4h" not in t
        assert "nw-bot-4h.yml/dispatches" not in t
