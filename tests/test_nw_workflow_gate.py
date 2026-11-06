"""بوابات الوركفلو الجديد: حماية على مستوى المهمة، تفعيل فوري، وعزله."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows"
NEW = WF / "nw-bot.yml"


def _load(path: Path) -> dict:
    # YAML يقرأ `on` كمفتاح منطقي في PyYAML 1.1 ⇒ نستخدم SafeLoader كما في اختبارات المشروع
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_job_level_gate_and_no_exit0_gate():
    doc = _load(NEW)
    jobs = doc["jobs"]
    assert len(jobs) == 1
    job = next(iter(jobs.values()))
    assert "NW_ENABLED" in str(job.get("if")), "يجب أن تكون الحماية على مستوى المهمة"
    names = [str(s.get("name", "")) for s in job["steps"]]
    assert not any(n.startswith("فحص مفتاح التشغيل") for n in names)


def test_live_telegram_is_enabled():
    job = next(iter(_load(NEW)["jobs"].values()))
    env = job["env"]
    assert str(env["TELEGRAM_MUTE"]) == "0"
    assert "secrets.TELEGRAM_BOT_TOKEN" in str(env["TELEGRAM_BOT_TOKEN"])
    assert "secrets.TELEGRAM_CHAT_ID" in str(env["TELEGRAM_CHAT_ID"])


def test_universe_covers_all_rulings_and_no_btc_gate():
    env = next(iter(_load(NEW)["jobs"].values()))["env"]
    assert env["NW_INCLUDE_ALL"] == "1"          # لا فلتر شرعي
    assert env["NW_USE_EXCLUDE_FILE"] == "0"
    assert env["NW_EXCLUDE_PEGGED_STABLES"] == "1"
    assert env["NW_EXCLUDE_TOKENIZED_STOCKS"] == "0"
    assert env["NW_REQUIRE_NEWEST_GREEN"] == "0"   # لون الخط ليس شرطًا
    assert env["NW_SHOW_REPAINT_WARNING"] == "0"   # القالب المبسّط بلا سطر تحذير
    for bad in ("DT_GATE", "BTC", "NW_GATE"):
        assert bad not in env


def test_schedule_and_concurrency():
    doc = _load(NEW)
    on = doc.get("on") or doc.get(True)
    crons = on["schedule"]
    assert any("5 * * * *" in str(c["cron"]) for c in crons)
    assert "workflow_dispatch" in on
    assert doc["concurrency"]["group"] == "nw-1h-bot"
    assert doc["permissions"]["contents"] == "write"


def test_workflow_only_commits_its_own_state_files():
    text = NEW.read_text(encoding="utf-8")
    assert "data/state_nw.json" in text and "data/trades_log_nw.csv" in text
    for other in ("state_daily_trend", "state_qaa", "state_sa", "state_zigzag", "trades_log_daily_trend"):
        assert other not in text, other
    assert "nw-1h-bot" in text


def test_no_workflow_is_triggered_by_push():
    """الدفع إلى main لا يجب أن يطلق أي تشغيل (المستودع حسّاس جدًا)."""
    for path in WF.glob("*.yml"):
        doc = _load(path)
        on = doc.get("on") or doc.get(True)
        assert "push" not in on, path.name
