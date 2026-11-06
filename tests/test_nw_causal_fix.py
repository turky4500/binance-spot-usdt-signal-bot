""" regression لحالة TSTUSDT 2026-11-06: توصية 10 صباحًا بلا ▲ حقيقية.

قاعدة المستخدم بالحرف: «إذا أُغلقت الشمعة وظهرت عليها إشارة دخول، انتظر
الشمعة التالية؛ إن أُغلقت خضراء أرسل التوصية» ⇒ والحمراء تُلغي الإشارة نهائيًا
وننتظر ▲ جديدة. الملف tests/fixtures/tst_1h_2026-11-06.json هو شموع Binance
الفعلية (500 مغلقة حتى 10:00 صباحًا + شمعة 11:00 غير مغلقة).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from nw_bot import NWBot, build_nw_bot
from src.nw_signals import NWSignalConfig, candles_closed, evaluate_symbol
from src.nw_state import NWStateStore

FIXTURE = Path(__file__).parent / "fixtures" / "tst_1h_2026-11-06.json"
NOW_MS = 1_793_953_144_583          # 2026-11-06 11:19 صباحًا بتوقيت الرياض
RIYADH = ZoneInfo("Asia/Riyadh")
SIGNAL_09 = 1_793_944_800_000       # شمعة 09:00 التي ادّعى الكود أن عليها ▲
CONFIRM_10 = 1_793_948_400_000      # شمعة 10:00 (شمعة الدخول)


def frame(red_confirm: bool = False) -> pd.DataFrame:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    df = pd.DataFrame([{
        "open_time": r[0], "open": r[1], "high": max(r[1], r[2]),
        "low": min(r[1], r[2]), "close": r[2], "close_time": r[3],
    } for r in rows])
    if red_confirm:
        i = df.index[df["open_time"] == CONFIRM_10][0]
        df.loc[i, "open"] = df.loc[i, "close"] + 0.00010   # شمعة التأكيد حمراء
    return df


class FakeClient:
    def __init__(self, df, now_ms=NOW_MS):
        self.df, self.now_ms = df, now_ms

    def get_server_time(self):
        return self.now_ms

    def get_klines_for_symbols(self, symbols, interval, limit):
        return {s: self.df for s in symbols}


class FakeTelegram:
    def __init__(self):
        self.messages: list[str] = []

    def send_message(self, text):
        self.messages.append(text)


def make_bot(tmp_path, cfg, df=None):
    tg = FakeTelegram()
    store = NWStateStore(str(tmp_path / "state.json"))
    bot = NWBot(
        client=FakeClient(df if df is not None else frame()),
        telegram=tg,
        state_store=store,
        cfg=cfg,
        state_path=str(tmp_path / "state.json"),
        trade_log=str(tmp_path / "trades.csv"),
        show_repaint_warning=False,
    )
    return bot, tg, store


RAW = lambda: NWSignalConfig(boundary_correction=False)      # مطابقة Pine الحرفية (التي فيها الخلل)
# إعداد النظام الحيّ: تصحيح الحافة + عتبة الضجيج
FIXED = lambda: NWSignalConfig(boundary_correction=True, min_trough_pct=0.0001)


def test_tst_phantom_label_is_reproduced_then_blocked():
    df = frame()
    o, c, t = candles_closed(df, NOW_MS)
    assert len(c) == 500
    assert datetime.fromtimestamp(t[-1] / 1000, RIYADH).strftime("%I:%M %p") == "10:00 AM"

    bad = evaluate_symbol(o, c, t, RAW())
    assert len(bad["buy"]) == 1                        # ▲ وهمية على 09:00
    assert bad["buy"][0]["bar_time"] == SIGNAL_09
    assert bad["buy"][0]["confirm_bar_time"] == CONFIRM_10
    assert bad["buy"][0]["confirm_green"] is True

    good = evaluate_symbol(o, c, t, FIXED())
    assert good["buy"] == []                          # لا ▲ حقيقية ⇒ لا توصية
    assert good["cancelled_buy"] == []


def test_noise_floor_rejects_shallow_troughs():
    """كل ▲ تحت عتبة العمق تُهمَل ولا تُرسل (حتى مع مطابقة Pine الحرفية)."""
    df = frame()
    o, c, t = candles_closed(df, NOW_MS)
    r = evaluate_symbol(o, c, t, NWSignalConfig(min_trough_pct=1.0))
    assert r["buy"] == [] and r["ignored_shallow"] == 1


def test_production_config_does_not_send_the_wrong_tst_message(tmp_path):
    bot, tg, store = make_bot(tmp_path, FIXED())
    s = bot.run_once(universe=["TSTUSDT"])
    assert s["entry"] == 0 and s["repeat"] == 0
    assert tg.messages == []
    assert store.load()["symbols"]["TSTUSDT"]["position"] is None


def test_green_confirmation_sends_with_entry_candle_time(tmp_path):
    """الشمعة التالية خضراء ⇒ تُرسل، والوقت = شمعة الدخول (قرار المستخدم)."""
    bot, tg, _ = make_bot(tmp_path, RAW())
    s = bot.run_once(universe=["TSTUSDT"])
    assert s["entry"] == 1 and len(tg.messages) == 1
    m = tg.messages[0]
    assert "📥 توصية دخول — TSTUSDT" in m
    assert "💰 سعر التوصية: 0.01204" in m       # إغلاق شمعة 10:00
    assert "🕒 2026-11-06 10:00 AM" in m         # وقت شمعة الدخول، 12 ساعة
    assert "🕐 فريم الساعة (1H) · Nadaraya" in m
    assert "\n\n" in m


def test_red_confirmation_cancels_the_signal_permanently(tmp_path):
    """الحمراء تُلغي ▲ نهائيًا: حتى لو عادت التسمية بشمعة خضراء لا تُرسل."""
    bot, tg, store = make_bot(tmp_path, RAW(), frame(red_confirm=True))
    s1 = bot.run_once(universe=["TSTUSDT"])
    assert s1["entry"] == 0 and s1["cancelled"] >= 1
    assert tg.messages == []
    assert SIGNAL_09 in store.load()["cancelled_buy_bars"]["TSTUSDT"]

    # دورة بلا تسميات تُمسح فيها prev_buy_bars (محاكاة لإعادة الرسم)
    flat = frame()
    flat["close"] = 0.01204
    flat["open"] = 0.01204
    bot.client.df = flat
    bot.run_once(universe=["TSTUSDT"])
    assert tg.messages == []

    # تعود نفس ▲ على نفس الشمعة B، لكن بشمعة تأكيد خضراء ⇒ يجب أن تبقى مُلغاة
    bot.client.df = frame()
    s3 = bot.run_once(universe=["TSTUSDT"])
    assert s3["entry"] == 0
    assert tg.messages == []                       # لا توصية خاطئة
    assert SIGNAL_09 in store.load()["cancelled_buy_bars"]["TSTUSDT"]


def test_build_nw_bot_enables_the_fix_by_default(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MUTE", "1")
    monkeypatch.delenv("NW_BOUNDARY_CORRECT", raising=False)
    bot = build_nw_bot()
    assert bot.cfg.boundary_correction is True
    assert bot.cfg.min_trough_pct == pytest.approx(0.0001)
    monkeypatch.setenv("NW_BOUNDARY_CORRECT", "0")
    assert build_nw_bot().cfg.boundary_correction is False


def test_state_has_the_cancellation_log(tmp_path):
    assert NWStateStore(str(tmp_path / "s.json")).load()["cancelled_buy_bars"] == {}
