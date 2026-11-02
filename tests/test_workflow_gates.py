"""حماية المفاتيح: «exit 0» داخل خطوة في GitHub Actions لا يوقف بقية الخطوات.

الخلل الذي حدث فعليًا (2026-11-02): بوت ZigZag ظل يعمل ويُرسل رسائل رغم أن خطوة
«فحص مفتاح التشغيل» تنتهي بـ exit 0 — لأن exit 0 يُنهي الخطوة بنجاح، لا المهمة.
العلاج الصحيح: if على مستوى المهمة (job-level if).
"""
from __future__ import annotations

from pathlib import Path

import yaml

WF = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WF / name).read_text(encoding="utf-8"))


def test_zigzag_jobs_are_gated_at_job_level():
    for name, job in _load("zigzag-signal-bot-cron.yml")["jobs"].items():
        assert "ZIGZAG_ENABLED" in str(job.get("if")), name
    for name, job in _load("zigzag-watch-loop.yml")["jobs"].items():
        assert "ZIGZAG_ENABLED" in str(job.get("if")), name
    for name, job in _load("zigzag-watchdog.yml")["jobs"].items():
        assert "ZIGZAG_ENABLED" in str(job.get("if")), name


def test_no_workflow_relies_on_exit0_as_a_gate():
    """أي خطوة «فحص مفتاح» يجب أن تُقابلها if على مستوى المهمة."""
    for path in WF.glob("*.yml"):
        doc = _load(path.name)
        for job_name, job in (doc.get("jobs") or {}).items():
            steps = job.get("steps") or []
            gate_steps = [s for s in steps if str(s.get("name", "")).startswith("فحص مفتاح التشغيل")]
            if gate_steps:
                assert job.get("if"), f"{path.name}:{job_name} يعتمد على exit 0 كبوابة"
