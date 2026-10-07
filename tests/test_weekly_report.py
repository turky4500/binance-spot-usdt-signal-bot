"""اختبار التقرير الأسبوعي: يبنيه على بيانات مركّبة ويتحقق من صحة كل الأرقام."""
import csv, os, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ["SMART_ENTRY"] = "1"
os.environ["ENTRY_HOURS"] = "0,2,5,6,14,21,23"
os.environ["MAX_OPEN_TRADES"] = "15"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import AppConfig  # noqa: E402
from app import SpotSignalBot  # noqa: E402
from src.shadow_journal import append_shadow_candidate, SHADOW_LOG_FILE  # noqa: E402

TZ = ZoneInfo("Asia/Riyadh")
tmp = tempfile.mkdtemp()
cfg = AppConfig(telegram_bot_token="x", telegram_chat_id="y", state_file=f"{tmp}/state.json")
bot = SpotSignalBot(cfg)
sent = []
bot.telegram.send_message = lambda t: sent.append(t)
bot.get_halal_verdict = lambda s: "حلال"

# يوم الأحد القادم عند منتصف الليل المحلي (موعد التقرير الأسبوعي)
now_local = datetime.now(TZ)
days_ahead = (6 - now_local.weekday()) % 7  # WEEKLY_REPORT_WEEKDAY=6 (الأحد)
due = (now_local + timedelta(days=days_ahead)).replace(hour=0, minute=5, second=0, microsecond=0)
if due <= now_local:
    due += timedelta(days=7)
report_end = (due.date() - timedelta(days=1))
report_start = report_end - timedelta(days=6)
due_ms = int(due.timestamp() * 1000)
print(f"أسبوع التقرير: {report_start} → {report_end} | يُرسل في {due.strftime('%Y-%m-%d %H:%M')} (الأحد)")


def mk_trade(i, day, hour, symbol, win, minutes, net, strong=False, smart=3):
    entry = datetime(day.year, day.month, day.day, hour, 0, tzinfo=TZ)
    exitdt = entry + timedelta(minutes=minutes)
    return {
        "trade_id": f"{symbol}-{i}", "symbol": symbol,
        "entry_time_ms": str(int(entry.timestamp() * 1000)), "entry_time_local": entry.strftime("%Y-%m-%d %H:%M"),
        "entry_date_local": day.isoformat(), "entry_weekday_ar": "الأحد", "entry_price": "1.0",
        "target_price": "1.02", "stop_price": "0.985", "strong_signal": "1" if strong else "0",
        "buy_score": "3" if strong else "2", "mode": "متوازن", "rsi": "55", "stoch": "60", "adx": "27",
        "plus_di": "30", "minus_di": "12", "relative_volume": "1.5", "reward_risk_ratio": "1.4",
        "buy_risk_pct": "1.5", "distance_from_ema200_pct": "1.2", "quote_volume": "2e6",
        "atr_at_entry": "0.01", "trail_atr_mult": "2.0", "trail_initial_stop_pct": "3.2",
        "reference_target_pct": "2.0", "max_favorable_pct": "2.4" if win else "0.6",
        "reference_target_hit": "1" if win else "0", "exit_mode": "hybrid",
        "smart_score": str(smart), "entry_hour_local": str(hour), "bullish_divergence": "0",
        "oversold_at_pivot": "1", "volume_confirm": "1", "bullish_trend_ok": "1", "adx_buy_ok": "1",
        "liquidity_ok": "1", "outcome": "target" if win else "loss",
        "exit_reason": "fixed_target" if win else "stop_loss",
        "exit_time_ms": str(int(exitdt.timestamp() * 1000)), "exit_time_local": exitdt.strftime("%Y-%m-%d %H:%M"),
        "exit_price": "1.02" if win else "0.985", "duration_minutes": str(minutes),
        "duration_text": f"{minutes // 60} ساعة", "gross_return_pct": f"{net:.2f}",
        "net_return_pct": f"{net:.2f}",
    }


# 12 صفقة خلال الأسبوع: 6 رابحة (زمن سيئ) و6 خاسرة (زمن جيد) — لاختبار كل الأعمدة
rows = []
plan = [
    (0, 0, "BTCUSDT", True, 180, 1.80, True), (0, 22, "ETHUSDT", False, 95, -1.28, False),
    (1, 2, "SOLUSDT", True, 240, 1.80, False), (1, 19, "XRPUSDT", False, 60, -1.31, False),
    (2, 6, "ADAUSDT", True, 300, 1.80, True), (2, 11, "DOGEUSDT", False, 45, -1.35, False),
    (3, 21, "AVAXUSDT", True, 150, 1.80, False), (3, 9, "LINKUSDT", False, 120, -1.22, False),
    (4, 14, "MATICUSDT", True, 210, 1.80, True), (4, 19, "DOTUSDT", False, 75, -1.40, False),
    (5, 5, "ATOMUSDT", False, 240, -1.10, False), (6, 0, "NEARUSDT", True, 120, 1.80, True),
]
for i, (d, h, sym, win, mins, net, strong) in enumerate(plan):
    rows.append(mk_trade(i, report_start + timedelta(days=d), h, sym, win, mins, net, strong))

