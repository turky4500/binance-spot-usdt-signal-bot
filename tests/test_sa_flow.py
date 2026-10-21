"""اختبارات دورة صفقة نظام القاع (سعودي) الكاملة: دخول → تحديث وقف → إغلاق (وقف/مهلة).

تشمل ما يميّز النسخة السعودية عن الكريبتو:
  • المهلة والمهلة بعدّ الشموع (فهرس) — صحيحة عبر أيام تداول غير متصلة (عطلات).
  • إغلاق شمعة يومي عند 15:00 الرياض (is_bar_closed).
  • حكم شرعي افتراضيًا «لا يوجد حكم» (قائمة يدوية اختيارية).
  • إثبات العزل: لا يُنشأ state.json/trades_log.csv ولا ملفات نظام القاع (qaa).

تعمل بـ pytest مباشرة:  python -m pytest tests/ -q
"""
from __future__ import annotations

import csv
import logging
import sys
from datetime import timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DAY_MS = 86_400_000
BASE_MS = 1_700_000_000_000


def _date(ms: int) -> str:
    from src.saudi_data import riyadh_date_str
    return riyadh_date_str(ms)


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []

    def send_message(self, text: str) -> None:
        self.sent.append(text)


class FakeMarket:
    def __init__(self, daily: pd.DataFrame | None = None):
        self.daily = daily

    def get_daily(self, code, range="2y"):
        return self.daily if self.daily is not None else pd.DataFrame()

    def get_daily_for_symbols(self, codes, range="2y"):
        return {c: self.daily for c in codes} if self.daily is not None else {}


def make_bot(tmp_path: Path):
    from sa_bot import SaSignalBot
    from src.config import AppConfig
    from src.saudi_strategy import SaSettings
    from src.state import StateStore

    bot = SaSignalBot.__new__(SaSignalBot)
    bot.config = AppConfig(telegram_bot_token="t", telegram_chat_id="c")
    bot.telegram = FakeTelegram()
    bot.logger = logging.getLogger("test-sa-flow")
    bot.settings = SaSettings()
    bot.market = FakeMarket()
    bot.store = StateStore(str(tmp_path / "state_sa.json"))
    bot.state = bot.store.load()
    bot.data_dir = str(tmp_path)
    bot.halal_dir = str(tmp_path / "sa")
    bot.halal_verdicts = {}
    bot.codes = ["2222"]
    bot.company_names = {"2222": "شركة أرامكو"}
    bot.last_universe_refresh = 0.0
    bot.get_halal_verdict = lambda code: "ℹ️ لا يوجد حكم"
    return bot


def make_trade(code="2222", entry=100.0, pivot_stop=95.0, bar_open=BASE_MS - DAY_MS):
    dist = (entry - pivot_stop) / entry * 100.0
    return {
        "trade_id": f"{code}-{bar_open}",
        "code": code,
        "company": "شركة أرامكو",
        "entry_price": entry,
        "stop_price": pivot_stop,
        "pivot_stop": pivot_stop,
        "target_price": entry + 2.0 * (entry - pivot_stop),
        "stop_dist_pct": dist,
        "size_suggest_pct": min(1.0 / dist * 100.0, 100.0),
        "entry_time": bar_open + DAY_MS - 1,
        "entry_bar_open_time": bar_open,
        "peak_price": entry,
        "last_stop_alert_price": pivot_stop,
        "stop_updates_count": 0,
        "halal_verdict": "ℹ️ لا يوجد حكم",
    }


def valid_entry_df(n: int = 250, spacing_days: int = 1) -> pd.DataFrame:
    """n يومًا صاعدًا + قاع صارم — شروط الدخول كاملة على الشمعة الأخيرة.

    spacing_days>1 يجعل أيام التداول غير متصلة (عطلات) لاختبار العدّ بالفهرس.
    """
    closes = [100.0 + i for i in range(n)]
    opens = list(closes)
    opens[n - 1] = closes[n - 1] - 10.0          # شمعة أخيرة خضراء
    highs = [max(o, c) + 1.0 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 1.0 for o, c in zip(opens, closes)]
    lows[n - 2] = closes[n - 2] - 60.0            # قاع الشمعة القاعية (منخفض بعمق)
    open_times = [BASE_MS + i * spacing_days * DAY_MS for i in range(n)]
    return pd.DataFrame({
        "open_time": open_times,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "date": [_date(t) for t in open_times],
    })


