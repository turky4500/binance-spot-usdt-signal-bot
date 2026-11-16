"""اختبارات نظام تجربة تتبّع الاتجاه (1H) — نظام مستقل وموازٍ.

تتحقق من: صحة القواعد، سببية المؤشرات، عزل المسارات، الجدولة،
وغياب أي محفّز push أو اعتماد على بوابة exit 0.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows"
HOURLY = WF / "nw-trend-experiment.yml"
REPORT = WF / "nw-trend-daily-report.yml"

from src import nw_trend as T  # noqa: E402


def _doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _on(path: Path) -> dict:
    doc = _doc(path)
    return doc.get("on") or doc.get(True) or {}


def _ramp(n: int = 240, start: float = 100.0, step: float = 0.15):
    t = np.arange(n, dtype=np.int64) * T.INTERVAL_MS + 1_700_000_000_000
    c = start + np.arange(n, dtype=float) * step
    h = c + 0.01
    l = c - 0.01
    return t, c, h, l


# ─────────────────────────────── ملفات الوركفلو والجدولة
@pytest.mark.parametrize("path", [HOURLY, REPORT])
def test_new_workflows_valid_yaml_and_no_push_trigger(path):
    assert isinstance(_doc(path), dict)
    assert "push" not in _on(path)
    assert "workflow_dispatch" in _on(path)
    text = path.read_text(encoding="utf-8")
    assert "فحص مفتاح التشغيل" not in text
    assert "ghp_" not in text and "PAT" not in text


def test_hourly_schedule_is_every_hour_at_minute_three():
    assert _on(HOURLY)["schedule"] == [{"cron": "3 * * * *"}]


def test_daily_report_schedule_is_2359_riyadh():
    # 23:59 الرياض = 20:59 UTC
    assert _on(REPORT)["schedule"] == [{"cron": "59 20 * * *"}]


@pytest.mark.parametrize("path", [HOURLY, REPORT])
def test_workflows_are_job_level_gated_and_isolated(path):
    doc = _doc(path)
    job = next(iter(doc["jobs"].values()))
    assert "NW_TREND_ENABLED" in str(job.get("if"))
    assert doc["permissions"]["contents"] == "write"
    assert doc["concurrency"]["group"] == "nw-trend-experiment"
    text = path.read_text(encoding="utf-8")
    for other in ("data/state_nw.json", "trades_log_nw.csv", "nw-bot.yml",
                  "state_nw4h", "state_sa", "state_qaa", "state_zigzag",
                  "state_daily_trend"):
        assert other not in text, other
    assert "data/nw_trend/" in text


def test_python_modules_touch_only_their_own_state():
    for rel in ("src/nw_trend.py", "tools/nw_trend_run.py", "tools/nw_trend_report.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for other in ("data/state_nw.json", "trades_log_nw.csv",
                      "state_daily_trend", "state_qaa", "state_sa", "state_zigzag",
                      "nw_bot", "ghp_"):
            assert other not in text, (rel, other)
        if rel.startswith("tools/"):
            assert "data/nw_trend" in text


def test_initial_state_is_isolated_and_well_formed():
    state = json.loads((ROOT / "data/nw_trend/state.json").read_text(encoding="utf-8"))
    assert state["system"] == "nw-trend-1h-experiment"
    assert state["runs"] == 0 and state["positions"] == {}
    assert state["last_bar_ms"] == {} and state["sent"] == []


# ─────────────────────────────── الكون
def test_only_pegged_stables_are_excluded():
    assert not T.is_excluded("HBARUSDT")
    assert not T.is_excluded("BTCUSDT")
    assert not T.is_excluded("1000SHIBUSDT")
    assert T.is_excluded("USDCUSDT")
    assert T.is_excluded("FDUSDUSDT")
    assert T.is_excluded("EURUSDT")
    assert T.is_excluded("ETHBUSD")  # ليس زوج USDT


# ─────────────────────────────── المؤشرات سببية
def test_indicators_are_causal_no_repaint():
    t, c, h, l = _ramp()
    a = T.indicators(h, l, c)
    c2, h2, l2 = c.copy(), h.copy(), l.copy()
    c2[-1] += 50.0
    h2[-1] += 100.0
    l2[-1] -= 10.0
    b = T.indicators(h2, l2, c2)
    for key in ("ema", "atr", "dh20", "dl10"):
        assert np.allclose(a[key][:-1], b[key][:-1], equal_nan=True), key
    assert b["ema"][-1] != a["ema"][-1]


def test_donchian_shifts_one_bar():
    t, c, h, l = _ramp()
    ind = T.indicators(h, l, c)
    j = 220
    assert np.isclose(ind["dh20"][j], h[j - 20:j].max())
    assert np.isclose(ind["dl10"][j], l[j - 10:j].min())


# ─────────────────────────────── قواعد الدخول والخروج
def test_entry_requires_breakout_above_ema_and_rising_slope():
    t, c, h, l = _ramp()
    ind = T.indicators(h, l, c)
    j = 220
    action, payload, pos = T.step(j, ind, t, c, l, None)
    assert action == "entry"
    assert payload["entry"] == float(c[j])
    assert payload["donchian20"] == float(ind["dh20"][j])
    assert payload["init_stop"] < payload["entry"]
    assert payload["trail"] >= payload["init_stop"]

    # كسر شرط واحد ⇒ لا دخول
    c_bad = c.copy()
    c_bad[j] = float(ind["dh20"][j]) - 1.0
    assert T.step(j, T.indicators(h, l, c_bad), t, c_bad, l, None)[0] is None

    h_flat = h.copy()
    h_flat[:] = 1000.0  # قمة 20 شمعة مستحيلة الاختراق
    assert T.step(j, T.indicators(h_flat, l, c), t, c, l, None)[0] is None


def test_trail_only_moves_up_and_exit_fills_at_trail():
    t, c, h, l = _ramp()
    ind = T.indicators(h, l, c)
    j = 221
    pos = {"entry": 100.0, "entry_bar_ms": int(t[j - 1]), "trail": 95.0}
    l_low = l.copy()
    l_low[j] = 1.0  # low يلمس خط التتبّع
    action, payload, newpos = T.step(j, ind, t, c, l_low, pos)
    # dl10 عند j أعلى من 95 ⇒ trail يرتفع، والخروج يُملأ عنده
    assert action == "exit"
    assert payload["trail"] >= 95.0
    assert payload["exit"] == payload["trail"]
    assert newpos is None
    assert payload["net_pct"] == round(payload["gross_pct"] - T.FEE_PCT, 4)

    # لو لم يلمس low الخط ⇒ لا خروج، ويُحتفظ بالـtrail الأعلى
    l_high = l.copy()
    l_high[j] = 1e9
    action2, _, newpos2 = T.step(j, ind, t, c, l_high, pos)
    assert action2 is None and newpos2 is not None
    assert newpos2["trail"] >= pos["trail"]


def test_no_entry_while_position_is_open():
    t, c, h, l = _ramp()
    ind = T.indicators(h, l, c)
    j = 220
    pos = {"entry": 100.0, "entry_bar_ms": int(t[j - 2]), "trail": 1.0}
    l_high = l.copy()
    l_high[j] = 1e9
    action, _, newpos = T.step(j, ind, t, c, l_high, pos)
    assert action is None and newpos is not None


# ─────────────────────────────── الرسائل والمقاييس
def test_messages_contain_strategy_details_and_disclaimer():
    pos = {"entry": 0.07921, "ema200": 0.076354, "donchian20": 0.07893,
           "atr14": 0.000712, "init_stop": 0.07743, "trail": 0.07743,
           "entry_bar_ms": 1_700_000_000_000, "donchian10_at_entry": 0.07694}
    msg = T.entry_message("HBARUSDT", 1_700_000_000_000, pos)
    assert "EMA200" in msg and "20 شمعة" in msg and "ATR(14)" in msg
    assert "خط التتبّع" in msg and "لا هدف ربح ثابت" in msg
    assert "ليست" in msg and "نصيحة استثمارية" in msg
    p = {"entry": 0.07921, "entry_bar_ms": 1_700_000_000_000,
         "exit": 0.07830, "net_pct": -1.349, "holding_bars": 12}
    out = T.exit_message("HBARUSDT", 1_700_003_600_000, p)
    assert "صافي" in out and "0.2% عمولة" in out and "12 ساعة" in out


def test_metrics_handle_empty_and_mixed_trades():
    assert T.metrics([])["trades"] == 0
    rows = [{"net_pct": 9.0, "holding_bars": 10},
            {"net_pct": -1.5, "holding_bars": 4},
            {"net_pct": -1.5, "holding_bars": 6}]
    m = T.metrics(rows)
    assert m["trades"] == 3 and m["wins"] == 1 and m["losses"] == 2
    assert m["win_rate_pct"] == 33.33
    assert m["payoff"] == 6.0
    assert m["total_additive_net_pct"] == 6.0
