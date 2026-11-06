"""regression للقطة SNDKBUSDT (2026-11-06) — الانتقال إلى الوضع السببي.

المستخدم أشار بسهم إلى ▲ عند **09:00 صباحًا** على شارت «Repainting Mode
Enabled»، بينما البوت أرسل توصية نسبها إلى شمعة **12:00 ظهرًا**. التشخيص:
تصحيح الحافة (`boundary_correction`) غيّر منحنى Pine فأخفى ▲ الحقيقية عند
09:00 (عمق 0.0028143%) وصنع قاعًا ضحلًا عند 12:00 (0.0001646%).

القرار: الوضع السببي `repaint=false` من **نفس** مؤشر LuxAlgo، مع تطبيق قاعدة
المثلثات ذاتها (`d*y1_d<0`) على الخط السببي. النتيجة على شموع Binance الفعلية:
▲ عند 09:00، وشمعة التأكيد 10:00 خضراء `855.70 → 859.76`، والخط لا يتغير
تاريخيًا أبدًا عند إضافة شموع جديدة.

الملف tests/fixtures/sndkb_1h_2026-11-06.json = 600 شمعة ساعية فعلية.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
import yaml

from nw_bot import NWBot, build_nw_bot
from src.nw_signals import NWSignalConfig, candles_closed, evaluate_symbol
from src.nw_state import NWStateStore
from src.nw_strategy import endpoint_estimator, endpoint_flip_signals

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "sndkb_1h_2026-11-06.json"
RIYADH = ZoneInfo("Asia/Riyadh")
SIGNAL_09 = 1_793_944_800_000       # 09:00 ص — ▲ التي أشار إليها المستخدم
CONFIRM_10 = 1_793_948_400_000      # 10:00 ص — الشمعة التالية الخضراء
NOON_12 = 1_793_955_600_000         # 12:00 م — شمعة التوصية الخاطئة
NOW_11_05 = 1_793_952_300_000       # 11:05 ص — آخر شمعة مغلقة = 10:00 ص
NOW_14_03 = 1_793_962_980_000       # 02:03 م — دورة البوت التي أرسلت SNDKBUSDT

ENDPOINT = lambda **kw: NWSignalConfig(mode="endpoint", **kw)


def frame(now_ms: int = NOW_11_05, red_confirm: bool = False) -> pd.DataFrame:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    df = pd.DataFrame([{
        "open_time": r[0], "open": r[1], "high": max(r[1], r[2]),
        "low": min(r[1], r[2]), "close": r[2], "close_time": r[3],
    } for r in rows])
    if red_confirm:
        i = df.index[df["open_time"] == CONFIRM_10][0]
        df.loc[i, "open"] = df.loc[i, "close"] + 1.0
    return df[df["close_time"] < now_ms]


class FakeClient:
    def __init__(self, df: pd.DataFrame, now_ms: int = NOW_11_05):
        self.df, self.now_ms = df, now_ms

    def get_server_time(self):
        return self.now_ms

    def get_klines_for_symbols(self, symbols, interval, limit):
        return {s: self.df for s in symbols}


class FakeTelegram:
    def __init__(self):
        self.messages: list[str] = []

    def send_message(self, text):
        self.messages.append(text)


def make_bot(tmp_path, cfg, df=None, now_ms=NOW_11_05):
    tg = FakeTelegram()
    store = NWStateStore(str(tmp_path / "state.json"))
    bot = NWBot(
        client=FakeClient(df if df is not None else frame(now_ms), now_ms),
        telegram=tg,
        state_store=store,
        cfg=cfg,
        state_path=str(tmp_path / "state.json"),
        trade_log=str(tmp_path / "trades.csv"),
        show_repaint_warning=False,
    )
    return bot, tg, store


# ── 1) المطابقة مع لقطة المستخدم ────────────────────────────────────────────

def test_endpoint_mode_finds_the_9am_signal_and_10am_entry():
    """▲ عند 09:00، الشمعة التالية 10:00 خضراء، السعر 859.76."""
    o, c, t = candles_closed(frame(), NOW_11_05)
    assert len(c) >= 500
    assert datetime.fromtimestamp(t[-1] / 1000, RIYADH).strftime("%I:%M %p") == "10:00 AM"

    res = evaluate_symbol(o, c, t, ENDPOINT())
    assert res["ok"] is True and res["mode"] == "endpoint"
    assert len(res["buy"]) == 1, res
    rec = res["buy"][0]
    assert rec["bar_time"] == SIGNAL_09                 # لا 12 ظهرًا
    assert rec["confirm_bar_time"] == CONFIRM_10
    assert rec["confirm_green"] is True
    assert rec["confirm_close"] == pytest.approx(859.76)
    assert rec["signal_price"] == pytest.approx(855.63)
    assert rec["depth_pct"] > 0.0001
    assert res["ignored_retro"] == 0                 # لا إعادة رسم أصلًا


def test_boundary_corrected_repaint_kernel_was_the_source_of_the_noon_label():
    """يُوثّق الخطأ: النواة المعدّلة نقلت ▲ إلى 12:00 ظهرًا في دورة 02:03 م."""
    o, c, t = candles_closed(frame(NOW_14_03), NOW_14_03)
    legacy = evaluate_symbol(
        o, c, t,
        NWSignalConfig(mode="repaint", boundary_correction=True,
                       min_trough_pct=0.0001),
    )
    assert len(legacy["buy"]) == 1
    assert legacy["buy"][0]["bar_time"] == NOON_12

    # الوضع السببي لا يرسل بأثر رجعي عن شمعة مضت عليها ساعات.
    assert evaluate_symbol(o, c, t, ENDPOINT())["buy"] == []


def test_exact_pine_kernel_agrees_with_the_screenshot_at_2pm():
    """بدون تصحيح الحافة، معادلة Pine الأصلية تعطي ▲ عند 09:00 أيضًا."""
    o, c, t = candles_closed(frame(NOW_14_03), NOW_14_03)
    raw = evaluate_symbol(
        o, c, t,
        NWSignalConfig(mode="repaint", boundary_correction=False,
                       min_trough_pct=0.0, max_age_bars=8),
    )
    labels = [r["bar_time"] for r in raw["buy"]]
    assert SIGNAL_09 in labels
    assert NOON_12 not in labels


# ── 2) السببية: لا إعادة رسم ───────────────────────────────────────────────

def test_endpoint_line_and_labels_never_change_when_new_bars_arrive():
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    c = np.array([r[2] for r in rows], dtype=float)
    prefix = c[:502]                       # حتى شمعة 10:00 ص
    a = endpoint_estimator(prefix)
    b = endpoint_estimator(c)
    assert np.nanmax(np.abs(a - b[:len(prefix)])) == 0.0

    fa = endpoint_flip_signals(a, prefix)
    fb = [s for s in endpoint_flip_signals(b, c) if s["index"] < len(prefix)]
    assert [(s["index"], s["kind"]) for s in fa] == [(s["index"], s["kind"]) for s in fb]


def test_signal_does_not_reappear_in_later_cycles(tmp_path):
    """نفس ▲ لا تُرسل مرتين عند وصول شموع جديدة (التسمية لا تتحرك)."""
    bot, tg, _ = make_bot(tmp_path, ENDPOINT())
    first = bot.run_once(universe=["SNDKBUSDT"])
    assert first["entry"] == 1 and len(tg.messages) == 1

    bot.client.df, bot.client.now_ms = frame(NOW_14_03), NOW_14_03
    second = bot.run_once(universe=["SNDKBUSDT"])
    assert second["entry"] == 0
    assert len(tg.messages) == 1


# ── 3) شرطا المستخدم بلا تغيير ─────────────────────────────────────────────

def test_red_confirmation_cancels_permanently_in_endpoint_mode(tmp_path):
    bot, tg, store = make_bot(tmp_path, ENDPOINT(), frame(red_confirm=True))
    s = bot.run_once(universe=["SNDKBUSDT"])
    assert s["entry"] == 0 and s["cancelled"] >= 1
    assert tg.messages == []
    assert SIGNAL_09 in store.load()["cancelled_buy_bars"]["SNDKBUSDT"]

    bot.client.df = frame()              # عودة الشمعة الخضراء لا تُ resurrect الإشارة
    assert bot.run_once(universe=["SNDKBUSDT"])["entry"] == 0
    assert tg.messages == []


def test_entry_message_uses_entry_candle_time_and_price(tmp_path):
    bot, tg, _ = make_bot(tmp_path, ENDPOINT())
    assert bot.run_once(universe=["SNDKBUSDT"])["entry"] == 1
    m = tg.messages[0]
    assert "📥 توصية دخول — SNDKBUSDT" in m
    assert "🕐 فريم الساعة (1H) · Nadaraya" in m
    assert "💰 سعر التوصية: 859.76" in m
    assert "🕒 2026-11-06 10:00 AM" in m        # وقت شمعة الدخول لا وقت الإرسال
    assert "12:00 PM" not in m
    assert "\n\n" in m


# ── 4) الإعداد الحيّ ────────────────────────────────────────────────────────

def test_workflow_runs_causal_mode_without_boundary_kernel():
    doc = yaml.safe_load((ROOT / ".github/workflows/nw-bot.yml").read_text(encoding="utf-8"))
    env = next(iter(doc["jobs"].values()))["env"]
    assert env["NW_MODE"] == "endpoint"
    assert env["NW_BOUNDARY_CORRECT"] == "0"
    assert env["NW_SIGNAL_MAX_AGE_BARS"] == "1"
    assert env["NW_INCLUDE_ALL"] == "1"


def test_build_nw_bot_defaults_to_endpoint(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MUTE", "1")
    monkeypatch.delenv("NW_MODE", raising=False)
    assert build_nw_bot().cfg.mode == "endpoint"


def test_repaint_mode_still_available_for_research():
    assert NWSignalConfig().mode == "repaint"
    with pytest.raises(ValueError):
        NWSignalConfig(mode="something_else")