def test_entry_message_details_and_zero_touch(tmp_path):
    bot = make_bot(tmp_path)
    trade = make_trade()
    trade["entry_time"] = BASE_MS + DAY_MS  # تاريخ صالح لعرض الوقت
    bot.send_entry_message(trade)
    text = bot.telegram.sent[-1]
    assert "نظام القاع (سهم يومي)" in text
    assert "الفريم: 1D (يومي)" in text
    assert "السهم: 2222" in text
    assert "سعر الدخول: 100.00 ريال" in text
    assert "وقف القاع (الأصلي): 95.00 ريال (-5.00%)" in text
    assert "الهدف المقترح" in text and "عرض فقط" in text
    assert "الحجم المقترح" in text
    assert "الحكم الشرعي" in text
    # العزل: دفتر السعودية منفصل وملفات الأنظمة الأخرى غير مُنشأة أبدًا
    assert (tmp_path / "trades_log_sa.csv").exists()
    assert not (tmp_path / "trades_log.csv").exists()
    assert not (tmp_path / "trades_log_qaa.csv").exists()
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "state_qaa.json").exists()


def test_stop_update_message_content(tmp_path):
    bot = make_bot(tmp_path)
    trade = make_trade()
    trade["entry_time"] = BASE_MS + DAY_MS
    bot.send_stop_update_message(trade, 95.0, 104.5, BASE_MS + DAY_MS, current_price=108.0)
    text = bot.telegram.sent[-1]
    assert "تحديث وقف متحرك" in text
    assert "الوقف السابق: 95.00 ريال" in text
    assert "الوقف الجديد: 104.50 ريال (+10.00%)" in text
    assert "السعر الآن: 108.00 ريال (+8.00% من الدخول • 3.35% فوق الوقف الجديد)" in text
    assert "الربح المضمون" in text and "+4.50%" in text
    assert "عدّل وقفك يدويًا" in text
    assert "الحكم الشرعي" in text

    # بلا سعر — السطر يُomit ولا تنكسر الرسالة
    bot.telegram.sent.clear()
    bot.send_stop_update_message(trade, 95.0, 104.5, BASE_MS + DAY_MS)
    assert "السعر الآن" not in bot.telegram.sent[-1]


def test_daily_stop_raise_gated_by_step(tmp_path):
    """القيم الصغيرة تُسجَّل صامتة؛ الرسائل فقط عند تجاوز عتبة 1% — على إغلاق يومي."""
    bot = make_bot(tmp_path)
    trade = make_trade()
    bot.state["open_trades"]["2222"] = trade
    day0 = _date(int(trade["entry_bar_open_time"]))
    # يوم تداول واحد: قمة 110 → رفع الوقف غدًا (لا حماية بنفس الشمعة)
    df1 = pd.DataFrame({
        "open_time": [BASE_MS],
        "date": [day0],
        "open": [100.0], "high": [110.0], "low": [99.0], "close": [105.0],
    })
    # اليوم التالي: قمة 120 (يرفع من 110×0.92=101.2 ≥ 95×1.01 → رسالة)
    next_day = _date(BASE_MS + 3 * DAY_MS)
    df2 = pd.DataFrame({
        "open_time": [BASE_MS + 3 * DAY_MS],
        "date": [next_day],
        "open": [105.0], "high": [120.0], "low": [104.0], "close": [112.0],
    })
    kmap = {"2222": pd.concat([df1, df2], ignore_index=True)}
    bot._process_one_trading_date(day0, kmap)
    # بعد يوم0: peak=110 لكن الوقف لم يُرفع (يُرفع من قمة اليوم التالي)
    assert bot.state["open_trades"]["2222"]["peak_price"] == 110.0
    bot._process_one_trading_date(next_day, kmap)

    alerts = [t for t in bot.telegram.sent if "تحديث وقف متحرك" in t]
    assert len(alerts) == 1
    live = bot.state["open_trades"]["2222"]
    assert abs(live["stop_price"] - 101.2) < 1e-6      # 110 × 0.92
    assert live["stop_updates_count"] == 1