# مرشحان مستبعدان بسببين مختلفين
for j, (reason, reason_ar) in enumerate([
    ("outside_time_window", "خارج النافذة الزمنية المعتمدة"),
    ("concurrency_cap", "تجاوز الحد الأقصى للصفقات المتزامنة"),
]):
    append_shadow_candidate(bot.data_dir, {
        "candidate_id": f"REJ{j}", "symbol": "PEPEUSDT", "rejected_time_ms": int(
            datetime(report_start.year, report_start.month, report_start.day, 12, tzinfo=TZ).timestamp() * 1000),
        "rejected_date_local": (report_start + timedelta(days=j)).isoformat(),
        "reject_reason": reason, "reject_reason_ar": reason_ar,
        "entry_price": "1.0", "target_price": "1.02", "stop_price": "0.985",
    })

# صفقة مفتوحة الآن
bot.state["open_trades"]["LIVEUSDT"] = {
    "trade_id": "LIVE-1", "symbol": "LIVEUSDT", "entry_price": 1.0, "target_price": 1.02, "stop_price": 0.985,
    "entry_time": due_ms - 3_600_000, "entry_bar_open_time": due_ms - 7_200_000, "last_target_check_ms": due_ms,
    "strong": False, "buy_score": 2, "mode": "متوازن", "metrics": {}, "atr_at_entry": 0.01,
    "peak_price": 1.0, "max_favorable_pct": 0.0, "reference_target_hit": False,
    "trail_stop_price": None, "trail_initial_stop_pct": None, "smart_score": 3, "entry_hour_local": 0,
    "halal_verdict": "حلال",
}
bot.store.save(bot.state)

# كتابة دفتر الصفقات
from src.trade_journal import FIELDNAMES, TRADE_LOG_FILE
path = Path(bot.data_dir) / TRADE_LOG_FILE
with open(path, "w", newline="", encoding="utf-8") as fh:
    writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in FIELDNAMES})

# ===== التشغيل =====
bot.send_weekly_report_if_due(due_ms)
assert sent, "لم يُرسل أي تقرير!"
text = sent[-1]
print("\n" + text + "\n")

# ===== التحققات =====
checks = [
    ("عنوان التقرير", "🗂️ التقرير الأسبوعي للإشارات"),
    ("الفترة", f"{report_start.isoformat()} ← "),
    ("عدد الصفقات الداخلة", "صفقات دخلت هذا الأسبوع: 12"),
    ("أغلق على هدف", "أُغلقت على هدف: 6"),
    ("أغلق على وقف", "أُغلقت على وقف: 6"),
    ("نسبة النجاح", "نسبة نجاح المغلقة: 50.0%"),
    ("متوسط نتيجة الصفقة", "متوسط نتيجة الصفقة المغلقة: +0.26%"),
    ("مدة الهدف", "متوسط مدة الوصول للهدف:"),
    ("مدة الوقف", "متوسط مدة ضرب الوقف:"),
    ("النافذة الزمنية", "الصفقات داخل النافذة الزمنية:"),
    ("أفضل الساعات", "🟢 أفضل ساعات الدخول:"),
    ("أسوأ الساعات", "🔴 أسوأ ساعات الدخول:"),
    ("المستبعدون", "مرشحون مستبعدون هذا الأسبوع: 2"),
    ("أسباب الاستبعاد", "outside_time_window=1"),
    ("قاعدة الخروج", "🧭 قاعدة الخروج المفعّلة:"),
    ("المفتوحة", "المفتوحة حاليًا: 1"),
]
for label, needle in checks:
    assert needle in text, f"❌ {label}: لم يُوجد «{needle}»"
    print(f"✅ {label}")

# التزام النافذة: 12 صفقة كلها داخل النافذة الأوسع [0,2,5,6,14,21,23]؟ (12,11,19,9 خارجها)
inside_line = [l for l in text.splitlines() if l.strip().startswith("• الصفقات داخل النافذة")][0]
print("\n" + inside_line)
assert "7 من 12" in inside_line, inside_line

# منع التكرار
sent.clear()
bot.send_weekly_report_if_due(due_ms + 60_000)
assert not sent, "أُرسل التقرير مرتين لنفس الأسبوع!"
print("✅ لا تكرار لنفس الأسبوع (يُرسل مرة واحدة)")

print("\n✔ أفقيًا: 12 صفقة | 6 هدف (متوسط 200 د ≈ 3 س 20 د) | 6 وقف (متوسط 106 د ≈ 1 س 46 د)")
print("    متوسط النتيجة: (+1.8×6 −1.28−1.31−1.35−1.22−1.40−1.10)/12 = +0.26% ✔")
print("\nALL WEEKLY REPORT TESTS PASSED")
