"""نظام NW 4H البديل عن 15M: نفس نظام 1H، فرق الفريم فقط + تنبيهات مفعّلة.

هذا الملف جديد؛ لا يعدّل اختبارات المستخدم ولا أي ملف قائم على main.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
import yaml

from src import nw_messages as m
from src.nw_4h_messages import FRAME_1H, FRAME_4H, with_4h_frame

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
def test_every_4h_message_names_the_frame_and_keeps_everything_else(name, build):
    original = build()
    text = with_4h_frame(original)
    assert FRAME_4H in text, name
    assert FRAME_1H not in text, name
    assert text == original.replace(FRAME_1H, FRAME_4H, 1), name
    assert text.split("\n\n")[1] == FRAME_4H, name
    assert "🕒 " in text and "🕌 الحكم الشرعي:" in text, name


def test_4h_wrapper_functions_cover_all_three_message_types():
    from src import nw_4h_messages as m4

    assert m4.entry_message("TRBUSDT", REC, "✅ مباح") == with_4h_frame(
        m.entry_message("TRBUSDT", REC, "✅ مباح"))
    assert m4.repeat_message("TRBUSDT", REC, 2, "✅ مباح") == with_4h_frame(
        m.repeat_message("TRBUSDT", REC, 2, "✅ مباح"))
    assert m4.exit_message("TRBUSDT", POS, REC, 5.8, 5.6, "✅ مباح") == with_4h_frame(
        m.exit_message("TRBUSDT", POS, REC, 5.8, 5.6, "✅ مباح"))


def test_missing_frame_line_still_produces_a_frame_line():
    assert with_4h_frame("📥 توصية دخول — X\n\n💰 سعر: 1").split("\n\n")[1] == FRAME_4H
    assert with_4h_frame(FRAME_4H) == FRAME_4H


def test_1h_template_is_untouched():
    assert m.entry_message("TRBUSDT", REC, "✅ مباح").split("\n\n")[1] == FRAME_1H


# ── البناء: نفس النواة، فريم/حالة/دفتر مختلفة ─────────────────────────────

def test_4h_bot_uses_same_core_with_isolated_paths(monkeypatch, tmp_path):
    from src.nw_4h_bot import (
        FRAME_LABEL_4H, INTERVAL_MS_4H, REPORT_STATE_KEY_4H, build_nw_4h_bot,
    )

    monkeypatch.setenv("TELEGRAM_MUTE", "1")
    monkeypatch.setenv("NW_STATE_FILE", str(tmp_path / "state_nw4h.json"))
    monkeypatch.setenv("NW_TRADE_LOG", str(tmp_path / "trades_log_nw4h.csv"))
    monkeypatch.delenv("NW_MODE", raising=False)
    monkeypatch.delenv("NW_KLINE_LIMIT", raising=False)

    bot = build_nw_4h_bot()

    assert bot.interval == "4h"
    assert bot.state_path == str(tmp_path / "state_nw4h.json")
    assert bot.trade_log == str(tmp_path / "trades_log_nw4h.csv")
    assert str(bot.store.path) == bot.state_path
    assert bot.cfg.mode == "envelope"
    assert bot.cfg.bandwidth == 8
    assert bot.cfg.mult == 3
    assert bot.cfg.window == 500
    assert bot.kline_limit == 560
    assert bot.include_all is True
    assert bot.exclude_pegged_stables is True
    assert bot.exclude_tokenized_stocks is False
    assert INTERVAL_MS_4H == 14_400_000
    assert FRAME_LABEL_4H == "NW 4H"
    assert REPORT_STATE_KEY_4H == "daily_report_nw4h"
    # الطابع 0 ⇒ 03:00 AM بالرياض ⇒ خارج نافذة 12 AM ⇒ لا يُرسل.
    assert bot.send_daily_report_if_due(0) is None

    sent = []
    bot.tg = type("TG", (), {"send_message": lambda self, t: sent.append(t)})()
    state = {"sent": [], "messages_sent": 0}
    assert bot._send_once(state, "buy:X:1:1", m.entry_message("XUSDT", REC, "✅ مباح"))
    assert FRAME_4H in sent[0]
    assert FRAME_1H not in sent[0]
    assert state["messages_sent"] == 1
    assert bot._send_once(state, "buy:X:1:1", m.entry_message("XUSDT", REC, "✅ مباح")) is False


def test_4h_bot_actually_uses_4h_klines(monkeypatch, tmp_path):
    """run_once() يمرّر 4h إلى العميل، ولا يقرأ ملفات 1H/15M."""
    from src.nw_4h_bot import build_nw_4h_bot

    monkeypatch.setenv("TELEGRAM_MUTE", "1")
    monkeypatch.setenv("NW_STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setenv("NW_TRADE_LOG", str(tmp_path / "t.csv"))
    bot = build_nw_4h_bot()

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

    before_1h = (ROOT / "data/state_nw.json").read_bytes()
    before_15m = (ROOT / "data/state_nw15.json").read_bytes()
    bot.run_once()
    assert calls[0][1] == "4h"
    assert calls[0][2] == 560
    assert (tmp_path / "s.json").exists()
    assert (ROOT / "data/state_nw.json").read_bytes() == before_1h
    assert (ROOT / "data/state_nw15.json").read_bytes() == before_15m


def test_4h_report_script_never_runs_a_signal_cycle():
    source = (ROOT / "run_nw4h_report_once.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    attrs = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    names = {n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "send_daily_report_if_due" in attrs
    assert "run_once" not in attrs and "run_once" not in names
    assert "refresh_universe" not in attrs and "refresh_universe" not in names


# ── الوركفلوات: نفس شروط 1H، بلا محفّز ⇒ لا تكرار ──────────────────────────

def _workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))


def test_4h_workflow_matches_1h_conditions_except_paths_and_frame():
    w1 = _workflow("nw-bot.yml")
    w4 = _workflow("nw-bot-4h.yml")
    e1 = w1["jobs"]["run-once"]["env"]
    e4 = w4["jobs"]["run-once"]["env"]

    for key in ("NW_BANDWIDTH", "NW_WINDOW", "NW_MODE", "NW_MULT", "NW_KLINE_LIMIT",
                "NW_REQUIRE_NEWEST_GREEN", "NW_COMMISSION_SIDE_PCT",
                "NW_SHOW_REPAINT_WARNING", "NW_INCLUDE_ALL",
                "NW_EXCLUDE_PEGGED_STABLES", "NW_EXCLUDE_TOKENIZED_STOCKS",
                "NW_USE_EXCLUDE_FILE", "TELEGRAM_MUTE", "TELEGRAM_MIN_INTERVAL_SECONDS",
                "TIMEZONE", "BINANCE_BASE_URL", "BINANCE_QUOTE_ASSET",
                "REQUEST_TIMEOUT", "MAX_WORKERS", "LOG_LEVEL",
                "NW_HALAL_PRIMARY", "NW_HALAL_EXTRA", "NW_HALAL_CACHE",
                "NW_EXCLUDE_FILE"):
        assert e4[key] == e1[key], key

    assert e4["NW_STATE_FILE"] == "data/state_nw4h.json"
    assert e4["NW_TRADE_LOG"] == "data/trades_log_nw4h.csv"
    assert e1["NW_STATE_FILE"] == "data/state_nw.json"
    assert e4["NW_STATE_FILE"] != e1["NW_STATE_FILE"]
    assert e4["TELEGRAM_MUTE"] == "0"          # التنبيهات مفعّلة
    assert w4["jobs"]["run-once"]["if"] == "vars.NW4H_ENABLED != 'false'"
    assert w1["jobs"]["run-once"]["if"] == "vars.NW_ENABLED != 'false'"
    assert w4["concurrency"]["group"] != w1["concurrency"]["group"]

    steps = "\n".join(s.get("run", "") for s in w4["jobs"]["run-once"]["steps"])
    assert "python run_nw4h_once_safe.py" in steps
    assert "run_nw4h_once.py" not in steps      # الغلاف الآمن فقط
    assert "data/state_nw4h.json data/trades_log_nw4h.csv" in steps
    assert "data/state_nw.json" not in steps.replace("data/state_nw4h.json", "")
    assert "data/state_nw15.json" not in steps
    assert "git fetch origin main" in steps and "git rebase" in steps
    assert "rebase -X theirs" not in steps       # لا إخفاء لتعارض الحالة
    assert "[skip ci]" in steps
    # 3: نجاح الدورة / لا تغييرات / نجاح الدفع — ولا يُستخدم أي exit 0 كبوابة.
    assert steps.count("exit 0") == 3
    assert w4["permissions"]["actions"] == "read"   # حاجز التشغيل الفردي
    names = [str(s.get("name", "")) for s in w4["jobs"]["run-once"]["steps"]]
    assert any("Guard against concurrent 4H" in n for n in names)
    assert any("Refresh checkout to latest main" in n for n in names)
    for s in w4["jobs"]["run-once"]["steps"]:
        if s.get("name") == "Run NW 4H bot once":
            assert s.get("if") == "steps.guard.outputs.proceed == 'true'"
        if s.get("name") == "Commit NW 4H state updates":
            assert "always()" in str(s.get("if"))
    assert "for attempt in 1 2 3" in steps          # إعادة محاولة شبكية آمنة

    on = w4.get("on", w4.get(True))
    assert on["schedule"][0]["cron"] == "3 0,4,8,12,16,20 * * *"
    assert "workflow_dispatch" in on
    assert "push" not in on
    assert "workflow_run" not in on


def test_4h_report_workflow_is_report_only_and_midnight_riyadh():
    r = _workflow("nw-bot-4h-report.yml")
    on = r.get("on", r.get(True))
    assert on["schedule"][0]["cron"] == "10 21 * * *"   # 00:10 AM الرياض
    assert "workflow_dispatch" in on
    assert "push" not in on
    assert "workflow_run" not in on

    job = r["jobs"]["send-daily-report"]
    assert job["if"] == "vars.NW4H_ENABLED != 'false'"
    assert job["env"]["TELEGRAM_MUTE"] == "0"
    assert job["env"]["NW_STATE_FILE"] == "data/state_nw4h.json"
    assert job["env"]["NW_TRADE_LOG"] == "data/trades_log_nw4h.csv"
    assert r["concurrency"]["group"] != _workflow("nw-bot-4h.yml")["concurrency"]["group"]

    steps = "\n".join(s.get("run", "") for s in job["steps"])
    assert "python run_nw4h_report_once.py" in steps
    assert "run_nw4h_once.py" not in steps
    assert "git add data/state_nw4h.json" in steps
    assert "data/trades_log_nw4h.csv" not in steps   # الدفتر يُقرأ فقط لا يُCommit
    assert "[skip ci]" in steps
    assert steps.count("exit 0") == 2


def test_no_unguarded_4h_trigger_and_only_guarded_dispatch():
    """تأمين الجدولة (2026-11-09): لا تريغر قديم بلا حاجز.

    جدولة GitHub native فاتت دورات 4H، فأُضيف مُشغّل **محمي** واحد فقط
    (nw-bot-4h-guard.yml) يطلق البوت/Tقرير عبر ``{"ref":"main"}`` بعد فحص
    النافذة + التشغيلات النشطة + عُمر آخر تشغيل + ``last_report_day``.
    وركفلوا البوت والتقرير نفسيهما يظلان بلا ``workflow_run`` وبلا dispatch،
    ومع حاجز تشغيل فردي داخلي ⇒ لا تكرار رسائل.
    """
    wf_dir = ROOT / ".github/workflows"
    assert not (wf_dir / "nw-bot-4h-trigger.yml").exists()
    guard = wf_dir / "nw-bot-4h-guard.yml"
    assert guard.exists()
    for p in wf_dir.glob("*.yml"):
        if p.name == "nw-bot-4h-guard.yml":
            continue
        text = p.read_text(encoding="utf-8")
        assert "nw-bot-4h.yml/dispatches" not in text, p.name
        assert "nw-bot-4h-report.yml/dispatches" not in text, p.name
    for name in ("nw-bot-4h.yml", "nw-bot-4h-report.yml"):
        text = (wf_dir / name).read_text(encoding="utf-8")
        doc = _workflow(name)
        on = doc.get("on", doc.get(True))
        assert "workflow_run" not in on
        assert "push" not in on
        assert "PAT" not in text and "ghp_" not in text
        assert "Signal Bot Cron" not in text
        assert "فحص مفتاح التشغيل" not in text
    g = guard.read_text(encoding="utf-8")
    # المُشغّل يبني مسار dispatch من اسم الوركفلو داخل دالة واحدة.
    assert "dispatch(\"nw-bot-4h.yml\")" in g
    assert "dispatch(\"nw-bot-4h-report.yml\")" in g
    assert "/actions/workflows/{wf}/dispatches" in g
    assert '{"ref":"main"}' in g
    assert "PAT" not in g and "ghp_" not in g
    assert "nw-bot.yml/dispatches" not in g and "nw-bot-15m" not in g
    assert "data/state_nw.json" not in g and "data/state_nw15.json" not in g


def test_4h_state_and_log_are_isolated():
    s4 = json.loads((ROOT / "data/state_nw4h.json").read_text(encoding="utf-8"))
    assert s4["system"] == "nw-4h"
    assert s4["cycles"] == 0 and s4["messages_sent"] == 0
    assert s4["sent"] == [] and s4["last_cycle_ms"] == 0
    assert {"symbols", "prev_buy_bars", "prev_sell_bars", "buy_history",
            "cancelled_buy_bars"} <= set(s4)
    assert "daily_report_nw4h" not in s4

    header = (ROOT / "data/trades_log_nw4h.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == [
        "symbol", "entry_time", "entry_price", "exit_time", "exit_price",
        "gross_pct", "net_pct", "entries_count", "exit_reason",
        "signal_bar_time", "confirm_bar_time",
    ]
    assert (ROOT / "data/state_nw4h.json").read_bytes() != (ROOT / "data/state_nw15.json").read_bytes()
    assert (ROOT / "data/state_nw4h.json").read_bytes() != (ROOT / "data/state_nw.json").read_bytes()


def test_user_files_are_untouched():
    """حماية من أي تعديل على ملفات المستخدم وملفات 15M المُسكَتة.

    استثناء مُصرَّح به من المستخدم (2026-11-09): إصلاح جدولة فريم 1H في
    ``.github/workflows/nw-bot.yml`` + تحديث اختباري الحماية ذاتهما.
    """
    tracked = [
        "src/nw_messages.py", "src/nw_signals.py", "src/nw_state.py",
        "src/nw_envelope.py", "run_nw_once.py",
        ".github/workflows/nw-bot-trigger.yml",
        "tests/test_nw_messages.py", "tests/test_nw_envelope.py",
        "README_NW_SYSTEM.md", "README_NW.md", "handoff.md",
        "data/state_nw.json", "data/trades_log_nw.csv",
        "src/nw_15m_bot.py", "src/nw_15m_messages.py", "run_nw15_once.py",
        ".github/workflows/nw-bot-15m.yml", ".github/workflows/nw-bot-15m-trigger.yml",
        "data/state_nw15.json", "data/trades_log_nw15.csv",
    ]
    diff = subprocess.run(["git", "diff", "--name-only", "HEAD", "--", *tracked],
                          cwd=ROOT, capture_output=True, text=True, check=True).stdout
    assert diff.strip() == "", f"ملفات المستخدم/15M معدّلة: {diff}"