def test_daily_stop_hit_closes_trade(tmp_path):
    bot = make_bot(tmp_path)
    trade = make_trade()
    bot.state["open_trades"]["2222"] = trade
    entry_open = int(trade["entry_bar_open_time"])
    # يوم التداول التالي: low يلامس الوقف المُرفع (101.2) → إغلاق عند الفتح إن فاقته
    day1_open = entry_open + 3 * DAY_MS
    df = pd.DataFrame({
        "open_time": [entry_open, day1_open],
        "date": [_date(entry_open), _date(day1_open)],
        "open": [100.0, 101.0],
        "high": [110.0, 103.0],
        "low": [99.0, 100.5],
        "close": [105.0, 101.5],
    })
    kmap = {"2222": df}
    bot._process_one_trading_date(_date(entry_open), kmap)  # peak=110 (رفع من قمة الغد)
    bot._process_one_trading_date(_date(day1_open), kmap)   # يرفع إلى 101.2 ثم low=100.5 ≤ 101.2 → إغلاق

    assert "2222" not in bot.state["open_trades"]
    close_msgs = [t for t in bot.telegram.sent if "انتهت الصفقة" in t]
    assert len(close_msgs) == 1
    text = close_msgs[0]
    assert "لمس الوقف المتحرك" in text       # الوقف رُفع فوق القاع (101.2 > 95)
    assert "سعر الخروج: 101.00 ريال" in text  # الفتح 101.0 < الوقف 101.2 → تنفيذ عند الفتح
    # الوقف المتحرك رفع الربح المضمون فوق الدخول → خروج رابح (+0.70% صافي بعد 0.15×2 عمولة)
    assert "رابحة" in text
    assert "+0.70%" in text
    assert bot.state["last_exit_bar_time"]["2222"] == day1_open


def test_timeout_exit_index_based(tmp_path):
    """المهلة بعدّ شموع تداول فعلية — حتى مع أيام غير متصلة (عطلات) لا تُحسب أيام تقويمية."""
    bot = make_bot(tmp_path)
    spacing = 3  # كل شمعة بعد 3 أيام تقويمية (عطلة نهاية أسبوع)
    df_entry = valid_entry_df(n=250, spacing_days=spacing)
    bot.codes = ["2222"]
    entry_open = int(df_entry.iloc[-1]["open_time"])
    bot._process_one_trading_date(_date(entry_open), {"2222": df_entry})  # يفتح الصفقة (peak=350)
    trade = bot.state["open_trades"]["2222"]
    entry_bar = int(trade["entry_bar_open_time"])
    assert entry_bar == entry_open

    # أضف 40 شمعة تداول بعد الدخول (120 يومًا تقويميًا) → العدّ بالفهرس يقرأ 40 لا 120
    from src.saudi_strategy import bars_between
    rows = [{
        "open_time": entry_open + k * spacing * DAY_MS,
        "date": _date(entry_open + k * spacing * DAY_MS),
        "open": 349.0, "high": 351.0, "low": 340.0, "close": 350.0,
    } for k in range(1, 41)]
    df_full = pd.concat([df_entry, pd.DataFrame(rows)], ignore_index=True)
    target_open = rows[-1]["open_time"]
    assert bars_between(df_full, entry_bar, target_open) == 40

    # إغلاق المهلة عبر محور المعالجة اليومي (low=340 فوق الوقف 322 → لا وقف، بل مهلة)
    bot._process_one_trading_date(_date(target_open), {"2222": df_full})

    assert "2222" not in bot.state["open_trades"]
    close_msgs = [t for t in bot.telegram.sent if "انتهت الصفقة" in t]
    assert close_msgs and "انتهت المهلة (40 شمعة)" in close_msgs[-1]


