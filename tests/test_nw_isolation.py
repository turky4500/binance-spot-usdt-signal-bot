"""عزل النظام السادس: لا يقرأ ولا يكتب ملفات الأنظمة الخمسة الأخرى."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

NEW_FILES = [
    ROOT / "nw_bot.py",
    ROOT / "run_nw_once.py",
    ROOT / "src" / "nw_strategy.py",
    ROOT / "src" / "nw_signals.py",
    ROOT / "src" / "nw_state.py",
    ROOT / "src" / "nw_journal.py",
    ROOT / "src" / "nw_messages.py",
]
FORBIDDEN_PATHS = (
    "data/state.json", "data/state_daily_trend.json", "data/state_qaa.json",
    "data/state_sa.json", "data/state_zigzag.json",
    "data/trades_log.csv", "data/trades_log_daily_trend.csv",
    "data/trades_log_qaa.csv", "data/trades_log_sa.csv", "data/trades_log_zigzag.csv",
)
FORBIDDEN_IMPORTS = (
    r"^\s*import\s+(daily_trend_bot|zigzag_bot|qaa_bot|sa_bot|app)\b",
    r"^\s*from\s+(daily_trend_bot|zigzag_bot|qaa_bot|sa_bot|app)\b",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_new_code_never_mentions_other_systems_state_files():
    for path in NEW_FILES:
        text = _text(path)
        for bad in FORBIDDEN_PATHS:
            assert bad not in text, f"{path.name} يلمس {bad}"


def test_new_code_never_imports_other_bots():
    import re
    for path in NEW_FILES:
        text = _text(path)
        for pattern in FORBIDDEN_IMPORTS:
            assert not re.search(pattern, text, re.MULTILINE), f"{path.name} يستورد نظامًا آخر: {pattern}"


def test_only_shared_read_only_modules_are_used():
    text = _text(ROOT / "nw_bot.py") + _text(ROOT / "run_nw_once.py")
    for allowed in ("binance_client", "telegram_client", "src.halal"):
        assert allowed in text
    # لا تعديل على ملفات مشتركة: الوحدات المشتركة تُستدعى للقراءة فقط
    assert "merge_with_primary" in text and "load_primary_verdicts" in text


def test_state_and_journal_default_paths_are_nw_only():
    from src.nw_state import DEFAULT_STATE
    assert DEFAULT_STATE["system"] == "nw-1h"
    text = _text(ROOT / "src" / "nw_state.py") + _text(ROOT / "src" / "nw_journal.py")
    assert "state_nw.json" in text and "trades_log_nw.csv" in text


def test_no_btc_gate_in_nw_code():
    """«لا تربط الدخول بالبتكوين» — لا بوابة سوق ولا EMA في كود النظام."""
    forbidden = ("BTCUSDT", "EMA", "btc_gate", "gate_open", "DT_GATE")
    for path in NEW_FILES:
        text = _text(path)
        for bad in forbidden:
            assert bad not in text, f"{path.name} يحتوي {bad}"


def test_workflow_env_uses_only_nw_paths():
    import yaml
    doc = yaml.safe_load(_text(ROOT / ".github" / "workflows" / "nw-bot.yml"))
    env = next(iter(doc["jobs"].values()))["env"]
    assert env["NW_STATE_FILE"] == "data/state_nw.json"
    assert env["NW_TRADE_LOG"] == "data/trades_log_nw.csv"
    assert env["NW_WINDOW"] == "500" and env["NW_BANDWIDTH"] == "8"
    for key in ("DT_STATE_FILE", "ZZ_STATE_FILE", "QAA_STATE_FILE", "SA_STATE_FILE"):
        assert key not in env
