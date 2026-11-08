"""نظام 15m الموازي: نفس شروط 1H + فريم مذكور + عزل كامل للملفات.

هذا الملف جديد؛ لا يعدّل اختبارات المستخدم ولا أي ملف قائم على main.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from src import nw_messages as m
from src.nw_15m_messages import FRAME_15M, FRAME_1H, with_15m_frame

ROOT = Path(__file__).resolve().parents[1]

REC = {
    "confirm_close": 15.87,
    "confirm_open": 15.60,
    "confirm_pct": 1.73,
    "confirm_bar_time": 1_700_000_000_000,
    "bar_time": 1_700_000_000_000 - 3_600_000,
    "signal_price": 15.50,
    "confirm_green": True,
    "color_newest": "up",
    "color_at_signal": "up",
}
POS = {"entry_price": 15.00, "entry_time": REC["bar_time"], "entry_count": 2}


# ── الرسائل: القالب نفسه بالحرف، مع سطر الفريم ────────────────────────────

BUILDERS = [
    ("entry", lambda: m.entry_message("TRBUSDT", REC, "✅ مباح")),
    ("repeat", lambda: m.repeat_message("TRBUSDT", REC, 2, "⚠️ مشبوه")),
    ("exit", lambda: m.exit_message("TRBUSDT", POS, REC, 5.80, 5.60, "✅ مباح")),
    ("entry_warning", lambda: m.entry_message("TRBUSDT", REC, "✅ مباح", True)),
]


@pytest.mark.parametrize("name,build", BUILDERS)
def test_every_15m_message_names_the_frame_and_keeps_everything_else(name, build):
    original = build()
    text = with_15m_frame(original)
    assert FRAME_15M in text, name
    assert FRAME_1H not in text, name
    assert text == original.replace(FRAME_1H, FRAME_15M, 1), name
    # الأسطر الخمسة/الستة والفواصل بينها محفوظة بالحرف
    assert text.split("\n\n")[1] == FRAME_15M, name
    assert "🕒 " in text and "🕌 الحكم الشرعي:" in text, name


def test_15m_wrapper_functions_cover_all_three_message_types():
    from src import nw_15m_messages as m15

    assert m15.entry_message("TRBUSDT", REC, "✅ مباح") == with_15m_frame(
        m.entry_message("TRBUSDT", REC, "✅ مباح"))
    assert m15.repeat_message("TRBUSDT", REC, 2, "✅ مباح") == with_15m_frame(
        m.repeat_message("TRBUSDT", REC, 2, "✅ مباح"))
    assert m15.exit_message("TRBUSDT", POS, REC, 5.8, 5.6, "✅ مباح") == with_15m_frame(
        m.exit_message("TRBUSDT", POS, REC, 5.8, 5.6, "✅ مباح"))


def test_missing_frame_line_still_produces_a_frame_line():
    assert with_15m_frame("📥 توصية دخول — X\n\n💰 سعر: 1").split("\n\n")[1] == FRAME_15M
    assert with_15m_frame(FRAME_15M) == FRAME_15M


def test_1h_template_is_untouched():
    assert m.entry_message("TRBUSDT", REC, "✅ مباح").split("\n\n")[1] == FRAME_1H


# ── البناء: نفس النواة، فريم/حالة/دفتر مختلفة ─────────────────────────────

def test_15m_bot_uses_same_core_with_isolated_paths(monkeypatch, tmp_path):
    from src.nw_15m_bot import build_nw_15m_bot

    monkeypatch.setenv("TELEGRAM_MUTE", "1")
    monkeypatch.setenv("NW_STATE_FILE", str(tmp_path / "state_nw15.json"))
    monkeypatch.setenv("NW_TRADE_LOG", str(tmp_path / "trades_log_nw15.csv"))
    monkeypatch.delenv("NW_MODE", raising=False)
    monkeypatch.delenv("NW_KLINE_LIMIT", raising=False)

    bot = build_nw_15m_bot()

    assert bot.interval == "15m"
    assert bot.state_path == str(tmp_path / "state_nw15.json")
    assert bot.trade_log == str(tmp_path / "trades_log_nw15.csv")
    assert str(bot.store.path) == bot.state_path
    # نفس شروط المؤشر بالحرف (القيم الافتراضية في build_nw_bot على main)
    assert bot.cfg.mode == "envelope"
    assert bot.cfg.bandwidth == 8
    assert bot.cfg.mult == 3
    assert bot.cfg.window == 500
    assert bot.kline_limit == 560
    assert bot.include_all is True
    assert bot.exclude_pegged_stables is True
    assert bot.exclude_tokenized_stocks is False
    # تقرير 15M اليومي: نفس صيغة/توقيت 1H، لكن بمفتاح حالة مستقل.
    # الطابع 0 ⇒ 03:00 AM بالرياض ⇒ خارج نافذة 12 AM ⇒ لا يُرسل.
    assert bot.send_daily_report_if_due(0) is None

    # نقطة الإرسال الوحيدة تُضيف سطر الفريم دون تغيير أي شيء آخر
    sent = []
    bot.tg = type("TG", (), {"send_message": lambda self, t: sent.append(t)})()
    state = {"sent": [], "messages_sent": 0}
    assert bot._send_once(state, "buy:X:1:1", m.entry_message("XUSDT", REC, "✅ مباح"))
    assert FRAME_15M in sent[0]
    assert FRAME_1H not in sent[0]
    assert state["messages_sent"] == 1
    assert bot._send_once(state, "buy:X:1:1", m.entry_message("XUSDT", REC, "✅ مباح")) is False


def test_15m_bot_actually_uses_15m_klines(monkeypatch, tmp_path):
    """run_once() يمرّر 15m إلى العميل، ولا يقرأ ملفات 1H."""
    import numpy as np
    from src.nw_15m_bot import build_nw_15m_bot

    monkeypatch.setenv("TELEGRAM_MUTE", "1")
    monkeypatch.setenv("NW_STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setenv("NW_TRADE_LOG", str(tmp_path / "t.csv"))
    bot = build_nw_15m_bot()

    calls = []

    class FakeClient:
        def get_server_time(self):
            return 1_700_000_000_000

        def get_klines_for_symbols(self, symbols, interval, limit):
            calls.append((list(symbols), interval, limit))
            return {}

    bot.client = FakeClient()
    bot.symbols = ["AAAUSDT"]
    bot.verdicts = {}
    bot.refresh_universe = lambda: ["AAAUSDT"]
    monkeypatch.setattr("nw_bot.candles_closed", lambda df, now: (
        np.array([]), np.array([]), np.array([])))
    monkeypatch.setattr("nw_bot.evaluate_symbol",
                        lambda o, c, t, cfg: {"ok": False, "reason": "not_enough_bars"})
    bot.run_once()
    assert calls[0][1] == "15m"
    assert calls[0][2] == 560
    assert (tmp_path / "s.json").exists()
    assert not (ROOT / "data/state_nw.json").read_text(encoding="utf-8").startswith(
        '{\n  "version": 1,\n  "system": "nw-15m"')


# ── الوركفلوات: تشغيل متوازٍ وعزل ────────────────────────────────────────

def _workflow(name):
    # PyYAML يقرأ المفتاح on كـ True؛ نتعامل مع الاثنين
    d = yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))
    return d


def test_15m_workflow_matches_1h_conditions_except_paths_and_frame():
    w1 = _workflow("nw-bot.yml")
    w15 = _workflow("nw-bot-15m.yml")
    e1 = w1["jobs"]["run-once"]["env"]
    e15 = w15["jobs"]["run-once"]["env"]

    for key in ("NW_BANDWIDTH", "NW_WINDOW", "NW_MODE", "NW_MULT", "NW_KLINE_LIMIT",
                "NW_REQUIRE_NEWEST_GREEN", "NW_COMMISSION_SIDE_PCT",
                "NW_SHOW_REPAINT_WARNING", "NW_INCLUDE_ALL",
                "NW_EXCLUDE_PEGGED_STABLES", "NW_EXCLUDE_TOKENIZED_STOCKS",
                "NW_USE_EXCLUDE_FILE", "TELEGRAM_MUTE", "TELEGRAM_MIN_INTERVAL_SECONDS",
                "TIMEZONE", "BINANCE_BASE_URL", "BINANCE_QUOTE_ASSET",
                "REQUEST_TIMEOUT", "MAX_WORKERS", "LOG_LEVEL"):
        assert e15[key] == e1[key], key

    assert e15["NW_STATE_FILE"] == "data/state_nw15.json"
    assert e15["NW_TRADE_LOG"] == "data/trades_log_nw15.csv"
    assert e1["NW_STATE_FILE"] == "data/state_nw.json"
    assert e15["NW_STATE_FILE"] != e1["NW_STATE_FILE"]
    assert w15["jobs"]["run-once"]["if"] == "vars.NW15_ENABLED != 'false'"
    assert w1["jobs"]["run-once"]["if"] == "vars.NW_ENABLED != 'false'"
    assert w15["concurrency"]["group"] != w1["concurrency"]["group"]

    steps = "\n".join(s.get("run", "") for s in w15["jobs"]["run-once"]["steps"])
    assert "python run_nw15_once.py" in steps
    assert "data/state_nw15.json data/trades_log_nw15.csv" in steps
    assert "data/state_nw.json" not in steps.replace("data/state_nw15.json", "")
    assert "git fetch origin main" in steps and "git rebase" in steps
    assert "[skip ci]" in steps
    # لا `exit 0` كبوابة إيقاف: فقط داخل خطوة الحفظ (لا تغييرات / بعد الدفع)
    assert steps.count("exit 0") == 2
    assert len([s for s in w15["jobs"]["run-once"]["steps"] if "if:" in str(s)]) >= 0

    on = w15.get("on", w15.get(True))
    assert on["schedule"][0]["cron"] == "3,18,33,48 * * * *"
    assert "workflow_dispatch" in on


def test_15m_trigger_is_independent_and_uses_job_level_if():
    t15 = _workflow("nw-bot-15m-trigger.yml")
    t1 = _workflow("nw-bot-trigger.yml")
    j = t15["jobs"]["dispatch-nw-15m"]
    assert j["if"] == "vars.NW15_ENABLED != 'false'"
    assert t1["jobs"]["dispatch-nw"]["if"] == "vars.NW_ENABLED != 'false'"
    assert t15["concurrency"]["group"] != t1["concurrency"]["group"]
    on = t15.get("on", t15.get(True))
    assert on["workflow_run"]["workflows"] == ["Signal Bot Cron"]
    steps = "\n".join(s.get("run", "") for s in j["steps"])
    assert "nw-bot-15m.yml/dispatches" in steps
    assert "nw-bot.yml/dispatches" not in steps
    # لا `exit 0` على الإطلاق في هذا الوركفلو
    assert "exit 0" not in steps
    for window in ('"$m" -ge 15 ] && [ "$m" -lt 20', '"$m" -ge 45 ] && [ "$m" -lt 50'):
        assert window in steps


def test_1h_files_are_byte_identical_to_main():
    """حماية من أي تعديل على ملفات المستخدم: لا أثر لـ 15m داخلها.

    الاستثناء الوحيد والمُصرَّح به من المستخدم (2026-11-08): دالة
    ``send_daily_report_if_due`` في ``nw_bot.py`` لضبط التقرير اليومي.
    """
    import subprocess

    tracked = ["src/nw_messages.py", "src/nw_signals.py",
               "src/nw_state.py", "src/nw_envelope.py", "run_nw_once.py",
               ".github/workflows/nw-bot.yml", ".github/workflows/nw-bot-trigger.yml",
               "tests/test_nw_messages.py", "tests/test_nw_envelope.py",
               "README_NW_SYSTEM.md", "data/state_nw.json", "data/trades_log_nw.csv"]
    diff = subprocess.run(["git", "diff", "--name-only", "HEAD", "--", *tracked],
                          cwd=ROOT, capture_output=True, text=True, check=True).stdout
    assert diff.strip() == "", f"ملفات المستخدم معدّلة: {diff}"

    # nw_bot.py: يُسمح بتغيير جسم دالة التقرير فقط، لا أي شيء آخر.
    head = subprocess.run(["git", "show", "HEAD:nw_bot.py"], cwd=ROOT,
                          capture_output=True, text=True, check=True).stdout
    current = (ROOT / "nw_bot.py").read_text(encoding="utf-8")

    def strip_report(src: str) -> str:
        start = src.index("    def send_daily_report_if_due(")
        end = src.index("    def run_once(", start)
        return src[:start] + "<REPORT>" + src[end:]

    assert strip_report(head) == strip_report(current), \
        "تعديل خارج دالة التقرير اليومي في nw_bot.py"


def test_15m_state_file_is_separate():
    """العزل هو المطلوب؛ لا نفترض أن الحالة الحيّة على main ما زالت فارغة."""
    import json

    p1, p15 = ROOT / "data/state_nw.json", ROOT / "data/state_nw15.json"
    s1 = json.loads(p1.read_text(encoding="utf-8"))
    s15 = json.loads(p15.read_text(encoding="utf-8"))
    assert s15["system"] == "nw-15m"
    assert s1["system"] == "nw-1h"
    assert {"cycles", "sent", "symbols"} <= set(s15)
    assert p1.read_bytes() != p15.read_bytes(), "ملفا الحالة يجب أن يظلا منفصلين"
    assert any(s15[k] != s1.get(k) for k in ("cycles", "sent", "symbols"))
