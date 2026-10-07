"""اختبار تكامل الدخول الذكي عبر المسار الكامل للبوت:
بوابة الساعة → جمع المرشحين → الدرجة → حد التزامن → رسالة الدخول → السجل.
"""
import csv, os, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ["SMART_ENTRY"] = "1"
os.environ["ENTRY_HOURS"] = "0,1,2"
os.environ["MAX_OPEN_TRADES"] = "3"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
from src.config import AppConfig  # noqa: E402
import app as app_module  # noqa: E402
from app import SpotSignalBot  # noqa: E402

HOUR = 3_600_000
TZ = ZoneInfo("Asia/Riyadh")


def bar_open_for_local_hour(hour: int) -> int:
    """open_time لشمعة ساعية تبدأ في الساعة المطلوبة بتوقيت الرياض (والأيام السابقة إن لزم)."""
    d = datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
    if d.hour < hour:
        d = d.replace(hour=hour)
    else:
        d = d.replace(hour=hour)
    # اختر يومًا يكون فيه هذا التوقيت قبل الآن
    while d.timestamp() * 1000 + HOUR > datetime.now(timezone.utc).timestamp() * 1000:
        d = d.replace(day=d.day - 1) if d.day > 1 else None
        break
    return int(d.timestamp() * 1000)


def run_hour(bot, symbols, hour_local, open_count=0, kline_builder=None):
    """يشغّل معالجة شمعة مغلقة واحدة بساعة محلية محددة على عملات مركّبة."""
    bar = bar_open_for_local_hour(hour_local)
    last = bar + HOUR
    frames = {}
    for sym in symbols:
        frames[sym] = kline_builder(sym, bar) if kline_builder else pd.DataFrame([
            {"open_time": bar, "open": 100, "high": 101, "low": 99, "close": 100.5, "close_time": bar + HOUR - 1},
        ])
    bot.symbols = list(symbols)
    bot.binance.get_klines_for_symbols = lambda s, interval="1h", limit=260: dict(frames)
    bot.process_new_closed_hour(last)
    return bar


def fake_signal(symbol, bar, buy_score=2, rv=1.5, plus_di=30, minus_di=10, adx=26, rsi=55):
    entry = 100.0
    return {
        "symbol": symbol, "bar_open_time": bar, "bar_close_time": bar + HOUR - 1,
        "entry_price": entry, "target_price": round(entry * 1.02, 10), "stop_price": 98.5,
        "strong": buy_score >= 3, "buy_score": buy_score, "mode": "متوازن",
        "metrics": {"atr": 1.6, "rsi": rsi, "relative_volume": rv, "plus_di": plus_di,
                    "minus_di": minus_di, "adx": adx},
    }


def new_bot(tag):
    cfg = AppConfig(telegram_bot_token="x", telegram_chat_id="y",
                    state_file=f"{tempfile.mkdtemp()}/{tag}.json")
    bot = SpotSignalBot(cfg)
    sent = []
    bot.telegram.send_message = lambda text: sent.append(text)
    bot.get_halal_verdict = lambda s: "حلال"
    return bot, sent


def rejections(bot):
    path = Path(bot.data_dir) / "rejected_candidates.csv"
    return list(csv.DictReader(open(path))) if path.exists() else []


# ============ A) خارج النافذة: كل المرشحين يُرفضون بسبب زمني ولا تُفتح صفقة ============
bot, sent = new_bot("A")
syms = [f"A{i}USDT" for i in range(5)]
app_module.compute_entry_signal = lambda df, *a, **k: fake_signal(df.iloc[-1]["symbol"], int(df.iloc[-1]["open_time"]))
run_hour(bot, syms, hour_local=15)  # الساعة 15:00 الرياض = خارج [0,1,2]
rej = rejections(bot)
assert bot.state["open_trades"] == {}, "لا يجب فتح أي صفقة خارج النافذة الزمنية"
assert not sent, "لا رسائل دخول خارج النافذة"
assert len(rej) == 5 and all(r["reject_reason"] == "outside_time_window" for r in rej), rej
print(f"[OK] خارج النافذة (15:00): رُفض {len(rej)} مرشحًا بلا صفقات ولا رسائل — {rej[0]['reject_reason_ar']}")

# ============ B) داخل النافذة: تُفتح الصفقات وتُرسل الرسائل ============
bot2, sent2 = new_bot("B")
run_hour(bot2, syms, hour_local=1)  # 01:00 الرياض = داخل [0,1,2]
opened2 = bot2.state["open_trades"]
assert len(opened2) == 3, opened2.keys()          # الحد الأقصى للتزامن
assert len(sent2) == 3, len(sent2)
assert all(r["reject_reason"] == "concurrency_cap" for r in rejections(bot2))
print(f"[OK] داخل النافذة (01:00): فُتحت {len(opened2)} صفقة (سقف التزامن) وأُرسلت {len(sent2)} رسالة")