def test_cooldown_is_bar_index_not_calendar(tmp_path):
    """مهلة أقل بين الدخولات تُحسب بالشموع لا بالأيام التقويمية (الدليل على التكييف)."""
    from src.saudi_strategy import SaSettings, evaluate_entry

    s = SaSettings()
    df = valid_entry_df(n=250, spacing_days=3)   # شمعة كل 3 أيام (عطلات)
    n = len(df)
    last_open = int(df.iloc[-1]["open_time"])

    # آخر دخول قبل شمعتين فقط (6 أيام تقويمية) → محجوب رغم أن 6 أيام > 3 (لو بالزمن لكان مسموحًا)
    recent = int(df.iloc[n - 3]["open_time"])
    assert evaluate_entry(df, s, last_entry_bar_open_time=recent) is None

    # آخر دخول قبل 6 شموع (18 يومًا تقويميًا) → مسموح (6 > cooldown 3)
    older = int(df.iloc[n - 7]["open_time"])
    assert evaluate_entry(df, s, last_entry_bar_open_time=older) is not None


def test_entry_blocked_on_exit_day(tmp_path):
    bot = make_bot(tmp_path)
    bot.codes = ["2222"]
    df = valid_entry_df()
    last_closed = int(df.iloc[-1]["open_time"])
    bot.state["last_exit_bar_time"]["2222"] = last_closed  # خرج في هذا اليوم نفسه
    bot._process_one_trading_date(_date(last_closed), {"2222": df})

    assert "2222" not in bot.state["open_trades"]
    assert bot.telegram.sent == []
    assert not (tmp_path / "trades_log_sa.csv").exists()
    # العزل: ملفات الأنظمة الأخرى لا تُنشأ
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "trades_log.csv").exists()
    assert not (tmp_path / "state_qaa.json").exists()
    assert not (tmp_path / "trades_log_qaa.csv").exists()


