"""اختبارات التقرير اليومي لـ NW 1H بعد تعديل 2026-11-08.

* 12:00 AM بتوقيت الرياض، وعن **اليوم السابق** لا اليوم الحالي.
* **أرقام فقط** بصيغة تقارير المستودع: إجمالي التوصيات المرسلة لليوم /
  مفتوحة / مغلقة / نجاح / خسارة / نسبة النجاح / إجمالي الربح أو الخسارة.
  بلا أي قائمة صفقات.
* بلا «دورات» و«تنبيهات»، وبلا تراكم للصفقات المُبلَّغ عنها سابقًا.

هذا الملف جديد بالكامل، لا يعدّل اختبارات المستخدم.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src import nw_daily_report as dr
from src.nw_messages import fmt_pct
from src.nw_state import NWStateStore
from src.trade_analysis import success_rate_percent

RIYADH = ZoneInfo("Asia/Riyadh")


def ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=RIYADH).timestamp() * 1000)


class FakeTelegram:
    def __init__(self, fail: bool = False) -> None:
        self.messages: list[str] = []
        self.fail = fail

    def send_message(self, text: str):
        if self.fail:
            raise OSError("telegram down")
        self.messages.append(text)
        return {"ok": True}


class FakeBot:
    def __init__(self, tmp_path, fail: bool = False) -> None:
        self.store = NWStateStore(str(tmp_path / "state.json"))
        self.trade_log = str(tmp_path / "trades.csv")
        self.tg = FakeTelegram(fail)


def write_trades(path, rows):
    fields = ["symbol", "entry_time", "entry_price", "exit_time", "exit_price",
              "gross_pct", "net_pct", "entries_count", "exit_reason",
              "signal_bar_time", "confirm_bar_time"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def trade(symbol, exit_ms, net, entry_price=1.0, exit_price=1.1):
    return {
        "symbol": symbol,
        "entry_time": exit_ms - 7_200_000,
        "entry_price": entry_price,
        "exit_time": exit_ms,
        "exit_price": exit_price,
        "net_pct": net,
        "gross_pct": net + 0.2,
        "entries_count": 1,
        "exit_reason": "red_label",
        "signal_bar_time": exit_ms - 3_600_000,
        "confirm_bar_time": exit_ms,
    }


def with_open_positions(bot, *symbols):
    state = bot.store.load()
    for sym in symbols:
        state["symbols"][sym] = {
            "position": {"entry_price": 0.707, "entry_time": ms(2026, 11, 8, 2, 0),
                         "entry_count": 1},
            "entry_count": 1,
        }
    bot.store.save(state)


def with_sent_keys(bot, *keys):
    state = bot.store.load()
    state["sent"] = list(keys)
    bot.store.save(state)


# ── التوقيت ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hour,minute", [
    (0, 1), (0, 30), (0, 59),      # 12:00–12:59 AM الرياض ⇒ يُرسل
])
def test_sends_only_in_the_midnight_hour_riyadh(tmp_path, hour, minute):
    bot = FakeBot(tmp_path)
    text = dr.send_daily_report_if_due(bot, ms(2026, 11, 9, hour, minute))
    assert text is not None
    assert len(bot.tg.messages) == 1


@pytest.mark.parametrize("hour,minute", [
    (1, 0), (3, 35), (12, 0), (23, 59),   # أي ساعة أخرى ⇒ لا يُرسل
])
def test_no_report_outside_midnight_hour_riyadh(tmp_path, hour, minute):
    bot = FakeBot(tmp_path)
    assert dr.send_daily_report_if_due(bot, ms(2026, 11, 9, hour, minute)) is None
    assert bot.tg.messages == []


def test_midnight_riyadh_is_21_utc(tmp_path):
    """12:00 AM الرياض = 21:00 UTC (لا 00:00 UTC كما كان)."""
    bot = FakeBot(tmp_path)
    now = ms(2026, 11, 9, 0, 5)
    assert datetime.fromtimestamp(now / 1000, ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M") \
        == "2026-11-08 21:05"
    assert dr.send_daily_report_if_due(bot, now) is not None


def test_report_date_is_previous_riyadh_day(tmp_path):
    bot = FakeBot(tmp_path)
    text = dr.send_daily_report_if_due(bot, ms(2026, 11, 9, 0, 10))
    assert "`2026-11-08`" in text
    assert "`2026-11-09`" not in text
    assert "12:00 AM" in text


def test_no_second_report_for_same_day(tmp_path):
    bot = FakeBot(tmp_path)
    assert dr.send_daily_report_if_due(bot, ms(2026, 11, 9, 0, 10)) is not None
    assert dr.send_daily_report_if_due(bot, ms(2026, 11, 9, 0, 40)) is None
    assert len(bot.tg.messages) == 1
    assert bot.store.load()["daily_report_nw"]["last_report_day"] == "2026-11-08"


# ── المحتوى: أرقام فقط ─────────────────────────────────────────────────────

def test_report_contains_only_the_requested_numbers(tmp_path):
    bot = FakeBot(tmp_path)
    with_open_positions(bot, "ASRUSDT", "HMSTRUSDT")
    write_trades(bot.trade_log, [
        trade("SIGNUSDT", ms(2026, 11, 8, 5, 0), 8.5558, 0.0123, 0.0134),
        trade("STRAXUSDT", ms(2026, 11, 6, 17, 0), -1.195, 0.05, 0.049),
    ])

    text = dr.send_daily_report_if_due(bot, ms(2026, 11, 9, 0, 10))
    assert "📥 إجمالي التوصيات المرسلة لليوم: 0" in text
    assert "📌 مفتوحة حاليًا: 2" in text
    assert "🔒 الصفقات المغلقة: 2" in text
    assert "✅ نجاح: 1 • 🛑 خسارة: 1" in text
    assert "📈 نسبة النجاح: 50.0%" in text
    assert f"`{fmt_pct(8.5558 - 1.195)}`" in text

    # لا قائمة صفقات: لا أسماء عملات، ولا أسعار، ولا أوقات، ولا أسطر تبدأ بنقطة
    for forbidden in ("ASRUSDT", "HMSTRUSDT", "SIGNUSDT", "STRAXUSDT",
                      "0.0123", "0.0134", "52.07", "🕒", "←"):
        assert forbidden not in text, forbidden
    assert not any(line.startswith("•") for line in text.splitlines())


def test_report_uses_repo_success_rate_helper(tmp_path):
    """نسبة النجاح مأخوذة من src/trade_analysis.py كما في تقارير المستودع."""
    bot = FakeBot(tmp_path)
    write_trades(bot.trade_log, [
        trade("AUSDT", ms(2026, 11, 8, 1, 0), 3.0),
        trade("BUSDT", ms(2026, 11, 8, 2, 0), 5.0),
        trade("CUSDT", ms(2026, 11, 8, 3, 0), 1.0),
        trade("DUSDT", ms(2026, 11, 8, 4, 0), -2.0),
    ])
    text = dr.send_daily_report_if_due(bot, ms(2026, 11, 9, 0, 10))
    expected = success_rate_percent(3, 1)
    assert f"📈 نسبة النجاح: {expected:.1f}%" in text
    assert "✅ نجاح: 3 • 🛑 خسارة: 1" in text


def test_break_even_trade_is_neither_win_nor_loss(tmp_path):
    bot = FakeBot(tmp_path)
    write_trades(bot.trade_log, [
        trade("WINUSDT", ms(2026, 11, 8, 1, 0), 4.0),
        trade("EVENUSDT", ms(2026, 11, 8, 2, 0), 0.0),
    ])
    text = dr.send_daily_report_if_due(bot, ms(2026, 11, 9, 0, 10))
    assert "🔒 الصفقات المغلقة: 2" in text
    assert "✅ نجاح: 1 • 🛑 خسارة: 0" in text
    assert "📈 نسبة النجاح: 100.0%" in text


def test_no_cycles_and_no_alerts_lines(tmp_path):
    bot = FakeBot(tmp_path)
    state = bot.store.load()
    state["cycles"] = 103
    state["messages_sent"] = 111
    bot.store.save(state)

    text = dr.send_daily_report_if_due(bot, ms(2026, 11, 9, 0, 10))
    assert "دورات" not in text
    assert "التنبيهات" not in text
    assert "103" not in text and "111" not in text


def test_empty_report_is_all_zeros(tmp_path):
    bot = FakeBot(tmp_path)
    text = dr.send_daily_report_if_due(bot, ms(2026, 11, 9, 0, 10))
    assert "📥 إجمالي التوصيات المرسلة لليوم: 0" in text
    assert "📌 مفتوحة حاليًا: 0" in text
    assert "🔒 الصفقات المغلقة: 0" in text
    assert "✅ نجاح: 0 • 🛑 خسارة: 0" in text
    assert "📈 نسبة النجاح: 0.0%" in text
    assert "`+0.00%`" in text


def test_text_is_short_and_has_no_trade_list(tmp_path):
    bot = FakeBot(tmp_path)
    with_open_positions(bot, *[f"S{i}USDT" for i in range(30)])
    text = dr.send_daily_report_if_due(bot, ms(2026, 11, 9, 0, 10))
    assert len(text) < 400
    assert len(text.splitlines()) <= 10


# ── منع التراكم ────────────────────────────────────────────────────────────

def test_trade_closed_after_midnight_belongs_to_next_report(tmp_path):
    bot = FakeBot(tmp_path)
    write_trades(bot.trade_log, [trade("LATEUSDT", ms(2026, 11, 9, 0, 30), 3.0)])
    text = dr.send_daily_report_if_due(bot, ms(2026, 11, 9, 0, 45))
    assert "🔒 الصفقات المغلقة: 0" in text
    text2 = dr.send_daily_report_if_due(bot, ms(2026, 11, 10, 0, 10))
    assert "🔒 الصفقات المغلقة: 1" in text2


def test_closed_trade_already_reported_is_never_counted_again(tmp_path):
    bot = FakeBot(tmp_path)
    write_trades(bot.trade_log, [trade("SIGNUSDT", ms(2026, 11, 8, 5, 0), 8.5558)])

    day1 = dr.send_daily_report_if_due(bot, ms(2026, 11, 9, 0, 10))
    assert "🔒 الصفقات المغلقة: 1" in day1
    day2 = dr.send_daily_report_if_due(bot, ms(2026, 11, 10, 0, 10))
    assert "🔒 الصفقات المغلقة: 0" in day2
    assert "`+0.00%`" in day2          # الإجمالي لا يتراكم
    day3 = dr.send_daily_report_if_due(bot, ms(2026, 11, 11, 0, 10))
    assert "🔒 الصفقات المغلقة: 0" in day3


def test_only_new_closed_trades_are_counted_in_later_report(tmp_path):
    bot = FakeBot(tmp_path)
    old = trade("OLDUSDT", ms(2026, 11, 8, 5, 0), 8.5558)
    write_trades(bot.trade_log, [old])
    assert "🔒 الصفقات المغلقة: 1" in dr.send_daily_report_if_due(bot, ms(2026, 11, 9, 0, 10))

    new = trade("NEWUSDT", ms(2026, 11, 9, 14, 0), -2.1973)
    write_trades(bot.trade_log, [old, new])
    day2 = dr.send_daily_report_if_due(bot, ms(2026, 11, 10, 0, 10))
    assert "🔒 الصفقات المغلقة: 1" in day2
    assert "✅ نجاح: 0 • 🛑 خسارة: 1" in day2
    assert "`-2.20%`" in day2


def test_reported_keys_are_persisted_and_deduplicated(tmp_path):
    bot = FakeBot(tmp_path)
    row = trade("DUPUSDT", ms(2026, 11, 8, 5, 0), 3.0839)
    write_trades(bot.trade_log, [row, dict(row)])       # صف مكرر في الدفتر
    text = dr.send_daily_report_if_due(bot, ms(2026, 11, 9, 0, 10))
    assert "🔒 الصفقات المغلقة: 1" in text
    keys = bot.store.load()["daily_report_nw"]["reported_trade_keys"]
    assert len(keys) == 1 and keys[0].startswith("DUPUSDT:")


def test_failed_send_does_not_mark_trade_as_reported(tmp_path):
    bot = FakeBot(tmp_path, fail=True)
    write_trades(bot.trade_log, [trade("SIGNUSDT", ms(2026, 11, 8, 5, 0), 8.5558)])
    assert dr.send_daily_report_if_due(bot, ms(2026, 11, 9, 0, 10)) is None
    assert "daily_report_nw" not in bot.store.load()
    # دورة لاحقة تعيد المحاولة بعد عودة تيليجرام
    bot.tg.fail = False
    assert "🔒 الصفقات المغلقة: 1" in dr.send_daily_report_if_due(bot, ms(2026, 11, 9, 0, 40))


def test_missing_trade_log_is_safe(tmp_path):
    bot = FakeBot(tmp_path)
    assert dr.send_daily_report_if_due(bot, ms(2026, 11, 9, 0, 10)) is not None


# ── الربط مع nw_bot ────────────────────────────────────────────────────────

def test_nw_bot_delegates_to_new_report_module():
    import inspect
    from nw_bot import NWBot

    src = inspect.getsource(NWBot.send_daily_report_if_due)
    assert "src.nw_daily_report" in src
    assert "_send(self, now_ms)" in src
    assert "cycles" not in src and "messages_sent" not in src
    assert "datetime.timezone.utc" not in src


def test_15m_bot_sends_its_own_report_at_same_time(monkeypatch, tmp_path):
    """تقرير 15M مستقل: نفس التوقيت والصيغة، بعنوانه ومفتاح حالته."""
    from src.nw_15m_bot import (
        FRAME_LABEL_15M, REPORT_STATE_KEY_15M, build_nw_15m_bot,
    )

    monkeypatch.setenv("TELEGRAM_MUTE", "1")
    state15 = tmp_path / "state15.json"
    log15 = tmp_path / "trades15.csv"
    monkeypatch.setenv("NW_STATE_FILE", str(state15))
    monkeypatch.setenv("NW_TRADE_LOG", str(log15))
    bot = build_nw_15m_bot()
    bot.tg = FakeTelegram()

    write_trades(str(log15), [trade("S15USDT", ms(2026, 11, 8, 5, 0), 3.0)])
    with_open_positions(bot, "OPEN15USDT")

    assert bot.send_daily_report_if_due(ms(2026, 11, 9, 0, 10)) is None
    text = bot.tg.messages[-1]
    assert f"({FRAME_LABEL_15M})" in text
    assert "(NW 1H)" not in text
    assert "📥 إجمالي التوصيات المرسلة لليوم: 0" in text
    assert "📌 مفتوحة حاليًا: 1" in text
    assert "🔒 الصفقات المغلقة: 1" in text
    assert "✅ نجاح: 1 • 🛑 خسارة: 0" in text
    assert "📈 نسبة النجاح: 100.0%" in text
    assert "`+3.00%`" in text
    assert "12:00 AM" in text and "`2026-11-08`" in text

    saved = json.loads(state15.read_text(encoding="utf-8"))
    assert saved[REPORT_STATE_KEY_15M]["last_report_day"] == "2026-11-08"
    assert len(saved[REPORT_STATE_KEY_15M]["reported_trade_keys"]) == 1
    assert "daily_report_nw" not in saved      # لا يلمس مفتاح 1H


def test_15m_report_does_not_accumulate_and_is_isolated_from_1h(monkeypatch, tmp_path):
    """لا تراكم داخل 15M، وسجل 1H لا يتأثر إطلاقًا."""
    from src.nw_15m_bot import REPORT_STATE_KEY_15M, build_nw_15m_bot

    monkeypatch.setenv("TELEGRAM_MUTE", "1")
    state15 = tmp_path / "state15.json"
    log15 = tmp_path / "trades15.csv"
    monkeypatch.setenv("NW_STATE_FILE", str(state15))
    monkeypatch.setenv("NW_TRADE_LOG", str(log15))
    bot = build_nw_15m_bot()
    bot.tg = FakeTelegram()
    write_trades(str(log15), [trade("S15USDT", ms(2026, 11, 8, 5, 0), 3.0)])

    bot.send_daily_report_if_due(ms(2026, 11, 9, 0, 10))
    bot.send_daily_report_if_due(ms(2026, 11, 9, 0, 40))   # نفس اليوم
    bot.send_daily_report_if_due(ms(2026, 11, 10, 0, 10))  # اليوم التالي
    assert len(bot.tg.messages) == 2
    assert "🔒 الصفقات المغلقة: 0" in bot.tg.messages[-1]
    assert "`+0.00%`" in bot.tg.messages[-1]

    # بوت 1H بحالة منفصلة: تقريره يعمل ولم يتأثر مفتاحه
    bot1h = FakeBot(tmp_path / "1h")
    text1h = dr.send_daily_report_if_due(bot1h, ms(2026, 11, 9, 0, 10))
    assert "(NW 1H)" in text1h and "(NW 15M)" not in text1h
    assert "daily_report_nw" in bot1h.store.load()
    assert REPORT_STATE_KEY_15M not in bot1h.store.load()


# ── إجمالي التوصيات المرسلة لليوم (طلب المستخدم 2026-11-08) ────────────────

def test_total_sent_today_counts_only_this_days_entry_messages(tmp_path):
    """يُعدّ كل buy: داخل اليوم فقط، بلا خروج ولا عدّاد تراكمي ولا مفاتيح تالفة."""
    bot = FakeBot(tmp_path)
    day_start = ms(2026, 11, 8, 0, 0)
    # زمن الرسالة = bar_time + ساعة (1H) ⇒ confirm_bar_time المطبوع في الرسالة
    inside = day_start - 3_600_000                       # ⇒ 00:00 داخل اليوم
    before = day_start - 7_200_000                       # ⇒ 23:00 اليوم السابق
    after = ms(2026, 11, 9, 0, 0) - 3_600_000            # ⇒ 00:00 اليوم التالي
    state = bot.store.load()
    state["messages_sent"] = 111
    bot.store.save(state)
    with_sent_keys(bot,
                   f"buy:AUSDT:{inside}:1",
                   f"buy:BUSDT:{inside + 3_600_000}:1",   # ⇒ 01:00 داخل اليوم
                   f"buy:AUSDT:{inside}:2",                # «إشارة دخول ثانية»
                   f"buy:DUSDT:{before}:1",
                   f"buy:EUSDT:{after}:1",
                   f"exit:FUSDT:{inside}",                 # الخروج لا يُعدّ
                   "buy:broken", "garbage")               # مفاتيح غير صالحة

    text = dr.send_daily_report_if_due(bot, ms(2026, 11, 9, 0, 10))
    assert "📥 إجمالي التوصيات المرسلة لليوم: 3" in text
    assert "111" not in text                              # لا messages_sent
    assert "دورات" not in text and "التنبيهات" not in text


def test_total_sent_today_does_not_accumulate_to_next_day(tmp_path):
    """اليوم التالي يبدأ من صفر، ولا يُعاد عدّ توصيات اليوم السابق."""
    bot = FakeBot(tmp_path)
    with_sent_keys(bot, f"buy:AUSDT:{ms(2026, 11, 8, 2, 0) - 3_600_000}:1")

    day1 = dr.send_daily_report_if_due(bot, ms(2026, 11, 9, 0, 10))
    assert "📥 إجمالي التوصيات المرسلة لليوم: 1" in day1
    day2 = dr.send_daily_report_if_due(bot, ms(2026, 11, 10, 0, 10))
    assert "📥 إجمالي التوصيات المرسلة لليوم: 0" in day2


def test_15m_total_uses_15_minute_interval(monkeypatch, tmp_path):
    """شمعة 23:15 ⇒ رسالة 23:30 داخل اليوم بـ15m، و00:15 خارجه بـ1h."""
    from src.nw_15m_bot import INTERVAL_MS_15M, build_nw_15m_bot

    monkeypatch.setenv("TELEGRAM_MUTE", "1")
    monkeypatch.setenv("NW_STATE_FILE", str(tmp_path / "state15.json"))
    monkeypatch.setenv("NW_TRADE_LOG", str(tmp_path / "trades15.csv"))
    bot = build_nw_15m_bot()
    bot.tg = FakeTelegram()
    bar = ms(2026, 11, 8, 23, 15)
    with_sent_keys(bot, f"buy:S15USDT:{bar}:1")
    assert INTERVAL_MS_15M == 900_000

    assert bot.send_daily_report_if_due(ms(2026, 11, 9, 0, 10)) is None
    text15 = bot.tg.messages[-1]
    assert "📥 إجمالي التوصيات المرسلة لليوم: 1" in text15
    assert "(NW 15M)" in text15

    # نفس المفتاح مع طول شمعة 1H ⇒ زمن الرسالة 00:15 من اليوم التالي ⇒ لا يُعدّ
    bot1h = FakeBot(tmp_path / "1h")
    with_sent_keys(bot1h, f"buy:S15USDT:{bar}:1")
    text1h = dr.send_daily_report_if_due(bot1h, ms(2026, 11, 9, 0, 10))
    assert "📥 إجمالي التوصيات المرسلة لليوم: 0" in text1h


def test_build_report_text_accepts_explicit_sent_today():
    """الدالة العامة تبقى قابلة للاستدعاء مباشرة مع العدّاد الجديد."""
    text = dr.build_report_text("2026-11-08", {"symbols": {}}, [], "NW 1H", 20)
    assert "📥 إجمالي التوصيات المرسلة لليوم: 20" in text


# ── تقرير فريم 4H المستقل (طلب المستخدم 2026-11-09) ─────────────────────────

def test_4h_bot_sends_its_own_report_at_same_time(monkeypatch, tmp_path):
    """تقرير 4H مستقل: نفس توقيت/صيغة 1H، بعنوانه ومفتاح حالته."""
    from src.nw_4h_bot import (
        FRAME_LABEL_4H, REPORT_STATE_KEY_4H, build_nw_4h_bot,
    )

    monkeypatch.setenv("TELEGRAM_MUTE", "1")
    state4 = tmp_path / "state4.json"
    log4 = tmp_path / "trades4.csv"
    monkeypatch.setenv("NW_STATE_FILE", str(state4))
    monkeypatch.setenv("NW_TRADE_LOG", str(log4))
    bot = build_nw_4h_bot()
    bot.tg = FakeTelegram()

    write_trades(str(log4), [trade("S4HUSDT", ms(2026, 11, 8, 5, 0), 3.0)])
    with_open_positions(bot, "OPEN4HUSDT")

    assert bot.send_daily_report_if_due(ms(2026, 11, 9, 0, 10)) is None
    text = bot.tg.messages[-1]
    assert f"({FRAME_LABEL_4H})" in text
    assert "(NW 1H)" not in text and "(NW 15M)" not in text
    assert "📥 إجمالي التوصيات المرسلة لليوم: 0" in text
    assert "📌 مفتوحة حاليًا: 1" in text
    assert "🔒 الصفقات المغلقة: 1" in text
    assert "✅ نجاح: 1 • 🛑 خسارة: 0" in text
    assert "📈 نسبة النجاح: 100.0%" in text
    assert "`+3.00%`" in text
    assert "12:00 AM" in text and "`2026-11-08`" in text

    saved = json.loads(state4.read_text(encoding="utf-8"))
    assert saved[REPORT_STATE_KEY_4H]["last_report_day"] == "2026-11-08"
    assert len(saved[REPORT_STATE_KEY_4H]["reported_trade_keys"]) == 1
    assert "daily_report_nw" not in saved       # لا يلمس مفتاح 1H
    assert "daily_report_nw15" not in saved     # ولا مفتاح 15M


def test_4h_total_uses_4_hour_interval(monkeypatch, tmp_path):
    """شمعة 20:00 اليوم السابق ⇒ رسالة 00:00 داخل اليوم بـ4h، وخارجه بـ1h."""
    from src.nw_4h_bot import INTERVAL_MS_4H, build_nw_4h_bot

    monkeypatch.setenv("TELEGRAM_MUTE", "1")
    monkeypatch.setenv("NW_STATE_FILE", str(tmp_path / "state4.json"))
    monkeypatch.setenv("NW_TRADE_LOG", str(tmp_path / "trades4.csv"))
    bot = build_nw_4h_bot()
    bot.tg = FakeTelegram()
    bar = ms(2026, 11, 7, 20, 0)                # +4h = 00:00 يوم التقرير
    with_sent_keys(bot, f"buy:S4HUSDT:{bar}:1")
    assert INTERVAL_MS_4H == 14_400_000

    assert bot.send_daily_report_if_due(ms(2026, 11, 9, 0, 10)) is None
    text4 = bot.tg.messages[-1]
    assert "📥 إجمالي التوصيات المرسلة لليوم: 1" in text4
    assert "(NW 4H)" in text4

    # نفس المفتاح مع طول شمعة 1H ⇒ 21:00 اليوم السابق ⇒ لا يُعدّ
    bot1h = FakeBot(tmp_path / "1h")
    with_sent_keys(bot1h, f"buy:S4HUSDT:{bar}:1")
    text1h = dr.send_daily_report_if_due(bot1h, ms(2026, 11, 9, 0, 10))
    assert "📥 إجمالي التوصيات المرسلة لليوم: 0" in text1h
