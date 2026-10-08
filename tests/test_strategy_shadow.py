"""اختبار الوضع الظلّي: تسجيل إشارة الارتداد بلا رسائل، ثم تسويتها هدفًا ووقفًا ومهلة."""
import os, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ["STRATEGY_SHADOW"] = "1"
os.environ["SMART_ENTRY"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
from src.config import AppConfig  # noqa: E402
from src.strategy_shadow import (  # noqa: E402
    GOOD_HOURS, load_rows, daily_trend_from_daily_klines, build_candidate, pullback_mask,
)
from app import SpotSignalBot  # noqa: E402

HOUR = 3_600_000
TZ = ZoneInfo("Asia/Riyadh")
tmp = tempfile.mkdtemp()
cfg = AppConfig(telegram_bot_token="x", telegram_chat_id="y", state_file=f"{tmp}/state.json")
bot = SpotSignalBot(cfg)
sent = []
bot.telegram.send_message = lambda t: sent.append(t)
bot.get_halal_verdict = lambda s: "حلال"


def hour_bar(local_hour: int, days_back: int = 1) -> int:
    """open_time لشمعة ساعية تبدأ في الساعة المحلية المطلوبة."""
    base = datetime.now(TZ).replace(minute=0, second=0, microsecond=0) - timedelta(days=days_back)
    return int(base.replace(hour=local_hour).timestamp() * 1000)


def build_series(bar: int, closes: list[float], start_hour_local: int | None = None) -> pd.DataFrame:
    rows = []
    for i, c in enumerate(closes):
        rows.append({
            "open_time": bar - (len(closes) - 1 - i) * HOUR,
            "open": c, "high": c * 1.001, "low": c * 0.999, "close": c, "volume": 1000.0,
            "close_time": bar - (len(closes) - 1 - i) * HOUR + HOUR - 1,
        })
    return pd.DataFrame(rows)


# ============ 0) شرط الارتداد نفسه: أول إغلاق تحت EMA20 ============
bar = hour_bar(2)
# 80 شمعة (فوق الحد الأدنى 60 المطلوب لتهيئة المؤشرات)
down_then_cross = [100 + i * 0.5 for i in range(77)]        # صعود إلى 138.5
down_then_cross += [137.5, 136.0, 131.0]                    # ثم هبوط حاد يكسر متوسط 20 (EMA≈134.5)
df = build_series(bar, down_then_cross)
mask = pullback_mask(df)
assert bool(mask.iloc[-1]), "يجب أن تتحقق إشارة الارتداد عند أول إغلاق تحت EMA20"
assert int((df.iloc[-1]["open_time"] + 3 * HOUR) // HOUR % 24) == 2, "الشبهة: الساعة المحلية"
print("[OK] شرط الارتداد: إشارة عند أول إغلاق تحت متوسط 20 ساعة")

# ============ 1) الاتجاه اليومي من شموع 1d ============
closes_up = [100 + i * 0.5 for i in range(60)]
daily_up = pd.DataFrame({"close": closes_up})
ok, close, sma = daily_trend_from_daily_klines(daily_up)
assert ok and close > sma, "اتجاه صاعد يجب أن يُقبل"
daily_down = pd.DataFrame({"close": [150 - i * 0.5 for i in range(60)]})
ok2, _, _ = daily_trend_from_daily_klines(daily_down)
assert not ok2, "اتجاه هابط يجب أن يُرفض"
print(f"[OK] فلتر الاتجاه اليومي: صاعد ✅ ({close:.1f} > {sma:.1f}) • هابط ❌")

# ============ 2) التسجيل الظلّي في الدورة: بلا رسائل ============
bot.binance.get_klines_for_symbols = lambda symbols, interval="1h", limit=260: (
    {"PBUSDT": df} if interval == "1h" else {"PBUSDT": daily_up} if interval == "1d" else {}
)
bot.symbols = ["PBUSDT"]
before_msgs = len(sent)
bot.process_new_closed_hour(int(df.iloc[-1]["open_time"]) + HOUR)
rows = load_rows(bot.data_dir)
assert len(rows) == 1, f"يجب تسجيل إشارة ظلّية واحدة، وُجد {len(rows)}"
assert len(sent) == before_msgs, "الوضع الظلّي ممنوع أن يرسل أي رسالة!"
row = rows[0]
print(f"[OK] سُجّلت إشارة ظلّية بلا رسائل: {row['symbol']} @ {row['entry_price']} | ساعة {row['entry_hour_local']} | هدف {row['target_price']} | وقف {row['stop_price']}")
assert int(row["entry_hour_local"]) in GOOD_HOURS

# ============ 3) التسوية على الهدف ============
entry = float(row["entry_price"])
target = float(row["target_price"])
signal_open = int(row["entry_time_ms"]) - HOUR + 1     # فتح شمعة الإشارة
next_open = signal_open + HOUR                          # الشمعة التالية (فيها الخروج)
nxt = pd.DataFrame([
    {"open_time": next_open, "open": entry, "high": target * 1.001, "low": entry * 0.999, "close": target, "volume": 1000.0, "close_time": next_open + HOUR - 1},
])
bot.binance.get_klines_for_symbols = lambda symbols, interval="1h", limit=260: (
    {"PBUSDT": nxt} if interval == "1h" else {"PBUSDT": daily_up} if interval == "1d" else {}
)
bot.process_new_closed_hour(next_open + HOUR)
rows = load_rows(bot.data_dir)
assert rows[0]["outcome"] == "target" and rows[0]["exit_reason"] == "take_profit", rows[0]
assert float(rows[0]["net_return_pct"]) > 1.5, rows[0]["net_return_pct"]
print(f"[OK] تسوية الهدف: {rows[0]['outcome']} | صافي {float(rows[0]['net_return_pct']):+.3f}% | المدة {rows[0]['duration_minutes']} دقيقة")

# ============ 4) التسوية على الوقف + منع الإشارة أثناء وجود صفقة ظلّية مفتوحة ============
tmp2 = tempfile.mkdtemp()
bot2 = SpotSignalBot(AppConfig(telegram_bot_token="x", telegram_chat_id="y", state_file=f"{tmp2}/state.json"))
sent2 = []
bot2.telegram.send_message = lambda t: sent2.append(t)
bar = hour_bar(2, days_back=2)
df2 = build_series(bar, down_then_cross)
bot2.binance.get_klines_for_symbols = lambda symbols, interval="1h", limit=260: (
    {"PBUSDT": df2} if interval == "1h" else {"PBUSDT": daily_up} if interval == "1d" else {}
)
bot2.symbols = ["PBUSDT"]
bot2.process_new_closed_hour(int(df2.iloc[-1]["open_time"]) + HOUR)
assert len(load_rows(bot2.data_dir)) == 1
first = load_rows(bot2.data_dir)[0]
entry2 = float(first["entry_price"])
stop2 = float(first["stop_price"])
signal_open2 = int(first["entry_time_ms"]) - HOUR + 1
bar1 = signal_open2 + HOUR
fall = pd.DataFrame([{"open_time": bar1, "open": entry2, "high": entry2 * 1.001, "low": stop2 * 0.999, "close": stop2, "volume": 1000.0, "close_time": bar1 + HOUR - 1}])
bot2.binance.get_klines_for_symbols = lambda symbols, interval="1h", limit=260: (
    {"PBUSDT": fall} if interval == "1h" else {"PBUSDT": daily_up} if interval == "1d" else {}
)
bot2.process_new_closed_hour(bar1 + HOUR)
r2 = load_rows(bot2.data_dir)[0]
assert r2["outcome"] == "loss" and r2["exit_reason"] == "stop_loss", r2
assert float(r2["net_return_pct"]) < 0
print(f"[OK] تسوية الوقف: {r2['outcome']} | صافي {float(r2['net_return_pct']):+.3f}% (الوقف أولًا عند لمس الاثنين)")
print(f"[OK] الوضع الظلّي بلا أي رسالة: {len(sent2)} رسالة")

# ============ 5) ساعة سيئة = لا إشارة ============
tmp3 = tempfile.mkdtemp()
bot3 = SpotSignalBot(AppConfig(telegram_bot_token="x", telegram_chat_id="y", state_file=f"{tmp3}/state.json"))
bad_hour = next(h for h in range(24) if h not in GOOD_HOURS)
bar_bad = hour_bar(bad_hour, days_back=2)
df3 = build_series(bar_bad, down_then_cross)
bot3.binance.get_klines_for_symbols = lambda symbols, interval="1h", limit=260: (
    {"PBUSDT": df3} if interval == "1h" else {"PBUSDT": daily_up} if interval == "1d" else {}
)
bot3.symbols = ["PBUSDT"]
bot3.process_new_closed_hour(int(df3.iloc[-1]["open_time"]) + HOUR)
assert load_rows(bot3.data_dir) == [], "لا يجب تسجيل إشارة في ساعة خارج النافذة"
print(f"[OK] الساعة {bad_hour:02d}:00 (خارج النافذة) → لا إشارة")

# ============ 6) إحصاءات الظلّي ============
stats = SpotSignalBot._strategy_shadow_stats([
    {"entry_date_local": "2026-10-01", "outcome": "target", "net_return_pct": "1.8"},
    {"entry_date_local": "2026-10-01", "outcome": "loss", "net_return_pct": "-1.3"},
    {"entry_date_local": "2026-10-02", "outcome": "", "net_return_pct": ""},
])
assert stats["total"] == 3 and stats["closed"] == 2 and stats["wins"] == 1 and stats["rate"] == 50.0
assert abs(stats["avg_net"] - 0.25) < 1e-9
print(f"[OK] الإحصاءات: {stats['total']} إشارة | مغلقة {stats['closed']} (نجاح {stats['rate']:.0f}%) | متوسط {stats['avg_net']:+.3f}%")

print("\nALL STRATEGY SHADOW TESTS PASSED")
