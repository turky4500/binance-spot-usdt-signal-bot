"""المرآة المرئية على TradingView تستخدم نفس قاعدة البوت السببي بالحرف.

المستخدم لاحظ (2026-11-06) أنه لا يجد ▲ خضراء على شارت TradingView تقابل
رسائل البوت. السبب: وضع `repaint=false` في LuxAlgo يرسم خطًّا بلا مثلثات،
والبوت يشتق المثلثات من ميل هذا الخط. tools/nw_endpoint_arrows.pine يرسمها
على الشارت لتكون قابلة للمطابقة. هذه الاختبارات تمنع انحراف السكربت عن الكود.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from src.nw_strategy import endpoint_estimator, endpoint_flip_signals

ROOT = Path(__file__).resolve().parents[1]
PINE = ROOT / "tools" / "nw_endpoint_arrows.pine"
FIXTURE = Path(__file__).parent / "fixtures" / "sndkb_1h_2026-11-06.json"


def test_pine_mirror_uses_the_same_constants_and_rule():
    t = PINE.read_text(encoding="utf-8")
    assert "//@version=5" in t
    assert "overlay=true" in t and "max_bars_back=500" in t
    assert "input.float(8." in t                         # NW_BANDWIDTH
    assert "input.source(close" in t                     # NW_SOURCE
    assert "math.exp(-(math.pow(x, 2) / (h * h * 2)))" in t   # gauss
    assert "for i = 0 to 499" in t                      # endpoint kernel
    assert "coefs.push(gauss(i, h))" in t
    # نفس قاعدة _evaluate_endpoint: d[B]<0 و d[B+1]>0 ⇒ ▲ على B
    assert "d_prev = out[1] - out[2]" in t
    assert "d_now  = out    - out[1]" in t
    assert "d_prev < 0 and d_now > 0" in t              # ▲
    assert "d_prev > 0 and d_now < 0" in t              # ▼
    assert "offset=-1" in t                            # السهم على B لا B+1
    assert "barstate.isconfirmed" in t                 # بلا إعادة رسم
    # الإسناد والترخيص إلزاميان (CC BY-NC-SA 4.0)
    assert "LuxAlgo" in t and "CC BY-NC-SA 4.0" in t


def test_pine_rule_is_equivalent_to_python_flips():
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    c = np.array([r[2] for r in rows], dtype=float)
    out = endpoint_estimator(c)
    for s in endpoint_flip_signals(out, c):
        t = int(s["index"])                 # شمعة الاكتشاف = B+1
        B = int(s["extremum_index"])       # شمعة ▲ / ▼
        assert B == t - 1
        # على الشمعة t في Pine: out[2], out[1], out ≡ out[t-2], out[t-1], out[t]
        d_prev = out[t - 1] - out[t - 2]
        d_now = out[t] - out[t - 1]
        if s["kind"] == "buy":
            assert d_prev < 0 and d_now > 0
        else:
            assert d_prev > 0 and d_now < 0


def test_pine_mirror_has_no_foreign_strategy_inputs():
    """لا بوابة BTC ولا أهداف/وقف ولا متوسطات أخرى في المرآة."""
    t = PINE.read_text(encoding="utf-8")
    for bad in ("ta.ema", "ta.sma(", "BTC", "strategy(", "takeprofit", "stoploss"):
        assert bad not in t, bad
    assert len(re.findall(r"indicator\(", t)) == 1
