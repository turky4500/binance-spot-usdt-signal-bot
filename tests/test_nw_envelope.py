"""النقل الحرفي لـ «Nadaraya-Watson Envelope [LuxAlgo]» الذي أرسله المستخدم.

لا يُختبر هنا أي سلوك مبتكر: تُقارن المعادلات والشروط بسطر الكود المقابل في
Pine، ثم يُختبر شرط المستخدم الوحيد (الشمعة التالية خضراء ⇒ إرسال / حمراء ⇒
إلغاء نهائي). ملف الشموع هو نفسه المستخدم في اللقطات: SNDKBUSDT 1H.

§ جديد: بلاغ المستخدم «WLDUSDT آخر إشارة دخول لها 30 أكتوبر وأرسلتها الآن»
مُثبّت كاختبار انحدار دائم في ``test_wldusdt_october_label_is_never_sent_today``.
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

from nw_bot import build_nw_bot
from src.nw_envelope import EnvelopeConfig, envelope_curve, envelope_labels, gauss
from src.nw_signals import NWSignalConfig, candles_closed, evaluate_symbol

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "sndkb_1h_2026-11-06.json"
WLD_FIXTURE = Path(__file__).parent / "fixtures" / "wld_1h_2026-11-06.json"
ONE_AM = 1_793_916_000_000
CYCLE_0503 = ONE_AM + 4 * 3_600_000 + 180_000        # 06 نوفمبر 05:03 ص
CYCLE_0603 = ONE_AM + 5 * 3_600_000 + 180_000        # 06 نوفمبر 06:03 ص
LIVE_NOW = 1_793_986_559_978                          # لحظة الدورة الحيّة 06 نوفمبر 8:35 م
RIYADH = ZoneInfo("Asia/Riyadh")


def src_at(now_ms: int) -> np.ndarray:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return np.array([r[2] for r in rows if r[3] < now_ms][::-1], dtype=float)


def frame(rows: list, now_ms: int) -> pd.DataFrame:
    return pd.DataFrame([{
        "open_time": r[0], "open": r[1], "high": max(r[1], r[2]),
        "low": min(r[1], r[2]), "close": r[2], "close_time": r[3],
    } for r in rows if r[3] < now_ms])


def sndk_frame(now_ms: int) -> pd.DataFrame:
    return frame(json.loads(FIXTURE.read_text(encoding="utf-8")), now_ms)


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


# ── 3) توقيت القرار: شرط المستخدم، بلا مقارنة لقطات ────────────────────────

def _synthetic(n: int = 520, green: bool = True):
    t = np.array([1_700_000_000_000 + k * 3_600_000 for k in range(n)], dtype=np.int64)
    c = np.linspace(100.0, 110.0, n)
    o = c - 0.1
    o[-1] = c[-1] - 0.5 if green else c[-1] + 0.5
    return o, c, t


def _label(i: int, kind: str, price: float = 99.0) -> dict:
    return {"bars_ago": i, "kind": kind, "price": price,
            "band": price + (1.0 if kind == "buy" else -1.0),
            "nwe": price, "sae": 1.0}


def _inject(monkeypatch, labels):
    calls = []

    def fake(src, cfg):
        calls.append(1)
        return 1.0, list(labels)

    monkeypatch.setattr("src.nw_signals.envelope_labels", fake)
    return calls


def test_only_the_label_whose_next_candle_just_closed_is_evaluated(monkeypatch):
    """تسمية i=1 ⇒ شمعة التأكيد هي آخر مغلقة؛ التسميات الأقدم لا تُقيَّم."""
    o, c, t = _synthetic()
    calls = _inject(monkeypatch, [_label(1, "buy"), _label(185, "buy"),
                                  _label(296, "sell"), _label(0, "buy")])
    res = evaluate_symbol(o, c, t, NWSignalConfig(mode="envelope"))
    assert len(calls) == 1                     # لقطة واحدة فقط: لا مقارنة لقطات
    assert res["ok"] is True and res["mode"] == "envelope"
    assert res["labels_total"] == 4 and res["labels_due"] == 1
    assert len(res["buy"]) == 1
    rec = res["buy"][0]
    assert rec["bars_ago"] == 1
    assert rec["confirm_bar_time"] == int(t[-1])
    assert rec["bar_time"] == int(t[-2])
    assert rec["confirm_green"] is True
    assert res["sell"] == [] and res["cancelled_buy"] == []


def test_red_next_candle_cancels_the_label_permanently(monkeypatch):
    o, c, t = _synthetic(green=False)
    _inject(monkeypatch, [_label(1, "buy")])
    res = evaluate_symbol(o, c, t, NWSignalConfig(mode="envelope"))
    assert res["buy"] == []
    assert len(res["cancelled_buy"]) == 1
    assert res["cancelled_buy"][0]["confirm_bar_time"] == int(t[-1])


def test_sell_label_needs_no_green_candle(monkeypatch):
    o, c, t = _synthetic(green=False)
    _inject(monkeypatch, [_label(1, "sell")])
    res = evaluate_symbol(o, c, t, NWSignalConfig(mode="envelope"))
    assert len(res["sell"]) == 1 and res["sell"][0]["kind"] == "sell"


def test_invented_filters_cannot_change_the_envelope_labels(monkeypatch):
    """لا تصحيح حافة ولا عتبة ضجيج ولا حدّ عمر يلمس تسميات المؤشر المرسل."""
    o, c, t = _synthetic()
    _inject(monkeypatch, [_label(1, "buy"), _label(50, "buy")])
    plain = evaluate_symbol(o, c, t, NWSignalConfig(mode="envelope"))
    loaded = evaluate_symbol(o, c, t, NWSignalConfig(
        mode="envelope", boundary_correction=True, min_trough_pct=1.0,
        max_age_bars=99, require_newest_green=True))
    assert [r["bar_time"] for r in loaded["buy"]] == [r["bar_time"] for r in plain["buy"]]
    assert [r["bar_time"] for r in loaded["sell"]] == [r["bar_time"] for r in plain["sell"]]


# ── 4) بلاغ المستخدم: WLDUSDT ───────────────────────────────────────────────

def test_wldusdt_october_label_is_never_sent_today():
    """«آخر إشارة دخول لها على المؤشر كانت 30 أكتوبر وأنت أرسلتها الآن»."""
    rows = json.loads(WLD_FIXTURE.read_text(encoding="utf-8"))
    o, c, t = candles_closed(frame(rows, LIVE_NOW), LIVE_NOW)
    assert len(c) >= 502
    _, labels = envelope_labels(c[::-1], EnvelopeConfig())
    buys = [L for L in labels if L["kind"] == "buy"]
    assert buys
    times = [r[0] for r in rows if r[3] < LIVE_NOW][::-1]
    newest = min(buys, key=lambda L: L["bars_ago"])
    stamp = datetime.fromtimestamp(times[newest["bars_ago"]] / 1000, RIYADH)
    assert stamp.strftime("%d %b") == "30 Oct"       # آخر ▲ فعلًا في 30 أكتوبر
    assert newest["bars_ago"] > 1                     # شمعة تأكيدها أُغلقت قديمًا

    res = evaluate_symbol(o, c, t, NWSignalConfig(mode="envelope"))
    assert res["ok"] is True
    assert res["labels_due"] == 0
    assert res["buy"] == [] and res["sell"] == [] and res["cancelled_buy"] == []


def test_wldusdt_old_labels_stay_inert_even_with_a_fresh_label(monkeypatch):
    """وجود تسمية حديثة لا يبرّر إرسال التسميات القديمة معها."""
    rows = json.loads(WLD_FIXTURE.read_text(encoding="utf-8"))
    o, c, t = candles_closed(frame(rows, LIVE_NOW), LIVE_NOW)
    _, labels = envelope_labels(c[::-1], EnvelopeConfig())
    old = [L for L in labels if L["bars_ago"] > 1]
    _inject(monkeypatch, old + [_label(1, "buy")])
    res = evaluate_symbol(o, c, t, NWSignalConfig(mode="envelope"))
    assert len(res["buy"]) == 1 and res["buy"][0]["bars_ago"] == 1


# ── 5) الإعداد الحيّ يطابق مدخلات Pine ──────────────────────────────────────

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
