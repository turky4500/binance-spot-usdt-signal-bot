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
assert len(s_off.select(candidates, open_trades_count=99)[0]) == 4
print("[OK] تعطيل الدخول الذكي يعيد السلوك القديم")

print("\nALL SMART ENTRY TESTS PASSED")
