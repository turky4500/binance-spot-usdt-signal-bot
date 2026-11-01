"""اختبار دورة كاملة لبوت تتبّع الاتجاه اليومي (دخول → تحديث وقف → إغلاق) بعميل وهمي."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import daily_trend_bot as dtb  # noqa: E402

DAY = 86_400_000
H4 = 14_400_000
BASE = 1_700_000_000_000


def daily_df(closes, start=BASE):
    n = len(closes)
    return pd.DataFrame({
        "open_time": [start + i * DAY for i in range(n)],
        "open": closes,
        "high": [c * 1.005 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [10.0] * n,
        "close_time": [start + i * DAY + DAY - 1 for i in range(n)],
        "quote_asset_volume": [1_000_000.0 + i for i in range(n)],
    })


def h4_df(closes, start=BASE):
    n = len(closes)
    return pd.DataFrame({
        "open_time": [start + i * H4 for i in range(n)],
        "open": closes,
        "high": [c * 1.001 for c in closes],
        "low": [c * 0.999 for c in closes],
        "close": closes,
        "volume": [1.0] * n,
        "close_time": [start + i * H4 + H4 - 1 for i in range(n)],
    })


class FakeClient:
    def __init__(self, daily, gate_up=True, now=None):
        self.daily = daily
        self.gate_up = gate_up
        self.now = now or (BASE + 21 * DAY + 5 * 60_000)   # 5 دقائق بعد إغلاق آخر شمعة يومية

    def get_server_time(self):
        return self.now

    def get_klines(self, symbol, interval="1d", limit=120, **kw):
        if interval == "4h":
            start = self.now - 261 * H4            # آخر شمعة 4h تُغلق قبل "الآن" مباشرة
            return h4_df([100.0 + i if self.gate_up else 500.0 - i for i in range(260)], start=start)
        return self.daily[symbol]

    def get_all_prices(self):
        return {sym: float(df["close"].iloc[-1]) for sym, df in self.daily.items()}

    def get_price_ticks(self):
        return {}

    def get_spot_usdt_symbols(self):
        return sorted(self.daily)


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []

    def send_message(self, text):
        self.sent.append(text)


@pytest.fixture()
def bot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    b = dtb.DailyTrendBot.__new__(dtb.DailyTrendBot)          # بلا __init__ (لا شبكة)
    from src.daily_trend import DailyTrendSettings
    b.settings = DailyTrendSettings(lookback=5, atr_len=3, atr_mult=2.5, risk_pct=1.0,
                                    max_positions=2, min_stop_pct=0.5, max_stop_pct=15.0,
                                    capital=300.0)
    b.state = b._default_state()
    b.state["equity"] = 300.0
    b.state["peak_equity"] = 300.0
    b.telegram = FakeTelegram()
    b.ticks = {}
    b.halal = {}
    b.symbols = []
    return b


def _setup_market(bot, breakout=True, gate_up=True):
    # 20 شمعة ثابتة ثم شمعة اختراق في آخر شمعة مغلقة
    closes = [100.0 + (i % 3) * 0.5 for i in range(20)]
    closes.append(103.0 if breakout else 100.0)
    btc = daily_df([100.0 + (i % 2) for i in range(20)] + [101.0])
    bot.client = FakeClient({"ADAUSDT": daily_df(closes), "BTCUSDT": btc}, gate_up=gate_up)


def test_full_cycle_opens_position_and_sends_entry_message(bot):
    _setup_market(bot)
    bot.symbols = ["ADAUSDT"]
    bot.run_cycle()

    assert "ADAUSDT" in bot.state["positions"]
    pos = bot.state["positions"]["ADAUSDT"]
    assert pos["stop_price"] < pos["entry_price"]
    assert pos["notional"] > 0
    assert bot.state["last_entry_bar_close"] > 0
    entry_msgs = [m for m in bot.telegram.sent if "دخول جديد" in m]
    assert len(entry_msgs) == 1
    assert "الوقف" in entry_msgs[0] and "حجم المركز" in entry_msgs[0]


def test_no_entry_when_gate_is_closed(bot):
    _setup_market(bot, gate_up=False)
    bot.symbols = ["ADAUSDT"]
    bot.run_cycle()
    assert bot.state["positions"] == {}
    assert not any("دخول جديد" in m for m in bot.telegram.sent)


def test_same_daily_bar_never_opens_twice(bot):
    _setup_market(bot)
    bot.symbols = ["ADAUSDT"]
    bot.run_cycle()
    first = len(bot.state["positions"])
    bot.run_cycle()
    assert len(bot.state["positions"]) == first
    assert len([m for m in bot.telegram.sent if "دخول جديد" in m]) == 1


def test_stop_touch_closes_position_and_updates_equity(bot):
    _setup_market(bot)
    bot.symbols = ["ADAUSDT"]
    bot.run_cycle()
    pos = dict(bot.state["positions"]["ADAUSDT"])
    # السعر يهبط تحت الوقف
    bot.client.get_all_prices = lambda: {"ADAUSDT": pos["stop_price"] * 0.98}
    bot.run_cycle()
    assert bot.state["positions"] == {}
    exit_msgs = [m for m in bot.telegram.sent if "أغلق الصفقة الآن" in m]
    assert len(exit_msgs) == 1
    assert "لمس الوقف" in exit_msgs[0]
    assert bot.state["equity"] < 300.0
    assert bot.state["trades"] == 1


def test_gate_close_liquidates_positions(bot):
    _setup_market(bot)
    bot.symbols = ["ADAUSDT"]
    bot.run_cycle()
    assert bot.state["positions"]
    bot.client.gate_up = False                     # البوابة تُغلق
    bot.state["gate_open"] = True                  # كانت مفتوحة
    bot.run_cycle()
    assert bot.state["positions"] == {}
    assert any("إغلاق بوابة السوق" in m for m in bot.telegram.sent)


def test_trailing_stop_moves_up_but_never_down(bot):
    _setup_market(bot)
    bot.symbols = ["ADAUSDT"]
    bot.run_cycle()
    pos = bot.state["positions"]["ADAUSDT"]
    base_stop = pos["stop_price"]
    bot.client.get_all_prices = lambda: {"ADAUSDT": pos["entry_price"] * 1.12}
    bot.run_cycle()
    raised = bot.state["positions"]["ADAUSDT"]["stop_price"]
    assert raised >= base_stop
    bot.client.get_all_prices = lambda: {"ADAUSDT": pos["entry_price"] * 1.10}   # أعلى من الوقف، أقل من القمة
    bot.run_cycle()
    assert bot.state["positions"]["ADAUSDT"]["stop_price"] == pytest.approx(raised)


def test_state_and_trade_log_are_written(bot, tmp_path):
    _setup_market(bot)
    bot.symbols = ["ADAUSDT"]
    bot.run_cycle()
    assert (tmp_path / dtb.STATE_FILE).exists()
    state = json.loads((tmp_path / dtb.STATE_FILE).read_text(encoding="utf-8"))
    assert "ADAUSDT" in state["positions"]


def test_entry_window_expired_is_skipped(bot):
    _setup_market(bot)
    bot.symbols = ["ADAUSDT"]
    bot.client.now = BASE + 21 * DAY + 5 * 3_600_000       # بعد الإغلاق اليومي بخمس ساعات
    bot.run_cycle()
    assert bot.state["positions"] == {}
    assert bot.state["last_entry_bar_close"] > 0            # عُلمت الشمعة كمعالَجة


# ───────────────── رسائل رفع الوقف والتقرير اليومي ─────────────────
def test_stop_raise_message_is_sent_even_below_entry(bot):
    """المستخدم يريد رسالة عند *كل* رفع للوقف، وليس فقط عند التعادل."""
    _setup_market(bot)
    bot.symbols = ["ADAUSDT"]
    bot.run_cycle()
    pos = bot.state["positions"]["ADAUSDT"]
    entry = pos["entry_price"]
    # ارتفاع بسيط: الوقف يتحرك لكنه يبقى تحت الدخول
    bot.client.now += 31 * 60_000                # بعد فترة السماح (30 دقيقة)
    bot.client.get_all_prices = lambda: {"ADAUSDT": entry * 1.02}
    bot.run_cycle()
    raise_msgs = [m for m in bot.telegram.sent if "حدّث أمر الوقف" in m]
    assert len(raise_msgs) == 1
    new_stop = bot.state["positions"]["ADAUSDT"]["stop_price"]
    assert new_stop < entry                      # لا يزال تحت الدخول
    assert "من:" in raise_msgs[0] and "إلى:" in raise_msgs[0]


def test_each_new_stop_level_sends_exactly_one_message(bot):
    _setup_market(bot)
    bot.symbols = ["ADAUSDT"]
    bot.run_cycle()
    entry = bot.state["positions"]["ADAUSDT"]["entry_price"]
    for mult in (1.02, 1.06, 1.10):
        bot.client.now += 31 * 60_000              # كل مستوى بعد فترة سماح
        bot.client.get_all_prices = lambda m=mult: {"ADAUSDT": entry * m}
        bot.run_cycle()
    raise_msgs = [m for m in bot.telegram.sent if "حدّث أمر الوقف" in m]
    assert len(raise_msgs) >= 2                  # مستويات مختلفة ⇒ رسائل مختلفة
    # نفس السعر مرة أخرى ⇒ لا رسالة جديدة (منع التكرار)
    before = len([m for m in bot.telegram.sent if "حدّث أمر الوقف" in m])
    bot.client.now += 31 * 60_000
    bot.run_cycle()
    assert len([m for m in bot.telegram.sent if "حدّث أمر الوقف" in m]) == before


def test_daily_report_is_sent_once_per_day(bot):
    _setup_market(bot)
    bot.symbols = ["ADAUSDT"]
    bot.run_cycle()
    reports = [m for m in bot.telegram.sent if "التقرير اليومي" in m]
    assert len(reports) == 1
    assert "الرصيد" in reports[0] and "بوابة السوق" in reports[0]
    assert "ADAUSDT" in reports[0]               # يعرض المراكز المفتوحة
    assert "الوقف المطلوب" in reports[0]
    bot.run_cycle()                              # نفس اليوم ⇒ لا تكرار
    assert len([m for m in bot.telegram.sent if "التقرير اليومي" in m]) == 1


def test_daily_report_when_no_positions_and_gate_closed(bot):
    _setup_market(bot, gate_up=False)
    bot.symbols = ["ADAUSDT"]
    bot.run_cycle()
    reports = [m for m in bot.telegram.sent if "التقرير اليومي" in m]
    assert len(reports) == 1
    assert "لا يوجد" in reports[0]
    assert "مغلقة" in reports[0]


def test_stop_notice_is_throttled_to_one_per_window(bot):
    """حماية من الإغراق: رفع الوقف مرات متعددة في أقل من 30 دقيقة ⇒ رسالة واحدة."""
    _setup_market(bot)
    bot.symbols = ["ADAUSDT"]
    bot.run_cycle()
    entry = bot.state["positions"]["ADAUSDT"]["entry_price"]
    before = len([m for m in bot.telegram.sent if "حدّث أمر الوقف" in m])
    bot.client.get_all_prices = lambda: {"ADAUSDT": entry * 1.02}
    bot.run_cycle()                      # أول رفع (تحت التعادل) ⇒ رسالة
    bot.client.get_all_prices = lambda: {"ADAUSDT": entry * 1.04}
    bot.run_cycle()                      # رفع ثانٍ تحت التعادل وخلال دقائق ⇒ لا رسالة (فاصل 30 دقيقة)
    after = len([m for m in bot.telegram.sent if "حدّث أمر الوقف" in m])
    assert after - before == 1
    # الوقف الحقيقي تحدّث رغم عدم إرسال رسالة
    assert bot.state["positions"]["ADAUSDT"]["stop_price"] > 0
