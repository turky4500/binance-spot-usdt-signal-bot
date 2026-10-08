"""اختبار وضع الهدوء: إسكات رسائل الدخول للنظام القديم مع بقاء التسجيل والمتابعة.

السلوك المتوقع:
  - QUIET_ENTRY_SIGNALS=1 → لا رسالة تيليجرام، لكن الحدث والصفقة يُسجَّلان.
  - الوضع الافتراضي (0) → الرسالة تُرسل طبيعيًا.
"""
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []

    def send_message(self, text: str) -> None:
        self.sent.append(text)


def make_bot(quiet: bool):
    import app as app_mod
    from app import SpotSignalBot

    bot = SpotSignalBot.__new__(SpotSignalBot)
    bot.data_dir = tempfile.mkdtemp()
    bot.quiet_entry_signals = quiet
    bot.quiet_entry_count_today = 0
    bot.telegram = FakeTelegram()
    bot.logger = logging.getLogger("test-quiet")
    bot.events = []
    bot.append_event = lambda t, s, m: bot.events.append((t, s, m))
    bot.get_halal_verdict = lambda s: "حلال"
    bot.config = type("C", (), {"timezone_name": "Asia/Riyadh"})()
    bot.smart_entry = type("S", (), {
        "local_hour": staticmethod(lambda ms: 14),
        "hour_note_ar": staticmethod(lambda h: ""),
    })()
    bot.exit_rules = type("E", (), {"is_hybrid": False, "uses_trailing_stop": False})()
    bot._build_trade_log_record = lambda trade: {"trade_id": "T1", "symbol": trade["symbol"]}
    bot.journal_writes = []
    app_mod.append_trade_entry = lambda d, r: bot.journal_writes.append(r)
    return bot


TRADE = {
    "symbol": "TESTUSDT", "entry_time": 1791450000000, "entry_price": 1.0,
    "target_price": 1.02, "stop_price": 0.985, "strong": False,
}

# 1) الوضع الافتراضي: تُرسل الرسالة
bot = make_bot(quiet=False)
bot.send_entry_message(dict(TRADE))
assert len(bot.telegram.sent) == 1, "الوضع الافتراضي يجب أن يرسل رسالة دخول"
assert bot.events and bot.events[0][0] == "entry"
assert bot.journal_writes, "الصفقة يجب أن تُسجَّل في الدفتر"
print("[OK] الوضع الافتراضي: رسالة أُرسلت + صفقة سُجّلت")

# 2) وضع الهدوء: لا رسالة، لكن التسجيل كامل
bot = make_bot(quiet=True)
bot.send_entry_message(dict(TRADE))
assert bot.telegram.sent == [], f"وضع الهدوء يجب أن يمنع الرسائل! أُرسل {len(bot.telegram.sent)}"
assert bot.quiet_entry_count_today == 1, "العداد يجب أن يزيد في وضع الهدوء"
assert bot.events and bot.events[0][0] == "entry", "الحدث يجب أن يُسجَّل"
assert bot.journal_writes, "الصفقة يجب أن تُسجَّل حتى في وضع الهدوء"
print("[OK] وضع الهدوء: صفر رسائل + الصفقة والحدث سُجّلا + العداد يزيد")

# 3) الرسالة نفسها لها محتوى سليم في الوضع الافتراضي (دخول/هدف/وقف/وقت)
default_bot = make_bot(quiet=False)
default_bot.send_entry_message(dict(TRADE))
text = default_bot.telegram.sent[0]
for needle in ("إشارة دخول شراء", "TESTUSDT", "سعر الدخول", "الهدف المرجعي", "وقف الخسارة"):
    assert needle in text, f"الرسالة يجب أن تحوي «{needle}»"
print("[OK] بنية رسالة الدخول سليمة (زوج/دخول/هدف/وقف)")

print("\nALL QUIET MODE TESTS PASSED")
