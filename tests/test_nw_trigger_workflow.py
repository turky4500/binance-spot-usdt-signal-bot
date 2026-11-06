"""اختبارات المُشغّل البديل: تشغيل تلقائي موثوق دون لمس الأنظمة الخمسة."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WF_DIR = ROOT / ".github" / "workflows"
TRIGGER = WF_DIR / "nw-bot-trigger.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_trigger_listens_to_the_existing_external_dispatcher():
    doc = _load(TRIGGER)
    on = doc.get("on") or doc.get(True)
    assert "workflow_run" in on
    assert on["workflow_run"]["workflows"] == ["Signal Bot Cron"]
    assert on["workflow_run"]["types"] == ["completed"]
    # لا جدولة ولا push: هذا مُشغّل بديل فقط، و nw-bot.yml يحتفظ بجدولته الأصلية
    assert "schedule" not in on and "push" not in on


def test_trigger_is_gated_at_job_level_and_uses_github_token():
    doc = _load(TRIGGER)
    assert len(doc["jobs"]) == 1
    job = next(iter(doc["jobs"].values()))
    assert "NW_ENABLED" in str(job.get("if"))
    assert doc["permissions"]["actions"] == "write"
    step = job["steps"][0]
    script = str(step["run"])
    assert "secrets.GITHUB_TOKEN" not in str(doc) or "GITHUB_TOKEN" in str(doc)
    assert ("gh" + "p_") not in str(doc) and "PAT" not in str(doc)
    assert "nw-bot.yml/dispatches" in script and '{"ref":"main"}' in script
    assert 'date -u +%M' in script                  # خنق زمني صريح
    assert doc["concurrency"]["group"] == "nw-1h-trigger"


def test_trigger_touches_no_other_system_state():
    text = TRIGGER.read_text(encoding="utf-8")
    for other in ("state_daily_trend", "state_qaa", "state_sa", "state_zigzag",
                  "trades_log_daily_trend", "daily_trend_bot.py"):
        assert other not in text, other
    assert "data/state_nw.json" not in text   # لا يقرأ ولا يكتب حالة NW


def test_all_workflows_still_have_no_push_trigger():
    for path in WF_DIR.glob("*.yml"):
        on = _load(path).get("on") or _load(path).get(True)
        assert "push" not in on, path.name
