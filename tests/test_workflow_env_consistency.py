"""اختبارات اتساق بيئة التشغيل بين ملفات الوركفلو والكود (2026-11-01).

سبب وجودها: متغيّر `ZZ_EMA_INTERVAL=4h` أُضيف إلى وركفلو الكرون فقط، وغاب عن
وركفلو المراقب السريع، فعاد الكود إلى القيمة الافتراضية `1h` بصمت لمدة 14 ساعة.
هذه الاختبارات تُفشل أي تعديل يكرّر الخطأ.

python -m pytest tests/ -q
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows"
WATCHER = "zigzag-watch-loop.yml"
CRON = "zigzag-signal-bot-cron.yml"
WATCHDOG = "zigzag-watchdog.yml"

# مفاتيح خاصة بوركفلو المراقب ولا تُقارن بالكرون
WORKFLOW_ONLY = {"GH_TOKEN", "REPO", "HOURS", "WATCHER_ENABLED", "GITHUB_TOKEN"}

# الإعدادات المعتمدة من المستخدم — يجب أن تكون ظاهرة صراحةً في ملفات التشغيل
APPROVED = {
    "ZZ_EMA_FILTER": "1",
    "ZZ_EMA_LEN": "200",
    "ZZ_EMA_INTERVAL": "4h",
    "ZZ_TARGET_R": "1.5",
    "ZZ_MAX_STOP_PCT": "2.5",
    "ZZ_MIN_STOP_PCT": "0",
    "ZZ_EXCLUDE_B_SUFFIX": "1",
    "EXCLUDE_PEGGED_STABLES": "1",
    "TELEGRAM_MUTE": "0",
}


def _load(name: str) -> dict:
    return yaml.safe_load((WF / name).read_text(encoding="utf-8"))


def _envs(name: str) -> dict[str, str]:
    data = _load(name)
    env: dict[str, str] = {}
    for job in (data.get("jobs") or {}).values():
        for key, value in (job.get("env") or {}).items():
            env[key] = str(value)
        for step in job.get("steps") or []:
            for key, value in (step.get("env") or {}).items():
                env[key] = str(value)
    return env


# ─────────────────────────────────────────── صلاحية الملفات
@pytest.mark.parametrize("name", sorted(p.name for p in WF.glob("*.yml")))
def test_all_workflows_are_valid_yaml(name):
    assert isinstance(_load(name), dict)


# ─────────────────────────────────────────── اتساق البيئة بين المراقب والكرون
def test_watcher_and_cron_have_identical_env():
    watcher = {k: v for k, v in _envs(WATCHER).items() if k not in WORKFLOW_ONLY}
    cron = {k: v for k, v in _envs(CRON).items() if k not in WORKFLOW_ONLY}
    missing_in_watcher = sorted(set(cron) - set(watcher))
    missing_in_cron = sorted(set(watcher) - set(cron))
    assert not missing_in_watcher, f"متغيّرات في الكرون وغائبة عن المراقب: {missing_in_watcher}"
    assert not missing_in_cron, f"متغيّرات في المراقب وغائبة عن الكرون: {missing_in_cron}"
    different = {k: (cron[k], watcher[k]) for k in cron if cron[k] != watcher[k]}
    assert not different, f"قيم مختلفة بين الملفين: {different}"


@pytest.mark.parametrize("name", [WATCHER, CRON])
@pytest.mark.parametrize("key,expected", sorted(APPROVED.items()))
def test_approved_settings_are_explicit(name, key, expected):
    env = _envs(name)
    assert key in env, f"{name}: المتغيّر {key} غير مضبوط صراحةً (سيعمل على القيمة الافتراضية)"
    assert env[key] == expected, f"{name}: {key}={env[key]!r} والمطلوب {expected!r}"


def test_ema_interval_is_never_left_to_the_code_default():
    """القيمة الافتراضية في الكود هي 1h — لذلك يجب أن يُضبط 4h صراحةً في كل ملف."""
    source = (ROOT / "src" / "zigzag.py").read_text(encoding="utf-8")
    default = re.search(r'ZZ_EMA_INTERVAL",\s*"([^"]+)"', source)
    assert default and default.group(1) == "1h"  # نُثبّت معرفة أن الافتراضي 1h
    for name in (WATCHER, CRON):
        assert _envs(name).get("ZZ_EMA_INTERVAL") == "4h"


# ─────────────────────────────────────────── استمرارية المراقب
def test_watcher_can_dispatch_itself():
    data = _load(WATCHER)
    permissions = data.get("permissions") or {}
    assert permissions.get("actions") == "write", "بدون actions: write ترجع جدولة الدورة التالية 403"
    assert permissions.get("contents") == "write"
    steps = [s for job in data["jobs"].values() for s in job.get("steps") or []]
    chain = [s for s in steps if "Chain" in str(s.get("name", ""))]
    assert chain, "خطوة إعادة تشغيل المراقب غير موجودة"
    assert str(chain[0].get("if", "")).startswith("${{ always()"), "إعادة التشغيل يجب أن تعمل حتى بعد فشل الحلقة"


def test_watchdog_exists_and_dispatches_the_watcher():
    data = _load(WATCHDOG)
    assert (data.get("permissions") or {}).get("actions") == "write"
    text = (WF / WATCHDOG).read_text(encoding="utf-8")
    assert "zigzag-watch-loop.yml/dispatches" in text
    assert "schedule" in data[True] or "schedule" in data.get("on", {})


# ─────────────────────────────────────────── الإسكات: ZigZag وحده يُرسل
@pytest.mark.parametrize("name", ["signal-bot-cron.yml", "qaa-signal-bot-cron.yml", "sa-signal-bot-cron.yml"])
def test_other_systems_stay_muted(name):
    assert _envs(name).get("TELEGRAM_MUTE") == "1", f"{name} يجب أن يبقى مُسكتًا"


def test_zigzag_is_unmuted():
    for name in (WATCHER, CRON):
        assert _envs(name).get("TELEGRAM_MUTE") == "0"


# ─────────────────────────────────────────── حجب التوكن
def test_telegram_token_is_redacted_in_logs():
    from src.telegram_client import redact

    text = "POST https://api.telegram.org/bot123456789:AAF9xYzQwErTyUiOpAsDfGhJkL/sendMessage failed"
    cleaned = redact(text)
    assert "AAF9xYzQwErTyUiOpAsDfGhJkL" not in cleaned
    assert "bot<REDACTED>" in cleaned


def test_effective_settings_log_warns_on_wrong_interval(tmp_path, caplog):
    import sys

    sys.path.insert(0, str(ROOT / "tests"))
    from test_zigzag_ema_batching import make_bot

    bot = make_bot(tmp_path)
    bot.settings.ema_filter = 1          # الفلتر مفعّل كما في الإنتاج
    bot.settings.ema_interval = "1h"     # فريم خاطئ يجب التحذير منه
    with caplog.at_level("INFO"):
        bot._log_effective_settings()
    assert any("فريم فلتر EMA" in r.message for r in caplog.records)
    assert any(r.levelname == "WARNING" for r in caplog.records), "يجب التحذير عند مخالفة فريم 4h"


def test_effective_settings_block_is_logged_once(tmp_path, caplog):
    import sys

    sys.path.insert(0, str(ROOT / "tests"))
    from test_zigzag_ema_batching import make_bot

    bot = make_bot(tmp_path)
    bot.settings.ema_interval = "4h"
    with caplog.at_level("INFO"):
        bot._log_effective_settings()
        bot._log_effective_settings()
    infos = [r for r in caplog.records if "الإعدادات الفعّالة" in str(r.message)]
    assert len(infos) == 1
    assert not any(r.levelname == "WARNING" for r in caplog.records)
