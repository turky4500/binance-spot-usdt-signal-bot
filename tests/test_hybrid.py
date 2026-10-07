"""اختبار وضع الهجينة: وقف أصلي حتى +2%، ثم وقف متحرك بأرضية التعادل."""
import csv, os, sys, tempfile
from pathlib import Path
os.environ["EXIT_MODE"] = "hybrid"
os.environ["HYBRID_LOCK_PCT"] = "0.0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from src.config import AppConfig
from app import SpotSignalBot
from src.trade_journal import append_trade_entry

HOUR = 3_600_000; BAR = 1791201600000
tmp = tempfile.mkdtemp()
cfg = AppConfig(telegram_bot_token="x", telegram_chat_id="y", state_file=f"{tmp}/state.json")
bot = SpotSignalBot(cfg); sent=[]
bot.telegram.send_message = lambda t: sent.append(t)
bot.get_halal_verdict = lambda s: "حلال"
bot._atr_value_for = lambda df, settings_atr_len=14: 2.0
print("mode:", bot.exit_rules.mode, "|", bot.exit_rules.describe())
assert bot.exit_rules.is_hybrid and bot.exit_rules.uses_trailing_stop

def mk(sym, entry=100.0, stop=98.5):
    return {"trade_id": f"{sym}-{BAR}", "symbol": sym, "entry_price": entry, "target_price": entry*1.02,
            "stop_price": stop, "entry_time": BAR, "entry_bar_open_time": BAR,
            "last_target_check_ms": BAR+60_000, "strong": False, "buy_score": 2, "mode": "متوازن",
            "metrics": {"atr": 2.0, "rsi": 40, "relative_volume": 1.5, "plus_di": 30, "minus_di": 10},
            "atr_at_entry": 2.0, "peak_price": entry, "max_favorable_pct": 0.0,
            "reference_target_hit": False, "trail_stop_price": None, "trail_initial_stop_pct": None,
            "halal_verdict": "حلال"}

# --- A) في الطور الأول: يجب أن يستخدم الوقف الأصلي (لا وقف متحرك) ---
t = mk("AAAAUSDT", stop=98.5)
bot.state["open_trades"]["AAAAUSDT"] = t; bot.store.save(bot.state)
append_trade_entry(bot.data_dir, bot._build_trade_log_record(t))
df = pd.DataFrame([
    {"open_time": BAR, "open":100,"high":100.5,"low":99.5,"close":100.2,"close_time":BAR+HOUR-1},
    {"open_time": BAR+HOUR, "open":100.2,"high":100.6,"low":98.2,"close":98.4,"close_time":BAR+2*HOUR-1},
])
bot.binance.get_klines_for_symbols = lambda symbols, interval="1h", limit=260: {"AAAAUSDT": df}
bot.process_new_closed_hour(BAR + 2*HOUR)
assert "AAAAUSDT" not in bot.state["open_trades"], "الطور الأول يجب أن يخرج عند الوقف الأصلي"
assert "وقف الخسارة" in sent[-1], sent[-1]
print("[OK] الطور الأول: خروج عند الوقف الأصلي (98.5) مع الرسالة القديمة")

# --- B) بعد بلوغ الهدف: لا يُغلق عند +2% بل يستمر، ويقفل ربحًا عند الارتداد ---
cfg2 = AppConfig(telegram_bot_token="x", telegram_chat_id="y", state_file=f"{tempfile.mkdtemp()}/state.json")
bot2 = SpotSignalBot(cfg2); sent2=[]
bot2.telegram.send_message = lambda t: sent2.append(t)
bot2.get_halal_verdict = lambda s: "حلال"
bot2._atr_value_for = lambda df, settings_atr_len=14: 2.0
t2 = mk("BBBBUSDT", stop=98.5)
bot2.state["open_trades"]["BBBBUSDT"] = t2; bot2.store.save(bot2.state)
append_trade_entry(bot2.data_dir, bot2._build_trade_log_record(t2))
d1 = pd.DataFrame([
    {"open_time": BAR, "open":100,"high":100.3,"low":99.8,"close":100.1,"close_time":BAR+HOUR-1},
    {"open_time": BAR+HOUR, "open":100.1,"high":103.2,"low":100.0,"close":102.8,"close_time":BAR+2*HOUR-1},
])
bot2.binance.get_klines_for_symbols = lambda symbols, interval="1h", limit=260: {"BBBBUSDT": d1}
bot2.process_new_closed_hour(BAR + 2*HOUR)
assert "BBBBUSDT" in bot2.state["open_trades"], "الصفقة يجب أن تبقى مفتوحة بعد الهدف المرجعي"
assert t2["reference_target_hit"] and t2["peak_price"] == 103.2
print(f"[OK] بعد +3.2%: الصفقة مستمرة | أعلى سعر {t2['peak_price']} | الوقف {t2['trail_stop_price']}")
assert any("الهدف المرجعي" in m for m in sent2)

d2 = pd.DataFrame([
    {"open_time": BAR+HOUR, "open":100.1,"high":103.2,"low":100.0,"close":102.8,"close_time":BAR+2*HOUR-1},
    {"open_time": BAR+2*HOUR, "open":102.8,"high":103.0,"low":99.4,"close":99.9,"close_time":BAR+3*HOUR-1},
])
bot2.binance.get_klines_for_symbols = lambda symbols, interval="1h", limit=260: {"BBBBUSDT": d2}
bot2.process_new_closed_hour(BAR + 3*HOUR)
assert "BBBBUSDT" not in bot2.state["open_trades"], "يجب الخروج عند كسر الوقف المتحرك"
print("--- exit message ---"); print(sent2[-1])
rows = list(csv.DictReader(open(f"{bot2.data_dir}/trades_log.csv", encoding="utf-8")))
row = [r for r in rows if r["trade_id"]=="BBBBUSDT-"+str(BAR)][0]
print("journal:", row["outcome"], row["exit_reason"], row["exit_mode"], "peak", row["max_favorable_pct"], "ref_hit", row["reference_target_hit"])
assert row["exit_reason"] == "trail_stop" and row["reference_target_hit"] == "1"
print("\nALL HYBRID TESTS PASSED")
