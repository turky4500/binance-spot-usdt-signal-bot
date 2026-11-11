"""حماية من تكرار عطل توقف نظام NW 1H (2026-11-09 ~9 ساعات).

المشهد الحقيقي: توقّفت جدولة GitHub وحدث workflow_run معًا، فلم يُطلق
nw-bot.yml، وفُقدت 73 رسالة لأنbars_ago==1 لا يُعاد تقييمها لاحقًا.
هذه الاختبارات تُثبّت ثلاث طبقات: إعادة محاولة استعلام GitHub API،
انتهاء صلاحية المهمة «النشطة» المعلّقة (زومبي)، وحارس صحة مستقل
يعتمد على عمر حالة 1H بدل الاعتماد على حدث واحد.

python -m pytest tests/ -q
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows"


def _doc(name: str) -> dict:
    return yaml.safe_load((WF / name).read_text(encoding="utf-8"))


def _on(doc: dict) -> dict:
    return doc.get("on") or doc.get(True)


def _script(name: str) -> str:
    doc = _doc(name)
    return "\n".join(
        str(s.get("run", "")) for job in doc["jobs"].values() for s in (job.get("steps") or [])
    )


def test_1h_bot_singleton_guard_retries_and_expires_zombies():
    text = (WF / "nw-bot.yml").read_text(encoding="utf-8")
    script = _script("nw-bot.yml")
    assert "range(1, 4)" in script, "استعلام GitHub API يجب أن يُعاد 3 مرات"
    assert "is_fresh" in script and "age < 30" in script, "يجب انتهاء صلاحية المهمة المعلّقة"
    assert "proceed=true" in script, "عطل GitHub API عابر لا يُجمّد النظام"
    assert "raise SystemExit(1)" not in script, "502 عابر لا يُسقط الدورة"
    assert "run_nw1h_once_safe.py" in script       # غلاف إعادة المحاولة موجود أصلًا
    assert "git reset --hard origin/main" in script
    assert "-X theirs" not in text


def test_guarded_kick_retries_and_expires_zombies():
    text = (WF / "nw-bot-1h-guard.yml").read_text(encoding="utf-8")
    script = _script("nw-bot-1h-guard.yml")
    assert "range(1, 4)" in script
    assert "is_fresh" in script and "age < 30" in script
    assert "raise SystemExit(1)" not in script
    assert "25" in script                          # منع الإطلاق المكرّر دون تغيير
    assert "date -u +%M" in script                # النافذتان دون تغيير
    assert "data/state_nw.json" not in text        # عزل المُشغّل عن حالة البوت


def test_health_watchdog_is_independent_and_state_based():
    path = WF / "nw-bot-1h-watchdog.yml"
    assert path.exists()
    doc = _doc(path.name)
    on = _on(doc)
    assert "push" not in on
    assert on["workflow_run"]["workflows"] == ["Signal Bot Cron"]
    assert on["workflow_run"]["types"] == ["completed"]
    assert any("*/10 * * * *" in str(c["cron"]) for c in on["schedule"])
    assert "workflow_dispatch" in on
    assert doc["permissions"]["actions"] == "write"
    assert doc["permissions"]["contents"] == "read"
    job = next(iter(doc["jobs"].values()))
    gate = str(job.get("if"))
    assert "NW_ENABLED" in gate
    assert "workflow_run.conclusion == 'success'" in gate   # الشرط الصارم محفوظ
    script = _script(path.name)
    assert "last_cycle_ms" in script
    assert "MAX_AGE_MIN = 75" in script
    assert "FRESH_RUN_MIN = 30" in script
    assert "data/state_nw.json" in script
    assert "nw-bot.yml/dispatches" in script
    assert '{"ref":"main"}' in script
    assert "range(1, 4)" in script
    # لا رسائل ولا حالة لأنظمة أخرى
    text = path.read_text(encoding="utf-8")
    for forbidden in ("TELEGRAM", "sendMessage", "state_nw4h", "state_sa",
                      "state_qaa", "state_zigzag", "state_daily_trend",
                      "nw-bot-4h", "nw-bot-15m", "ghp_", "PAT"):
        assert forbidden not in text, forbidden


def test_no_push_trigger_anywhere():
    for path in WF.glob("*.yml"):
        assert "push" not in _on(_doc(path.name)), path.name
