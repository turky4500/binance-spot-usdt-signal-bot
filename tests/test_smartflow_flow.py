"""اختبارات دورة بوت Smart Flow — كائنات وهمية بلا شبكة، بملفات حالة مؤقتة."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import smartflow_bot as sf_bot  # noqa: E402

H = 3_600_000
BASE = 1_699_999_200_000  # محاذاة لشبكة الساعة UTC
SYMBOL = "AAAUSDT"


def make_df(rows):
    n = len(rows)
    return pd.DataFrame({
        "open_time": [BASE + i * H for i in range(n)],
        "close_time": [BASE + (i + 1) * H - 1 for i in range(n)],
        "open": [float(r[0]) for r in rows],
        "high": [float(r[1]) for r in rows],
        "low": [float(r[2]) for r in rows],
        "close": [float(r[3]) for r in rows],
    })


def rising_breakout_rows():
    """تسطيح ثم هبوط ثم شمعة اختراق كبيرة — شراء (فرضية v1.1)."""
    rows = []
    c = 100.0
    for i in range(188):
        delta = 0.3 if i % 2 == 0 else -0.3
        o, cl = c, c + delta
        rows.append((o, max(o, cl) + 0.1, min(o, cl) - 0.1, cl))
        c = cl
    for _ in range(10):
        o, cl = c, c - 0.5
        rows.append((o, o + 0.1, cl - 0.3, cl))
        c = cl
    o, cl = c, c + 8.0
    rows.append((o, cl + 0.5, o - 0.2, cl))
    return rows


def flat_rows(n: int = 200):
    rows = []
    c = 100.0
    for i in range(n):
        delta = 0.3 if i % 2 == 0 else -0.3
        o, cl = c, c + delta
        rows.append((o, max(o, cl) + 0.1, min(o, cl) - 0.1, cl))
        c = cl
    return rows


class StubBinance:
    """عميل وهمي: يرجع أول k شمعة من الإطار المخزّن ووقت تخادم يضبطه الاختبار."""

    def __init__(self, frames: dict, k: int) -> None:
        self.frames = frames
        self.k = k
        self.server_time = 0

    def get_server_time(self) -> int:
        return self.server_time

    def get_klines_for_symbols(self, symbols, interval, limit):
        out = {}
        for s in symbols:
            df = self.frames.get(s)
            out[s] = df.iloc[: self.k].copy() if df is not None else pd.DataFrame()
        return out


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTFLOW_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("SMARTFLOW_HALAL_DIR", str(tmp_path / "halal"))
    monkeypatch.setenv("SMARTFLOW_SIGNALS_LOG", str(tmp_path / "signals.csv"))
    monkeypatch.setenv("SMARTFLOW_DRY_RUN", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")
    b = sf_bot.SmartFlowSignalBot(sf_bot.AppConfig.from_env())
    b.sent = []
    monkeypatch.setattr(b, "_send", lambda text: b.sent.append(text))
    monkeypatch.setattr(b, "get_halal_verdict", lambda _s: "حلال ✅")
    return b


def test_signal_flow_sends_buy_and_logs(tmp_path, bot):
    frame = {"AAAUSDT": make_df(rising_breakout_rows())}
    stub = StubBinance(frame, k=200)
    bot.binance = stub
    bot.symbols = [SYMBOL]
    target_open = BASE + 198 * H
    stub.server_time = target_open + H + 60_000

    bot.process_new_closed_hour(stub.server_time)

    assert len(bot.sent) == 1
    text = bot.sent[0]
    assert "شراء" in text and SYMBOL in text and "قيد المعايرة" in text and "حلال" in text
    assert bot.state["last_processed_open_time"] == target_open
    assert bot.state["last_signal"][SYMBOL]["kind"] == "buy"

    log_path = tmp_path / "signals.csv"
    with log_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0][:4] == ["time_local", "time_ms", "symbol", "kind"]
    assert len(rows) == 2 and rows[1][2] == SYMBOL and rows[1][3] == "buy"

    # إعادة نفس الدورة: الشمعة نفسها لا تُعالَج مرتين
    bot.process_new_closed_hour(stub.server_time)
    assert len(bot.sent) == 1

    # شمعة هادئة تالية: لا إشارة والمؤشر يتقدم
    quiet = rising_breakout_rows() + [(103.0, 104.0, 102.0, 102.5)]
    stub.frames = {SYMBOL: make_df(quiet)}
    stub.k = 200
    stub.server_time += H
    bot.process_new_closed_hour(stub.server_time)
    assert bot.state["last_processed_open_time"] == target_open + H
    assert len(bot.sent) == 1


def test_no_signal_on_flat_market(tmp_path, bot):
    stub = StubBinance({SYMBOL: make_df(flat_rows(200))}, k=200)
    bot.binance = stub
    bot.symbols = [SYMBOL]
    stub.server_time = BASE + 198 * H + H + 60_000

    bot.process_new_closed_hour(stub.server_time)

    assert bot.sent == []
    assert bot.state["last_processed_open_time"] == BASE + 198 * H
    assert (tmp_path / "signals.csv").exists() is False


def test_send_allowed_on_flip_and_cooldown(bot):
    bot.state["last_signal"] = {
        SYMBOL: {"kind": "buy", "bar_open_time": BASE + 10 * H},
    }
    # نفس الاتجاه داخل المهلة → ممنوع
    assert bot.send_allowed(SYMBOL, "buy", BASE + 12 * H) is False
    # انقلاب الاتجاه → مسموح دائمًا
    assert bot.send_allowed(SYMBOL, "sell", BASE + 11 * H) is True
    # رمز بلا إشارة سابقة → مسموح
    assert bot.send_allowed("BBBUSDT", "buy", BASE + 11 * H) is True
    # نفس الاتجاه بعد انصراف المهلة (6 شموع) → مسموح
    assert bot.send_allowed(SYMBOL, "buy", BASE + 16 * H) is True
    # نمط every_bar يتجاوز كل القيود
    bot.settings.signal_mode = "every_bar"
    assert bot.send_allowed(SYMBOL, "buy", BASE + 11 * H) is True


def test_state_persists_between_cycles(tmp_path, bot):
    frame = {"AAAUSDT": make_df(rising_breakout_rows())}
    stub = StubBinance(frame, k=200)
    bot.binance = stub
    bot.symbols = [SYMBOL]
    stub.server_time = BASE + 198 * H + H + 60_000
    bot.process_new_closed_hour(stub.server_time)
    assert len(bot.sent) == 1

    # كائن جديد على نفس ملف الحالة (بلا إشارة جديدة) لا يكرر الرسالة القديمة
    b2 = sf_bot.SmartFlowSignalBot(sf_bot.AppConfig.from_env())
    b2.sent = []
    b2._send = lambda text: b2.sent.append(text)
    b2.get_halal_verdict = lambda _s: "حلال ✅"
    b2.binance = stub
    b2.symbols = [SYMBOL]
    b2.process_new_closed_hour(stub.server_time)
    assert b2.sent == []
    assert b2.state["last_signal"][SYMBOL]["kind"] == "buy"
