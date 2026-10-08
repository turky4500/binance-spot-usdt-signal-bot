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

# ============ 7) وسم المسار (مركّز 0/5/6 مقابل شامل) ============
from src.strategy_shadow import FOCUS_HOURS  # noqa: E402
bar_focus = hour_bar(5, days_back=2)
df_f = build_series(bar_focus, down_then_cross)
cand_f = build_candidate(df_f, "PBUSDT", daily_trend_ok=True)
assert cand_f is not None and cand_f["in_focus_hours"] == 1, "الساعة 5 يجب أن تُوسم مركّزة"
bar_wide = hour_bar(14, days_back=2)
df_w = build_series(bar_wide, down_then_cross)
cand_w = build_candidate(df_w, "PBUSDT", daily_trend_ok=True)
assert cand_w is not None and cand_w["in_focus_hours"] == 0, "الساعة 14 يجب أن تكون شاملة فقط"
print(f"[OK] وسم المسار: ساعة 5 → مركّز ({sorted(FOCUS_HOURS)}) | ساعة 14 → شامل فقط")

# الإحصاءات تفصل المسارين
all_rows = [
    {"entry_date_local": "2026-10-08", "outcome": "target", "net_return_pct": "1.8", "in_focus_hours": "1"},
    {"entry_date_local": "2026-10-08", "outcome": "loss", "net_return_pct": "-1.4", "in_focus_hours": "1"},
    {"entry_date_local": "2026-10-08", "outcome": "loss", "net_return_pct": "-1.3", "in_focus_hours": "0"},
]
a = SpotSignalBot._strategy_shadow_stats(all_rows)
b = SpotSignalBot._strategy_shadow_stats([r for r in all_rows if r["in_focus_hours"] == "1"])
assert a["total"] == 3 and b["total"] == 2 and b["wins"] == 1 and b["rate"] == 50.0
print(f"[OK] إحصاء المسارين: شامل {a['total']} إشارة (متوسط {a['avg_net']:+.3f}%) | مركّز {b['total']} إشارة (متوسط {b['avg_net']:+.3f}%)")

# ============ 8) مسار IBS (من دفعة يوتيوب) ============
from src.strategy_shadow import build_ibs_candidate, ibs_value  # noqa: E402

H = 3_600_000
# يوم كامل يبدأ منتصف ليل UTC + 4 شمعات من اليوم التالي (آخرها 03:00 UTC = 06:00 الرياض)
day0 = 1781136000000          # 2026-… منتصف ليل UTC
rows_ibs = []
for i in range(96):
    t = day0 - (96 - i) * H
    c = 105 + (i % 5) * 0.1
    rows_ibs.append({"open_time": t, "close_time": t + H - 1, "open": c, "high": c + 0.3, "low": c - 0.3, "close": c, "volume": 1000.0})
next_day = [
    {"open_time": day0 + 0 * H, "close": 105.0, "high": 106.0, "low": 100.0},   # القاع يكون هنا
    {"open_time": day0 + 1 * H, "close": 105.0, "high": 110.0, "low": 104.0},   # القمة تكون هنا
    {"open_time": day0 + 2 * H, "close": 105.0, "high": 106.0, "low": 104.0},
    {"open_time": day0 + 3 * H, "close": 101.0, "high": 106.0, "low": 104.0},   # الإغلاق الأخير
]
for b in next_day:
    rows_ibs.append({"open_time": b["open_time"], "close_time": b["open_time"] + H - 1,
                     "open": b["close"], "high": b["high"], "low": b["low"], "close": b["close"], "volume": 1000.0})
df_ibs = pd.DataFrame(rows_ibs)
ibs = ibs_value(df_ibs)
assert ibs is not None and abs(ibs - 0.1) < 1e-9, f"IBS يجب أن تكون 0.1 وليست {ibs}"
print(f"[OK] حساب IBS: نطاق اليوم 100→110 وإغلاق 101 → IBS={ibs:.2f}")

hour_local = ((int(df_ibs.iloc[-1]["open_time"]) + 3 * H) // H) % 24
assert hour_local == 6, f"الساعة المحلية يجب أن تكون 6 وليست {hour_local}"
cand_ibs = build_ibs_candidate(df_ibs, "IBSTEST", daily_trend_ok=True)
assert cand_ibs is not None, "يجب أن يُبنى مرشح IBS"
assert cand_ibs["lane"] == "ibs" and cand_ibs["in_focus_hours"] == 1
assert 1.2 <= cand_ibs["stop_pct"] <= 2.5
assert abs(cand_ibs["target_price"] / cand_ibs["entry_price"] - 1.02) < 1e-9
print(f"[OK] مرشح IBS: دخول {cand_ibs['entry_price']} | وقف {cand_ibs['stop_pct']}% | هدف +2% | ساعة 6 | المسار ibs")

df_bad = df_ibs.copy()
df_bad.loc[df_bad.index[-1], "close"] = 109.5   # IBS = 0.95
assert build_ibs_candidate(df_bad, "IBSTEST", daily_trend_ok=True) is None
print("[OK] IBS≥0.2 → لا إشارة")

assert build_ibs_candidate(df_ibs, "IBSTEST", daily_trend_ok=False) is None
print("[OK] اتجاه يومي غير صاعد → لا إشارة IBS")

# آخر شمعة عند 10:00 UTC = 13:00 الرياض → خارج الساعات المركّزة
df_ibs3 = df_ibs.copy()
df_ibs3.loc[df_ibs3.index[-1], "open_time"] = day0 + 10 * H
df_ibs3.loc[df_ibs3.index[-1], "close_time"] = day0 + 11 * H - 1
assert build_ibs_candidate(df_ibs3, "IBSTEST", daily_trend_ok=True) is None
print("[OK] ساعة 13 (خارج 0/5/6) → لا إشارة IBS")

print("\nALL STRATEGY SHADOW TESTS PASSED")
