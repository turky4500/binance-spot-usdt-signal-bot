"""اختبارات البوابة الزمنية والدخول الذكي."""
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["SMART_ENTRY"] = "1"
os.environ["ENTRY_HOURS"] = "0,1,2"
os.environ["MAX_OPEN_TRADES"] = "3"

from src.smart_entry import SmartEntrySettings  # noqa: E402


def ms_at_local_hour(hour: int) -> int:
    tz = ZoneInfo("Asia/Riyadh")
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=30, second=0, microsecond=0)
    return int(target.astimezone(timezone.utc).timestamp() * 1000)


s = SmartEntrySettings()
print("hours:", s.allowed_hours, "| max:", s.max_open_trades, "|", s.describe())
assert s.allowed_hours == [0, 1, 2]

for h in (0, 1, 2):
    assert s.is_hour_allowed(ms_at_local_hour(h)), f"الساعة {h} يجب أن تكون مسموحة"
for h in (9, 12, 19, 21):
    assert not s.is_hour_allowed(ms_at_local_hour(h)), f"الساعة {h} يجب أن تكون ممنوعة"
print("[OK] البوابة الزمنية تعمل بتوقيت الرياض (00/01/02 مسموحة، والبقية ممنوعة)")

# درجة الجودة
score_high = s.quality_score({"plus_di": 30, "minus_di": 10, "adx": 30, "rsi": 55, "relative_volume": 1.5}, buy_score=3)
score_low = s.quality_score({"plus_di": 10, "minus_di": 30, "adx": 15, "rsi": 80, "relative_volume": 0.5}, buy_score=2)
print(f"score high={score_high} low={score_low}")
assert score_high == 5 and score_low == 0

# الترتيب والحد الأقصى
candidates = [
    {"symbol": "LOW", "signal": {"buy_score": 2, "metrics": {"plus_di": 5, "minus_di": 20, "relative_volume": 0.5}}},
    {"symbol": "MID", "signal": {"buy_score": 2, "metrics": {"plus_di": 30, "minus_di": 10, "adx": 26, "relative_volume": 1.3}}},
    {"symbol": "TOP", "signal": {"buy_score": 3, "metrics": {"plus_di": 40, "minus_di": 5, "adx": 30, "rsi": 55, "relative_volume": 1.8}}},
    {"symbol": "MID2", "signal": {"buy_score": 2, "metrics": {"plus_di": 30, "minus_di": 10, "adx": 26, "relative_volume": 1.9}}},
]
selected, dropped = s.select(candidates, open_trades_count=1)
print("selected:", [c["symbol"] for c in selected], "| dropped:", [c["symbol"] for c in dropped])
assert [c["symbol"] for c in selected] == ["TOP", "MID2"], "يجب اختيار الأعلى جودة ثم الأعلى حجمًا"
assert [c["symbol"] for c in dropped] == ["MID", "LOW"]
print("[OK] الترتيب وحد الصفقات المتزامنة (3 - 1 مفتوحة = مقعدان)")

# عند امتلاء الحد بالكامل
selected_full, dropped_full = s.select(candidates, open_trades_count=3)
assert selected_full == [] and len(dropped_full) == 4
print("[OK] عند بلوغ الحد الأقصى تُرفض كل المرشحات وتُسجَّل")

# التعطيل
os.environ["SMART_ENTRY"] = "0"
s_off = SmartEntrySettings()
assert s_off.is_hour_allowed(ms_at_local_hour(12)), "عند التعطيل يجب السماح بكل الساعات"
# البوابة الزمنية والسقف مستقلان: تعطيل البوابة لا يلغي السقف
sel_off, dropped_off = s_off.select(candidates, open_trades_count=99)
assert sel_off == [] and len(dropped_off) == 4, "مع سقف 3 و99 صفقة مفتوحة لا يُفتح جديد"
sel_off2, _ = s_off.select(candidates, open_trades_count=0)
assert len(sel_off2) == 3, "السقف 3 يسمح بثلاث صفقات"
print("[OK] تعطيل البوابة الزمنية لا يلغي سقف الصفقات (مستقلان)")

# ============ الوضع الافتراضي الجديد: 24 ساعة وبلا سقف (قرار المستخدم) ============
for key in ("SMART_ENTRY", "ENTRY_HOURS", "MAX_OPEN_TRADES"):
    os.environ.pop(key, None)
s_def = SmartEntrySettings()
print("\nالافتراضي:", s_def.describe())
assert s_def.enabled is False, "البوابة الزمنية يجب أن تكون معطّلة افتراضيًا"
assert s_def.has_open_trade_cap is False, "السقف يجب أن يكون مفتوحًا افتراضيًا (0)"
for h in range(24):
    assert s_def.is_hour_allowed(ms_at_local_hour(h)), f"الساعة {h} يجب أن تُسمح في وضع 24 ساعة"
print("[OK] الوضع الافتراضي: 24 ساعة كاملة مسموحة (لا تفويت لإشارات النوم)")

selected_all, dropped_all = s_def.select(candidates, open_trades_count=50)
assert len(selected_all) == 4 and dropped_all == [], "مع سقف مفتوح: لا يُرفض أي مرشح"
assert [c["symbol"] for c in selected_all][0] == "TOP", "يُرتَّب الأقوى أولًا (للقراءة والترتيب)"
print("[OK] مع سقف مفتوح: كل المرشحات تُقبل ولا يُرفض أحد (الترتيب بالجودة يبقى)")

os.environ["MAX_OPEN_TRADES"] = "3"
s_cap = SmartEntrySettings()
sel, drp = s_cap.select(candidates, open_trades_count=1)
assert len(sel) == 2 and len(drp) == 2, "السقف يعمل عند تحديده برقم"
os.environ.pop("MAX_OPEN_TRADES", None)

# ============ تصنيف الساعات تاريخيًا (يظهر في كل رسالة دخول) ============
expect = {0: "strong", 2: "strong", 5: "strong", 22: "strong", 23: "strong",
          19: "weak", 9: "weak", 16: "weak", 1: "normal", 6: "normal", 18: "normal"}
for hour, want in expect.items():
    got = SmartEntrySettings.hour_verdict(hour)
    assert got == want, f"الساعة {hour}: توقعت {want} وحصلت {got}"
note = s_def.hour_note_ar(0)
assert "🟢" in note and "قوية" in note and "+0.327" in note, note
note_weak = s_def.hour_note_ar(19)
assert "🔴" in note_weak and "ضعيفة" in note_weak and "-0.388" in note_weak, note_weak
print(f"[OK] تصنيف الساعات: 0/2/5/22/23 قوية • 1/6/18 متوسطة • 19/9/16 ضعيفة")
print(f"     مثال رسالة: {note}")
print(f"     مثال رسالة: {note_weak}")

print("\nALL SMART ENTRY TESTS PASSED")