# ============ C) حد التزامن + الترتيب بالجودة ============
bot3, sent3 = new_bot("C")
# كلها تمر من بوابات الحجم/الاتجاه، وتختلف في درجة الجودة فقط
mixed = [
    ("LOW1USDT", dict(buy_score=2, rv=1.25, plus_di=20, minus_di=10, adx=20, rsi=75)),  # درجة 2
    ("MID1USDT", dict(buy_score=2, rv=1.30, plus_di=30, minus_di=10, adx=26, rsi=55)),  # درجة 4
    ("TOP1USDT", dict(buy_score=3, rv=1.90, plus_di=40, minus_di=5, adx=30, rsi=55)),   # درجة 5
    ("TOP2USDT", dict(buy_score=3, rv=1.40, plus_di=40, minus_di=5, adx=30, rsi=55)),   # درجة 5
    ("LOW2USDT", dict(buy_score=2, rv=1.20, plus_di=20, minus_di=10, adx=20, rsi=70)),  # درجة 2
]
params = {s: p for s, p in mixed}
app_module.compute_entry_signal = lambda df, *a, **k: fake_signal(
    df.iloc[-1]["symbol"], int(df.iloc[-1]["open_time"]), **params[df.iloc[-1]["symbol"]])
bar = run_hour(bot3, [s for s, _ in mixed], hour_local=2)  # 02:00 داخل النافذة
opened = set(bot3.state["open_trades"].keys())
rej3 = rejections(bot3)
print("المفتوحة:", sorted(opened), "| المرفوضة:", [(r["symbol"], r["reject_reason"]) for r in rej3])
assert opened == {"TOP1USDT", "TOP2USDT", "MID1USDT"}, opened
assert len(rej3) == 2 and all(r["reject_reason"] == "concurrency_cap" for r in rej3), rej3
assert sorted(r["symbol"] for r in rej3) == ["LOW1USDT", "LOW2USDT"]
print("[OK] حد التزامن 3: اختار الأعلى جودة (5,5,4) ورفض الأضعف (2,2) بسبب التزامن")
assert all("smart_score" in t and "entry_hour_local" in t for t in bot3.state["open_trades"].values())
assert {t["entry_hour_local"] for t in bot3.state["open_trades"].values()} == {2}
scores = {s: t["smart_score"] for s, t in bot3.state["open_trades"].items()}
print("[OK] درجة الجودة محفوظة مع الصفقة:", scores)

# ============ D) ملء المقاعد + سجل التسجيل في ملف الصفقات ============
app_module.compute_entry_signal = lambda df, *a, **k: fake_signal(df.iloc[-1]["symbol"], int(df.iloc[-1]["open_time"]))
bot4, _ = new_bot("D")
for i in range(3):
    t = {"trade_id": f"X{i}", "symbol": f"X{i}USDT", "entry_price": 100.0, "target_price": 102.0,
         "stop_price": 98.5, "entry_time": 1791201600000, "entry_bar_open_time": 1791201600000,
         "last_target_check_ms": 1791201660000, "strong": False, "buy_score": 2, "mode": "متوازن",
         "metrics": {"atr": 1.6}, "atr_at_entry": 1.6, "peak_price": 100.0, "max_favorable_pct": 0.0,
         "reference_target_hit": False, "trail_stop_price": None, "trail_initial_stop_pct": None,
         "smart_score": 2, "entry_hour_local": 1, "halal_verdict": "حلال"}
    bot4.state["open_trades"][t["symbol"]] = t
bot4.store.save(bot4.state)
run_hour(bot4, ["NEW1USDT"], hour_local=0)
assert not bot4.state["open_trades"].get("NEW1USDT"), "المقاعد ممتلئة (3/3) فلا تُفتح صفقة جديدة"
assert rejections(bot4)[-1]["reject_reason"] == "concurrency_cap"
print("[OK] عند امتلاء كل المقاعد: المرشح الجديد يُرفض ويُسجَّل بلا رسالة")

bot5, _ = new_bot("E")
app_module.compute_entry_signal = lambda df, *a, **k: fake_signal(df.iloc[-1]["symbol"], int(df.iloc[-1]["open_time"]))
run_hour(bot5, ["JRN1USDT"], hour_local=0)
bot5.process_new_closed_hour(0)  # لا شيء (لا شمعة جديدة) — يتحقق من عدم التكرار
row = list(csv.DictReader(open(Path(bot5.data_dir) / "trades_log.csv")))[-1]
assert row["smart_score"] != "" and row["entry_hour_local"] == "0", row
print(f"[OK] السجل يحتوي smart_score={row['smart_score']} و entry_hour_local={row['entry_hour_local']}")

print("\nALL SMART ENTRY INTEGRATION TESTS PASSED")
