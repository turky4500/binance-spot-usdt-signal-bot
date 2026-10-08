"""اختبارات نظام «فوق قمة الأمس» (PDH): الكشف، التسوية للمسارين، الرسائل، والحذف التلقائي."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategy_pdh import (  # noqa: E402
    FEE_ROUND_TRIP_PCT, MAX_HOLD_BARS, TARGET_B_PCT,
    build_entry_message, build_pdh_signal, prev_day_levels, settle_record,
)

HOUR = 3_600_000
DAY = 86_400_000
START = 1780704000000  # منتصف ليل UTC ليوم كامل


def make_hourly(days: list[dict]) -> pd.DataFrame:
    """days: قائمة أيام، كل يوم dict فيه closes (24 إغلاقًا) وقمة اختيارية."""
    rows = []
    t = START
    for spec in days:
        closes = spec["closes"]
        for h, c in enumerate(closes):
            high = c * (1 + spec.get("wick", 0.001))
            low = c * (1 - spec.get("wick", 0.001))
            if "high_override" in spec and h == spec.get("high_at", 12):
                high = spec["high_override"]
            rows.append({"open_time": t, "close_time": t + HOUR - 1, "open": c, "high": high, "low": low, "close": c, "volume": 1000.0})
            t += HOUR
    return pd.DataFrame(rows)


def uptrend_days(n: int, base: float = 90.0, step: float = 1.0) -> list[dict]:
    days = []
    for d in range(n):
        start = base + d * step
        closes = [start * (1 + 0.0002 * h) for h in range(24)]
        days.append({"closes": closes, "wick": 0.0})
    return days


failures = []


def check(name: str, ok: bool, extra: str = ""):
    print(("[OK] " if ok else "[FAIL] ") + name + (f" — {extra}" if extra else ""))
    if not ok:
        failures.append(name)


# ============ 1) الكشف: أول إغلاق فوق قمة الأمس مع اتجاه صاعد ============
days = uptrend_days(6)
prev_high = max(d["closes"][-1] * 1.001 for d in days[-2:-1])  # قمة اليوم السابق (wick 0.1%)
# 10 أيام صاعدة (لتفعيل EMA20 اليومي) ثم أمس قمته 100.0 واليوم اختراق 100.5
days = uptrend_days(10, 90.0, 1.0)
days.append({"closes": [99.0 + 0.02 * h for h in range(23)] + [100.0], "wick": 0.0})   # أمس (قمته 100.0)
today = [99.5] * 23 + [100.5]
days.append({"closes": today, "wick": 0.0})                                              # اليوم (اختراق 100.5)
df = make_hourly(days)
cand = build_pdh_signal(df, "TESTUSDT")
check("كشف الاختراق الأول فوق قمة الأمس مع اتجاه صاعد", cand is not None,
      f"level={cand['level_price'] if cand else '—'}")
if cand:
    check("سعر الدخول = إغلاق شمعة الاختراق", abs(cand["entry_price"] - 100.5) < 1e-9)
    check("الهدف = +1.2%", abs(cand["target_price"] - 100.5 * (1 + TARGET_B_PCT / 100)) < 1e-6)
    check("الوقف بين 1.2% و2.5%", 1.2 <= cand["stop_pct"] <= 2.5, f"stop={cand['stop_pct']}%")

# ============ 2) لا إشارة: تجاوز متضخم (> 1%) ============
days_bad = uptrend_days(10, 90.0, 1.0)
days_bad.append({"closes": [99.0 + 0.02 * h for h in range(23)] + [100.0], "wick": 0.0})
days_bad.append({"closes": [99.5] * 23 + [101.5], "wick": 0.0})   # تجاوز 1.5%
check("لا إشارة عند تجاوز > 1%", build_pdh_signal(make_hourly(days_bad), "T") is None)

# ============ 3) لا إشارة: اتجاه يومي هابط ============
days_down = []
val = 120.0
for _ in range(10):
    days_down.append({"closes": [val - 0.2 * h for h in range(24)], "wick": 0.0})
    val -= 1.0
days_down.append({"closes": [110.0] * 23 + [110.5], "wick": 0.0})
check("لا إشارة في اتجاه يومي هابط", build_pdh_signal(make_hourly(days_down), "T") is None)

# ============ 4) لا إشارة: ليست أول شمعة فوق الخط ============
days_second = uptrend_days(10, 90.0, 1.0)
days_second.append({"closes": [99.0 + 0.02 * h for h in range(23)] + [100.0], "wick": 0.0})
days_second.append({"closes": [99.5] * 22 + [100.5, 100.6], "wick": 0.0})
check("لا إشارة إن كانت الشمعة السابقة فوق الخط أصلًا", build_pdh_signal(make_hourly(days_second), "T") is None)

# ============ 5) التسوية: المسار الرئيسي يخرج عند إغلاق تحت الخط ============
record = dict(cand) if cand else None
check("سجل الإشارة متوفر للتسوية", record is not None)
record["peak_price"] = record["entry_price"]
bar = {"open_time": record["entry_bar_open_time"] + HOUR, "close_time": record["entry_bar_open_time"] + 2 * HOUR - 1,
       "high": 101.0, "low": 99.9, "close": 100.4}
upd = settle_record(record, bar, level_now=100.0)
check("بقيت مفتوحة والشمعة فوق الخط (لا خروج)", not any(k.startswith("outcome_") for k in upd), str(upd)[:80])
check("سُجّل أعلى سعر (MFE)", record.get("max_favorable_pct", 0) > 0)

bar2 = {"open_time": record["entry_bar_open_time"] + 2 * HOUR, "close_time": record["entry_bar_open_time"] + 3 * HOUR - 1,
        "high": 100.6, "low": 99.5, "close": 99.8}   # إغلاق تحت الخط 100.0
upd2 = settle_record(record, bar2, level_now=100.0)
check("المسار أ خرج عند «نزلت تحت الخط»", record.get("outcome_main") == "loss" and record.get("exit_reason_main") == "below_level",
      f"main={record.get('outcome_main')}/{record.get('exit_reason_main')}")
check("الصافي بعد العمولة صحيح", abs(record["net_main_pct"] - ((99.8 / 100.5 - 1) * 100 - FEE_ROUND_TRIP_PCT)) < 1e-6)
check("المسار ب ما زال مفتوحًا (لم يلمس الهدف ولا الوقف)", not record.get("outcome_b"))

# ============ 6) التسوية: المسار ب يخرج عند +1.2% ============
bar3 = {"open_time": record["entry_bar_open_time"] + 3 * HOUR, "close_time": record["entry_bar_open_time"] + 4 * HOUR - 1,
        "high": record["target_price"] + 0.05, "low": 100.2, "close": 101.0}
settle_record(record, bar3, level_now=100.0)
check("المسار ب خرج عند بلوغ الهدف +1.2%", record.get("outcome_b") == "target" and record.get("exit_reason_b") == "take_profit")
check("ربح المسار ب ثابت ≈ +1.0% صافي (1.2 − 0.2)", abs(record["net_b_pct"] - (TARGET_B_PCT - FEE_ROUND_TRIP_PCT)) < 1e-6,
      f"net_b={record['net_b_pct']}%")

# ============ 7) الوقف أولًا عند لمس الاثنين ============
rec2 = dict(cand)
rec2["peak_price"] = rec2["entry_price"]
bar4 = {"open_time": rec2["entry_bar_open_time"] + HOUR, "close_time": rec2["entry_bar_open_time"] + 2 * HOUR - 1,
        "high": rec2["target_price"] + 1, "low": rec2["stop_price"] - 0.01, "close": 100.0}
settle_record(rec2, bar4, level_now=100.0)
check("الوقف أولًا عند لمس الاثنين (المساران معًا)", rec2.get("outcome_main") == "loss" and rec2.get("outcome_b") == "loss",
      f"main={rec2.get('outcome_main')} b={rec2.get('outcome_b')}")

# ============ 8) المهلة 48 ساعة ============
rec3 = dict(cand)
rec3["peak_price"] = rec3["entry_price"]
bar5 = {"open_time": rec3["entry_bar_open_time"] + MAX_HOLD_BARS * HOUR,
        "close_time": rec3["entry_bar_open_time"] + (MAX_HOLD_BARS + 1) * HOUR - 1,
        "high": rec3["entry_price"] + 0.01, "low": rec3["entry_price"] - 0.01, "close": rec3["entry_price"] + 0.005}
settle_record(rec3, bar5, level_now=90.0)
check("المهلة تُغلق المسار الرئيسي", rec3.get("exit_reason_main") == "time_limit")

# ============ 9) الرسالة مميزة وتحوي كل الأساسيات ============
msg = build_entry_message(cand)
for needle in ("فوق قمة الأمس", "الزوج", "سعر الدخول", "الهدف المقترح", "وقف الخسارة", "+1.2%"):
    assert needle in msg, f"الرسالة يجب أن تحوي «{needle}»"
print("[OK] رسالة الإشارة مميزة وتحوي الدخول/الهدف/الوقف")

# ============ 10) الحذف التلقائي للعملات المربوطة ============
from app import SpotSignalBot, is_pegged_stable_symbol  # noqa: E402
import logging, tempfile  # noqa: E402

bot = SpotSignalBot.__new__(SpotSignalBot)
bot.data_dir = tempfile.mkdtemp()
bot.state = {
    "open_trades": {"RLUSDUSDT": {"entry_price": 1.0003, "entry_time": 1}, "XRPUSDT": {"entry_price": 2.0, "entry_time": 1}},
    "last_entry_bar_time": {"RLUSDUSDT": 1, "XRPUSDT": 1},
}
bot.config = type("C", (), {"quote_asset": "USDT"})()
bot.logger = logging.getLogger("t")
bot.events = []
bot.append_event = lambda t, s, m: bot.events.append((t, s))
bot.store = type("S", (), {"save": staticmethod(lambda st: None)})()
bot.purge_pegged_stable_trades()
check("حُذفت RLUSDUSDT من المفتوحة", "RLUSDUSDT" not in bot.state["open_trades"])
check("بقيت XRPUSDT", "XRPUSDT" in bot.state["open_trades"])
check("سُجّل حدث الإزالة", any(e[0] == "removed_pegged" for e in bot.events))

print()
if failures:
    print("FAILURES:", failures)
    sys.exit(1)
print("ALL PDH STRATEGY TESTS PASSED")
