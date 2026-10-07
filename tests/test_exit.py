"""اختبار كامل لقاعدة الوقف المتحرك: دورة حياة صفقة من الدخول حتى الخروج."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["EXIT_MODE"] = "trailing"
os.environ["TRAIL_ATR_MULT"] = "2.0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.config import AppConfig
from src.exit_rules import ExitSettings
from app import SpotSignalBot

tmp = tempfile.mkdtemp()
config = AppConfig(telegram_bot_token="x", telegram_chat_id="y", state_file=f"{tmp}/state.json")
bot = SpotSignalBot(config)
sent = []
bot.telegram.send_message = lambda text: sent.append(text)
bot.get_halal_verdict = lambda symbol: "حلال"

print("exit mode:", bot.exit_rules.mode, "|", bot.exit_rules.describe())
assert bot.exit_rules.is_trailing

# ---- 1) فتح صفقة يدويًا مع ATR معروف ----
HOUR = 3_600_000
entry_price = 100.0
atr = 2.0  # → الوقف المبدئي = 100 - 2*2 = 96 (4%)
trade = {
    "trade_id": "TESTUSDT-1",
    "symbol": "TESTUSDT",
    "entry_price": entry_price,
    "target_price": entry_price * 1.02,
    "stop_price": 97.5,
    "entry_time": 1791201600000,
    "entry_bar_open_time": 1791201600000,
    "last_target_check_ms": 1791201600000 + 60_000,
    "strong": False,
    "buy_score": 2,
    "mode": "متوازن",
    "metrics": {"rsi": 40, "relative_volume": 1.5, "plus_di": 30, "minus_di": 10, "atr": atr},
    "atr_at_entry": atr,
    "peak_price": entry_price,
    "max_favorable_pct": 0.0,
    "reference_target_hit": False,
    "trail_stop_price": round(bot.exit_rules.trail_level(entry_price, atr), 10),
    "trail_initial_stop_pct": round((entry_price - bot.exit_rules.trail_level(entry_price, atr)) / entry_price * 100, 6),
    "halal_verdict": "حلال",
}
print("trail initial stop:", trade["trail_stop_price"], "=", trade["trail_initial_stop_pct"], "% below entry")
assert abs(trade["trail_stop_price"] - 96.0) < 1e-9
assert abs(trade["trail_initial_stop_pct"] - 4.0) < 1e-6

bot.state["open_trades"]["TESTUSDT"] = trade
bot.store.save(bot.state)
bot.send_entry_message(trade)
print("--- entry message ---")
print(sent[-1])

# ---- 2) شمعة تصعد فوق الهدف المرجعي → تنبيه بلا إغلاق، والوقف يرتفع ----
bar2_open = 1791201600000 + HOUR
df = pd.DataFrame([
    {"open_time": 1791201600000, "open": 100, "high": 100.5, "low": 99.5, "close": 100.2, "close_time": bar2_open - 1},
    {"open_time": bar2_open, "open": 100.2, "high": 103.0, "low": 100.0, "close": 102.5, "close_time": bar2_open + HOUR - 1},
])
# نُغذّي شموع الاختبار مباشرة بدل الشبكة
bot._atr_value_for = lambda df, settings_atr_len=14: 2.0
bot.binance.get_klines_for_symbols = lambda symbols, interval="1h", limit=260: {"TESTUSDT": df}
bot.process_new_closed_hour(bar2_open + HOUR)  # يُعالج آخر ساعة مغلقة = bar2_open

print("\n--- after +2.5% candle ---")
print("reference_target_hit:", trade.get("reference_target_hit"))
print("peak:", trade.get("peak_price"), "| trail stop:", trade.get("trail_stop_price"))
print("still open:", "TESTUSDT" in bot.state["open_trades"])
assert trade.get("reference_target_hit") is True
assert "TESTUSDT" in bot.state["open_trades"], "الصفقة يجب أن تبقى مفتوحة في وضع التتبع"
print("--- reference target message ---")
print([m for m in sent if "الهدف المرجعي" in m][-1])

# ---- 3) شمعة ترتد وتغلق تحت الوقف المتحرك → خروج ----
bot.state["open_trades"]["TESTUSDT"] = trade
peak_before = trade["peak_price"]
trail_before = trade["trail_stop_price"]
bar3_open = bar2_open + HOUR
df2 = pd.DataFrame([
    {"open_time": bar2_open, "open": 102.5, "high": 103.0, "low": 100.0, "close": 102.5, "close_time": bar3_open - 1},
    {"open_time": bar3_open, "open": 102.5, "high": 102.6, "low": 97.5, "close": 97.8, "close_time": bar3_open + HOUR - 1},
])
# نضبط ATR على 2.0 → الوقف = 103 - 4 = 99.0 وهو أعلى من إغلاق 97.8 → خروج
bot.binance.get_klines_for_symbols = lambda symbols, interval="1h", limit=260: {"TESTUSDT": df2}
bot.process_new_closed_hour(bar3_open + HOUR)
print("\n--- after reversal candle ---")
print("closed:", "TESTUSDT" not in bot.state["open_trades"])
assert "TESTUSDT" not in bot.state["open_trades"], "يجب أن تُغلق الصفقة"
print("--- exit message ---")
print(sent[-1])

import csv
rows = list(csv.DictReader(open(f"{tmp}/trades_log.csv", encoding="utf-8")))
row = rows[0]
print("\n--- journal row ---")
for k in ["outcome", "exit_reason", "exit_mode", "max_favorable_pct", "reference_target_hit",
          "trail_atr_mult", "trail_initial_stop_pct", "net_return_pct", "duration_text"]:
    print(f"  {k}: {row[k]}")
assert row["exit_reason"] == "trail_stop"
assert row["exit_mode"] == "trailing"
assert row["reference_target_hit"] == "1"
assert float(row["max_favorable_pct"]) > 2.9

print("\nALL TRAILING EXIT TESTS PASSED")
