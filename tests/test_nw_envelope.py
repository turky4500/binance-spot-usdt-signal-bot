"""النقل الحرفي لـ «Nadaraya-Watson Envelope [LuxAlgo]» الذي أرسله المستخدم.

لا يُختبر هنا أي سلوك مبتكر: تُقارن المعادلات والشروط بسطر الكود المقابل في
Pine، ثم يُختبر شرط المستخدم الوحيد (الشمعة التالية خضراء ⇒ إرسال / حمراء ⇒
إلغاء نهائي). ملف الشموع هو نفسه المستخدم في اللقطات: SNDKBUSDT 1H.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from nw_bot import build_nw_bot
from src.nw_envelope import EnvelopeConfig, envelope_curve, envelope_labels, gauss
from src.nw_signals import NWSignalConfig, candles_closed, evaluate_symbol

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "sndkb_1h_2026-11-06.json"
ONE_AM = 1_793_916_000_000
CYCLE_0503 = ONE_AM + 4 * 3_600_000 + 180_000        # 06 نوفمبر 05:03 ص
CYCLE_0603 = ONE_AM + 5 * 3_600_000 + 180_000        # 06 نوفمبر 06:03 ص


def src_at(now_ms: int) -> np.ndarray:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return np.array([r[2] for r in rows if r[3] < now_ms][::-1], dtype=float)


def frame_at(now_ms: int) -> pd.DataFrame:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return pd.DataFrame([{
        "open_time": r[0], "open": r[1], "high": max(r[1], r[2]),
        "low": min(r[1], r[2]), "close": r[2], "close_time": r[3],
    } for r in rows if r[3] < now_ms])


# ── 1) المعادلات كما وردت في الكود المرسل ───────────────────────────────────

def test_gauss_is_the_pine_gaussian_window():
    assert gauss(0, 8) == pytest.approx(1.0)
    assert gauss(8, 8) == pytest.approx(np.exp(-0.5))
    assert gauss(-8, 8) == pytest.approx(np.exp(-0.5))


def test_sae_divides_by_499_not_500():
    """سطر Pine: sae := sae / math.min(499, n - 1) * mult."""
    src = src_at(CYCLE_0503)
    cfg = EnvelopeConfig()
    sae, _ = envelope_labels(src, cfg)
    nwe = envelope_curve(src, cfg)
    expected = np.abs(src[:500] - nwe).sum() / 499 * 3.0
    assert sae == pytest.approx(expected)
    assert sae != pytest.approx(np.abs(src[:500] - nwe).sum() / 500 * 3.0)


def test_label_conditions_are_the_pine_inequalities():
    """if src[i] < nwe[i]-sae and src[i+1] > nwe[i]-sae ⇒ ▲ وهكذا."""
    src = src_at(CYCLE_0503)
    cfg = EnvelopeConfig()
    sae, labels = envelope_labels(src, cfg)
    nwe = envelope_curve(src, cfg)
    assert labels
    for L in labels:
        i = L["bars_ago"]
        lower, upper = nwe[i] - sae, nwe[i] + sae
        if L["kind"] == "buy":
            assert src[i] < lower and src[i + 1] > lower
            assert L["band"] == pytest.approx(lower)
        else:
            assert src[i] > upper and src[i + 1] < upper
            assert L["band"] == pytest.approx(upper)
        assert L["price"] == pytest.approx(src[i])


def test_label_is_drawn_on_bar_n_minus_i():
    src = src_at(CYCLE_0503)
    _, labels = envelope_labels(src)
    for L in labels:
        assert L["bars_ago"] >= 0 and L["bars_ago"] < 500
        assert L["price"] == pytest.approx(src[L["bars_ago"]])


# ── 2) سلوك الطباعة على الشارت (إعادة الرسم) ────────────────────────────────

def test_labels_appear_then_stay_on_the_same_bar():
    """▲ على 1:00 ص تظهر أول مرة في دورة 06:03 ص ثم تبقى على نفس الشمعة."""
    def find(now):
        src = src_at(now)
        _, labs = envelope_labels(src)
        rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
        times = [r[0] for r in rows if r[3] < now][::-1]
        return [(L["bars_ago"], L["price"]) for L in labs if times[L["bars_ago"]] == ONE_AM]
    assert find(CYCLE_0503) == []
    first = find(CYCLE_0603)
    assert first and first[0][0] == 4                 # ظهرت بأثر رجعي عند i=4
    second = find(CYCLE_0603 + 3_600_000)
    assert second[0][0] == first[0][0] + 1           # نفس الشمعة، تقدّم i فقط
    assert second[0][1] == first[0][1]               # السعر لا يتغير


# ── 3) شرط المستخدم فوق تسميات المؤشر ──────────────────────────────────────

def test_new_label_with_green_next_candle_produces_the_recommendation():
    o, c, t = candles_closed(frame_at(CYCLE_0503), CYCLE_0503)
    res = evaluate_symbol(o, c, t, NWSignalConfig(mode="envelope"))
    assert res["ok"] is True and res["mode"] == "envelope"
    assert res["labels_new"] >= 1
    assert len(res["buy"]) == 1
    rec = res["buy"][0]
    assert rec["confirm_bar_time"] - rec["bar_time"] == 3_600_000
    assert rec["confirm_green"] is True
    assert rec["confirm_close"] == pytest.approx(841.34)
    # ▲ تعني أن السعر عبر تحت الشريط السفلي: src[i] < nwe[i] - sae
    assert rec["sae"] > 0 and rec["band"] > rec["signal_price"]


def test_red_next_candle_cancels_the_label_permanently():
    df = frame_at(CYCLE_0503)
    o, c, t = candles_closed(df, CYCLE_0503)
    rec = evaluate_symbol(o, c, t, NWSignalConfig(mode="envelope"))["buy"][0]
    i = int(np.flatnonzero(t == rec["confirm_bar_time"])[0])
    o_red = o.copy()
    o_red[i] = c[i] + 1.0                            # نفس الشمعة لكن حمراء
    res = evaluate_symbol(o_red, c, t, NWSignalConfig(mode="envelope"))
    assert res["buy"] == []
    assert any(x["bar_time"] == rec["bar_time"] for x in res["cancelled_buy"])


def test_sell_label_needs_no_green_candle():
    o, c, t = candles_closed(frame_at(CYCLE_0503), CYCLE_0503)
    res = evaluate_symbol(o, c, t, NWSignalConfig(mode="envelope"))
    assert len(res["sell"]) == 1
    assert res["sell"][0]["kind"] == "sell"


def test_invented_filters_cannot_change_the_envelope_labels():
    """لا تصحيح حافة ولا عتبة ضجيج ولا حدّ عمر يلمس تسميات المؤشر المرسل."""
    o, c, t = candles_closed(frame_at(CYCLE_0503), CYCLE_0503)
    plain = evaluate_symbol(o, c, t, NWSignalConfig(mode="envelope"))
    loaded = evaluate_symbol(o, c, t, NWSignalConfig(
        mode="envelope", boundary_correction=True, min_trough_pct=1.0,
        max_age_bars=99, require_newest_green=True))
    assert [r["bar_time"] for r in loaded["buy"]] == [r["bar_time"] for r in plain["buy"]]
    assert [r["bar_time"] for r in loaded["sell"]] == [r["bar_time"] for r in plain["sell"]]


# ── 4) الإعداد الحيّ يطابق مدخلات Pine ──────────────────────────────────────

def test_workflow_uses_only_the_pine_inputs():
    doc = yaml.safe_load((ROOT / ".github/workflows/nw-bot.yml").read_text(encoding="utf-8"))
    env = next(iter(doc["jobs"].values()))["env"]
    assert env["NW_MODE"] == "envelope"
    assert env["NW_BANDWIDTH"] == "8"       # input.float(8.,'Bandwidth')
    assert env["NW_MULT"] == "3"            # input.float(3., minval=0)
    assert env["NW_WINDOW"] == "500"        # max_bars_back=500
    assert int(env["NW_KLINE_LIMIT"]) >= 502
    for removed in ("NW_BOUNDARY_CORRECT", "NW_MIN_TROUGH_PCT", "NW_SIGNAL_MAX_AGE_BARS"):
        assert removed not in env


def test_build_nw_bot_defaults_to_the_envelope(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MUTE", "1")
    for k in ("NW_MODE", "NW_MULT", "NW_KLINE_LIMIT"):
        monkeypatch.delenv(k, raising=False)
    bot = build_nw_bot()
    assert bot.cfg.mode == "envelope" and bot.cfg.mult == pytest.approx(3.0)
    assert bot.kline_limit >= 502


def test_no_invented_pine_mirror_is_left():
    assert not (ROOT / "tools" / "nw_endpoint_arrows.pine").exists()