def test_entry_flow_opens_trade(tmp_path):
    bot = make_bot(tmp_path)
    bot.codes = ["2222"]
    df = valid_entry_df()
    last_closed = int(df.iloc[-1]["open_time"])
    bot._process_one_trading_date(_date(last_closed), {"2222": df})

    live = bot.state["open_trades"]["2222"]
    assert live["entry_price"] == 349.0
    assert live["pivot_stop"] == 288.0
    assert live["target_price"] == 471.0
    assert bot.state["last_entry_bar_time"]["2222"] == last_closed
    assert any("إشارة دخول" in t for t in bot.telegram.sent)
    with (tmp_path / "trades_log_sa.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1 and rows[0]["code"] == "2222"


def test_exit_updates_journal_row_after_real_entry(tmp_path):
    """دخول حقيقي ثم إغلاق لاحقًا — يُحدَّث الصف بكل الحقول (peak_gain_pct وغيره)."""
    bot = make_bot(tmp_path)
    bot.codes = ["2222"]
    df = valid_entry_df()
    last_closed = int(df.iloc[-1]["open_time"])
    bot._process_one_trading_date(_date(last_closed), {"2222": df})
    trade = bot.state["open_trades"]["2222"]
    # شمعة الدخول: قمة 350 → أول رفع: 350 × 0.92 = 322
    future_open = last_closed + 3 * DAY_MS
    row = pd.DataFrame({
        "open_time": [future_open],
        "date": [_date(future_open)],
        "open": [340.0], "high": [345.0], "low": [320.0], "close": [330.0],
    })
    big = pd.concat([df, row], ignore_index=True)
    bot._process_one_trading_date(_date(future_open), {"2222": big})

    assert "2222" not in bot.state["open_trades"]
    with (tmp_path / "trades_log_sa.csv").open(newline="", encoding="utf-8") as f:
        r = next(csv.DictReader(f))
    assert r["exit_reason"] != "" and r["exit_price"] != ""
    assert r["peak_gain_pct"] != ""


def test_entry_flow_survives_telegram_failure(tmp_path):
    """فشل تيليجرام (HTTP400) لا يقتل الدورة: الصفقة تُحفظ في الحالة والدفتر."""
    class BoomTelegram:
        def send_message(self, text: str) -> None:
            raise RuntimeError("telegram 400")

    bot = make_bot(tmp_path)
    bot.telegram = BoomTelegram()
    bot.codes = ["2222"]
    df = valid_entry_df()
    last_closed = int(df.iloc[-1]["open_time"])
    bot._process_one_trading_date(_date(last_closed), {"2222": df})  # لا يُسقَط استثناء

    assert "2222" in bot.state["open_trades"]
    with (tmp_path / "trades_log_sa.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1 and rows[0]["code"] == "2222"


def test_halal_default_is_no_ruling():
    from src.saudi_halal import NO_RULING, ensure_verdict, load_verdicts
    assert load_verdicts("/nonexistent/path.json") == {}
    assert ensure_verdict("/nonexistent/dir", "2222", {}) == NO_RULING


def test_is_bar_closed_uses_riyadh_close():
    """شمعة اليوم لا تُعتبر مغلقة قبل 15:00 الرياض؛ والأيام السابقة مغلقة دائمًا."""
    from src.saudi_data import date_close_ms, is_bar_closed, riyadh_today_str
    from datetime import datetime
    # «اليوم» = تاريخ محدد: قبل الإغلاق → غير مغلق، بعده → مغلق
    today = "2026-10-21"
    close_ms = date_close_ms(today)
    assert not is_bar_closed(today, close_ms - 1)
    assert is_bar_closed(today, close_ms)
    # يوم ماضٍ مغلق دائمًا
    assert is_bar_closed("2026-10-20", close_ms - 1)
    # يوم مستقبلي غير مغلق
    assert not is_bar_closed("2026-10-22", close_ms + 10 * 3600 * 1000)


def test_backtest_entry_parity_with_evaluate_entry():
    """نسخة الـ backtest للدخول (entry_signal_at) تطابق evaluate_entry على كل الشموع.

    يضمن أن أرقام التحقق الورقي تُبنى على نفس منطق الإنتاج حرفيًا (حتى لا تختلف).
    """
    from backtest_sa import entry_signal_at
    from src.saudi_strategy import SaSettings, ema, evaluate_entry

    s = SaSettings()
    # سلسلة فيها إشارة دخول صحيحة عند آخر شمعة (مثل valid_entry_df) على تقويم غير متصل
    df = valid_entry_df(n=250, spacing_days=3)
    open_time_to_idx = {int(t): k for k, t in enumerate(df["open_time"].to_numpy(dtype="int64"))}
    ema_arr = ema(df["close"]).to_numpy(dtype="float64")

    last_entry_open = int(df.iloc[200]["open_time"])   # قديم بما يكفي (فوقي cooldown)
    last_exit_open = 0

    checked = 0
    for i in range(s.min_bars, len(df)):
        prefix = df.iloc[: i + 1]
        prod = evaluate_entry(
            prefix, s,
            last_entry_bar_open_time=last_entry_open,
            last_exit_bar_open_time=last_exit_open,
        )
        mine = entry_signal_at(
            df, i, s, float(ema_arr[i]), open_time_to_idx,
            last_entry_open=last_entry_open, last_exit_open=last_exit_open,
        )
        assert (prod is None) == (mine is None), f"parity mismatch at bar {i}: prod={prod} mine={mine}"
        if prod is not None:
            assert mine is not None
            assert abs(prod["entry_price"] - mine["entry_price"]) < 1e-9
            assert abs(prod["stop_price"] - mine["stop_price"]) < 1e-9
            assert abs(prod["peak_price"] - mine["peak_price"]) < 1e-9
            checked += 1
    assert checked >= 1, "يجب أن توجد إشارة دخول واحدة على الأقل للتحقق من التطابق"

    # اختبار ممنوع إعادة الدخول في يوم الخروج: يوم الخروج = شمعة الإشارة يجب أن يُسقطها
    exit_open = int(df.iloc[-1]["open_time"])
    blocked = entry_signal_at(
        df, len(df) - 1, s, float(ema_arr[-1]), open_time_to_idx,
        last_entry_open=last_entry_open, last_exit_open=exit_open,
    )
    assert blocked is None
