"""اختبار انحدار: الوضع القديم (هدف ثابت) ما زال يعمل + التقارير تعرض أرقامًا صحيحة."""
import csv
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import AppConfig

HOUR = 3_600_000
BAR = 1791201600000


def make_bot(exit_mode: str):
    os.environ["EXIT_MODE"] = exit_mode
    tmp = tempfile.mkdtemp()
    from app import SpotSignalBot

    cfg = AppConfig(telegram_bot_token="x", telegram_chat_id="y", state_file=f"{tmp}/state.json")
    bot = SpotSignalBot(cfg)
    sent = []
    bot.telegram.send_message = lambda text: sent.append(text)
    bot.get_halal_verdict = lambda symbol: "حلال"
    bot._atr_value_for = lambda df, settings_atr_len=14: 2.0
    return bot, sent, tmp


def make_trade(bot, symbol="AAAUSDT"):
    entry = 100.0
    return {
        "trade_id": f"{symbol}-{BAR}", "symbol": symbol, "entry_price": entry,
        "target_price": entry * 1.02, "stop_price": 97.5,
        "entry_time": BAR, "entry_bar_open_time": BAR, "last_target_check_ms": BAR + 60_000,
        "strong": False, "buy_score": 2, "mode": "متوازن",
        "metrics": {"rsi": 40, "atr": 2.0, "relative_volume": 1.5, "plus_di": 30, "minus_di": 10},
        "atr_at_entry": 2.0, "peak_price": entry, "max_favorable_pct": 0.0,
        "reference_target_hit": False,
        "trail_stop_price": bot.exit_rules.trail_level(entry, 2.0),
        "trail_initial_stop_pct": 4.0, "halal_verdict": "حلال",
    }


import pandas as pd

# ---------- 1) الوضع القديم: هدف ثابت يُغلق عند +2% ----------
bot, sent, tmp = make_bot("fixed_target")
assert not bot.exit_rules.is_trailing
trade = make_trade(bot)
bot.state["open_trades"]["AAAUSDT"] = trade
bot.store.save(bot.state)
from src.trade_journal import append_trade_entry
append_trade_entry(bot.data_dir, bot._build_trade_log_record(trade))
df = pd.DataFrame([
    {"open_time": BAR, "open": 100, "high": 100.4, "low": 99.5, "close": 100.1, "close_time": BAR + HOUR - 1},
    {"open_time": BAR + HOUR, "open": 100.1, "high": 102.4, "low": 100.0, "close": 102.1, "close_time": BAR + 2 * HOUR - 1},
])
bot.binance.get_klines_for_symbols = lambda symbols, interval="1h", limit=260: {"AAAUSDT": df}
bot.process_new_closed_hour(BAR + 2 * HOUR)
assert "AAAUSDT" not in bot.state["open_trades"], "الوضع القديم يجب أن يُغلق عند الهدف"
assert any("تم تحقيق الهدف" in m for m in sent)
rows = list(csv.DictReader(open(f"{tmp}/trades_log.csv", encoding="utf-8")))
assert rows[0]["outcome"] == "target" and rows[0]["exit_reason"] == "take_profit"
print("[OK] الوضع القديم (هدف ثابت) يعمل كما كان")

# ---------- 2) التقارير: عدّ exit_win / exit_loss ----------
bot2, sent2, tmp2 = make_bot("trailing")
from src.trade_analysis import is_win, is_loss

assert is_win("win") and is_win("target") and not is_win("loss")
assert is_loss("loss") and is_loss("stop") and not is_loss("win")

bot2.state["event_log"] = [
    {"type": "entry", "symbol": "A", "time_ms": BAR},
    {"type": "exit_win", "symbol": "A", "time_ms": BAR + HOUR},
    {"type": "entry", "symbol": "B", "time_ms": BAR + HOUR},
    {"type": "exit_loss", "symbol": "B", "time_ms": BAR + 2 * HOUR},
    {"type": "reference_target", "symbol": "C", "time_ms": BAR + 3 * HOUR},
]
import datetime
from zoneinfo import ZoneInfo

# نضبط التاريخ على يوم التقرير (أمس بتوقيت الرياض) عبر تشغيل التقرير على وقت حقيقي
now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
local_now = datetime.datetime.fromtimestamp(now_ms / 1000, tz=ZoneInfo("Asia/Riyadh"))
midnight = local_now.replace(hour=0, minute=5, second=0, microsecond=0)
midnight_ms = int(midnight.timestamp() * 1000)
report_day = (midnight.date() - datetime.timedelta(days=1))
day_start_ms = int(datetime.datetime(report_day.year, report_day.month, report_day.day, tzinfo=ZoneInfo("Asia/Riyadh")).timestamp() * 1000)
bot2.state["event_log"] = [
    {"type": "entry", "symbol": "A", "time_ms": day_start_ms + HOUR},
    {"type": "exit_win", "symbol": "A", "time_ms": day_start_ms + 2 * HOUR},
    {"type": "entry", "symbol": "B", "time_ms": day_start_ms + 3 * HOUR},
    {"type": "exit_loss", "symbol": "B", "time_ms": day_start_ms + 4 * HOUR},
    {"type": "reference_target", "symbol": "C", "time_ms": day_start_ms + 5 * HOUR},
]
bot2.send_daily_report_if_due(midnight_ms)
report = sent2[-1]
print("--- daily report ---")
print(report)
assert "عدد الصفقات المرسلة: 2" in report
assert "عدد النجاح: 1" in report
assert "عدد الخسارة: 1" in report, "exit_loss يجب أن يُحسب خسارة"

# ---------- 3) التحليل اليومي: قسم مقارنة قواعد الخروج ----------
with open(f"{tmp2}/trades_log.csv", "w", newline="", encoding="utf-8") as f:
    from src.trade_journal import FIELDNAMES

    w = csv.DictWriter(f, fieldnames=FIELDNAMES)
    w.writeheader()
    base = {k: "" for k in FIELDNAMES}
    w.writerow({**base, "trade_id": "T1", "symbol": "A", "entry_time_ms": day_start_ms + HOUR,
                "entry_date_local": report_day.isoformat(), "entry_price": 100, "outcome": "win",
                "exit_reason": "trail_stop", "exit_time_ms": day_start_ms + 2 * HOUR, "exit_price": 104,
                "net_return_pct": 3.8, "reference_target_hit": "1", "max_favorable_pct": 5.0,
                "buy_score": 2, "rsi": 40, "adx": 20, "relative_volume": 1.5, "strong_signal": "0"})
    w.writerow({**base, "trade_id": "T2", "symbol": "B", "entry_time_ms": day_start_ms + 3 * HOUR,
                "entry_date_local": report_day.isoformat(), "entry_price": 100, "outcome": "loss",
                "exit_reason": "trail_stop", "exit_time_ms": day_start_ms + 4 * HOUR, "exit_price": 98,
                "net_return_pct": -2.2, "reference_target_hit": "1", "max_favorable_pct": 2.4,
                "buy_score": 2, "rsi": 45, "adx": 22, "relative_volume": 1.6, "strong_signal": "1"})
bot2.send_daily_analysis_if_due(midnight_ms)
analysis = sent2[-1]
print("\n--- daily analysis (exit-mode section) ---")
for line in analysis.splitlines():
    if "قاعدة الخروج" in line or "الهدف المرجعي" in line or "أكملت" in line or "ارتدت" in line or "القاعدة القديمة" in line:
        print(line)
assert "قاعدة الخروج المفعّلة" in analysis
assert "بلغت الهدف المرجعي" in analysis

print("\nALL REGRESSION TESTS PASSED")
