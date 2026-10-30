"""اختبارات فلتر EMA200 وتجميع الرسائل في ZigZag++ (2026-10-30).

الخلفية (من بيانات التشغيل الحقيقية): في 2026-10-30 أرسل النظام 246 رسالة دخول
في ثانية واحدة، ثم 141 رسالة خسارة في ثانية واحدة — لأن قاعدة الدخول شرط سوقي
عام وكل الأزواج كانت تحت المتوسط. العلاج المُختبر هنا:

   1) فلتر EMA200: لا إشارة إلا فوق المتوسط (وإلا رفض مسجّل).
   2) تجميع الرسائل: رسالة واحدة لكل نوع في الدورة، بلا تأخير زمني
      (تُرسل في نفس الدورة التي حدث فيها اللمس/الإغلاق).

python -m pytest tests/ -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.zigzag import HOUR_MS, ZigZagSettings, ema_at_bar, ema_series  # noqa: E402
from test_zigzag_flow import make_bot  # noqa: E402

BASE_MS = 1_699_999_200_000


def trend_df(direction: str = "up", n: int = 220) -> pd.DataFrame:
    """إطار سعري باتجاه واحد ثم قاع عميق وشمعة دخول خضراء في آخر 3 شموع.

    الاتجاه يحدّد علاقة الإغلاق بـ EMA200:
        "up"   ⇒ الإغلاق أعلى من المتوسط (الإشارة يجب أن تُقبل)
        "down" ⇒ الإغلاق أدنى من المتوسط (الإشارة يجب أن تُرفض)
    """
    step = 0.25 if direction == "up" else -0.25
    closes = [100.0 + step * i for i in range(n)]
    rows = []
    for i, close in enumerate(closes):
        rows.append({"open": close - step * 0.4, "high": close + 0.3, "low": close - 0.3, "close": close})

    # القاع: أدنى قاع في التقييم (لن يسقط بسبب التشابه)
    dip_close = closes[n - 3]
    rows[n - 3] = {"open": dip_close, "high": dip_close + 0.2, "low": dip_close * 0.90, "close": dip_close}
    # شمعة الدخول: خضراء +2% (غير دوجي) وإغلاقها فوق قاع الإشارة
    entry_open = dip_close
    entry_close = dip_close * 1.02
    rows[n - 2] = {"open": entry_open, "high": entry_close * 1.005, "low": entry_open * 0.999, "close": entry_close}
    # شمعة أخيرة بعد الدخول (لا تُغيّر شيئًا)
    rows[n - 1] = {"open": entry_close, "high": entry_close + 0.5, "low": entry_close - 0.2, "close": entry_close + 0.2}

    return pd.DataFrame({
        "open_time": [BASE_MS + i * HOUR_MS for i in range(n)],
        "close_time": [BASE_MS + (i + 1) * HOUR_MS - 1 for i in range(n)],
        "open": [r["open"] for r in rows],
        "high": [r["high"] for r in rows],
        "low": [r["low"] for r in rows],
        "close": [r["close"] for r in rows],
    })


def server_time_for(df: pd.DataFrame) -> int:
    return int(df["close_time"].iloc[-1]) + 1


def short_df(n: int = 60) -> pd.DataFrame:
    """تاريخ أقصر من EMA200 ⇒ لا يمكن حساب المتوسط ⇒ تُرفض الإشارة احتياطًا."""
    rows = [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0} for _ in range(n)]
    rows[n - 3] = {"open": 100.0, "high": 101.0, "low": 90.0, "close": 100.0}
    rows[n - 2] = {"open": 100.0, "high": 103.0, "low": 99.0, "close": 102.0}
    rows[n - 1] = {"open": 102.0, "high": 103.0, "low": 101.0, "close": 102.5}
    return pd.DataFrame({
        "open_time": [BASE_MS + i * HOUR_MS for i in range(n)],
        "close_time": [BASE_MS + (i + 1) * HOUR_MS - 1 for i in range(n)],
        "open": [r["open"] for r in rows],
        "high": [r["high"] for r in rows],
        "low": [r["low"] for r in rows],
        "close": [r["close"] for r in rows],
    })


# ─────────────────────────────────────────── فلتر EMA200
def test_ema_accepts_entry_above_average(tmp_path):
    bot = make_bot(tmp_path)
    bot.settings.ema_filter = 1  # الفلتر مفعّل كما في الإنتاج
    bot.symbols = ["AAAUSDT"]
    df = trend_df("up")
    bot.process_new_bottoms({"AAAUSDT": df}, server_time_for(df))

    assert "AAAUSDT" in bot.state["open_signals"]
    signal = bot.state["open_signals"]["AAAUSDT"]
    assert signal["ema_value"] is not None
    assert signal["entry_price"] > signal["ema_value"]
    assert len(bot.telegram.sent) == 1
    assert "فوق EMA200" in bot.telegram.sent[0]


def test_ema_rejects_entry_below_average(tmp_path):
    bot = make_bot(tmp_path)
    bot.settings.ema_filter = 1
    bot.symbols = ["BBBUSDT"]
    df = trend_df("down")
    bot.process_new_bottoms({"BBBUSDT": df}, server_time_for(df))

    assert bot.state["open_signals"] == {}
    assert bot.telegram.sent == []
    # يُسجَّل الرفض للشفافية في التقرير اليومي
    assert (bot.state["rejection_stats"]["counts"] or {}).get("below_ema", 0) >= 1


def test_ema_rejects_when_history_shorter_than_average(tmp_path):
    bot = make_bot(tmp_path)
    bot.settings.ema_filter = 1
    bot.symbols = ["CCCUSDT"]
    df = short_df(60)
    bot.process_new_bottoms({"CCCUSDT": df}, server_time_for(df))

    assert bot.state["open_signals"] == {}
    assert bot.telegram.sent == []
    assert (bot.state["rejection_stats"]["counts"] or {}).get("ema_unknown", 0) == 1


def test_ema_filter_can_be_disabled(tmp_path):
    bot = make_bot(tmp_path)
    bot.settings.ema_filter = 0
    bot.symbols = ["DDDUSDT"]
    df = trend_df("down")
    bot.process_new_bottoms({"DDDUSDT": df}, server_time_for(df))
    assert "DDDUSDT" in bot.state["open_signals"]


def test_ema_at_bar_has_no_lookahead():
    """قيمة EMA عند شمعة ماضية لا تتغيّر بتغيّر الشموع اللاحقة."""
    df = trend_df("up")
    bar = int(df["open_time"].iloc[-2])
    before = ema_at_bar(df, 200, bar)

    changed = df.copy()
    changed.loc[changed.index[-1], "close"] = float(changed["close"].iloc[-1]) * 3
    assert ema_at_bar(changed, 200, bar) == pytest.approx(before)


def test_ema_at_bar_returns_none_when_insufficient_history():
    df = trend_df("up", n=50)
    assert ema_at_bar(df, 200, int(df["open_time"].iloc[-2])) is None


def test_ema_series_matches_pandas_ewm():
    closes = pd.Series([float(i) for i in range(1, 301)])
    assert ema_series(closes, 200).iloc[-1] == pytest.approx(
        closes.ewm(span=200, adjust=False).mean().iloc[-1]
    )


def test_default_settings_enable_ema_filter():
    settings = ZigZagSettings()
    assert settings.ema_enabled is True
    assert settings.ema_len == 200
    assert "فوق EMA200" in settings.describe()


# ─────────────────────────────────────────── تجميع الرسائل
def test_three_entries_arrive_as_one_message(tmp_path):
    bot = make_bot(tmp_path)
    bot.symbols = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    df = trend_df("up", n=40)  # الفلتر معطّل داخل make_bot
    bot.process_new_bottoms({s: df for s in bot.symbols}, server_time_for(df))

    assert len(bot.state["open_signals"]) == 3
    assert len(bot.telegram.sent) == 1, "ثلاث إشارات يجب أن تصل في رسالة واحدة"
    text = bot.telegram.sent[0]
    assert "إشارات دخول (3)" in text
    for symbol in bot.symbols:
        assert symbol in text


def test_single_entry_keeps_detailed_message(tmp_path):
    bot = make_bot(tmp_path)
    bot.symbols = ["AAAUSDT"]
    df = trend_df("up", n=40)
    bot.process_new_bottoms({"AAAUSDT": df}, server_time_for(df))

    assert len(bot.telegram.sent) == 1
    text = bot.telegram.sent[0]
    assert text.startswith("📥 إشارة دخول — AAAUSDT")
    assert "الدخول:" in text and "الهدف:" in text and "الحكم الشرعي:" in text


def test_batch_splits_when_over_item_limit(tmp_path, monkeypatch):
    import zigzag_bot

    monkeypatch.setattr(zigzag_bot, "MSG_BATCH_MAX_ITEMS", 2)
    bot = make_bot(tmp_path)
    bot.symbols = [f"SYM{i}USDT" for i in range(5)]
    df = trend_df("up", n=40)
    bot.process_new_bottoms({s: df for s in bot.symbols}, server_time_for(df))

    assert len(bot.telegram.sent) == 3  # 2 + 2 + 1
    assert "رسالة 1/3" in bot.telegram.sent[0]
    assert "رسالة 3/3" in bot.telegram.sent[-1]


def test_exits_are_batched_in_one_message(tmp_path):
    from test_zigzag_flow import make_1m, make_signal

    bot = make_bot(tmp_path)
    for symbol in ("AAAUSDT", "BBBUSDT", "CCCUSDT"):
        bot.state["open_signals"][symbol] = make_signal(symbol=symbol)
    bot.sync_open_signals_to_journal()

    from test_zigzag_flow import FakeBinance

    bot.binance = FakeBinance(klines_1m=make_1m([
        (100.0, 102.5, 99.0, 101.0, BASE_MS + 17 * HOUR_MS + 60_000 - 1),
    ]))
    bot.monitor_open_signals(BASE_MS + 17 * HOUR_MS + 2 * 60_000, {})

    assert bot.state["open_signals"] == {}
    assert len(bot.telegram.sent) == 1
    text = bot.telegram.sent[0]
    assert "أهداف تحققت (3)" in text
    for symbol in ("AAAUSDT", "BBBUSDT", "CCCUSDT"):
        assert symbol in text


def test_flush_is_idempotent(tmp_path):
    bot = make_bot(tmp_path)
    bot.symbols = ["AAAUSDT"]
    df = trend_df("up", n=40)
    bot.process_new_bottoms({"AAAUSDT": df}, server_time_for(df))
    sent_before = len(bot.telegram.sent)
    bot.flush_messages()
    bot.flush_messages()
    assert len(bot.telegram.sent) == sent_before


# ─────────────────────────────────────────── قائمة الاستبعاد
class FakeUniverseBinance:
    def get_spot_usdt_symbols(self, quote_asset="USDT"):
        return ["BTCUSDT", "SPYBUSDT", "NVDABUSDT", "SOLUSDT", "PAXGUSDT"]

    def get_price_ticks(self, quote_asset="USDT"):
        return {}


def test_exclude_file_removes_tokenized_stocks(tmp_path, monkeypatch):
    import zigzag_bot

    exclude_file = tmp_path / "zz_exclude.txt"
    exclude_file.write_text("# أسهم مرمّزة\nSPYBUSDT\nNVDABUSDT\nPAXGUSDT\n", encoding="utf-8")
    monkeypatch.setattr(zigzag_bot, "ZZ_EXCLUDE_FILE", str(exclude_file))

    bot = make_bot(tmp_path)
    bot.binance = FakeUniverseBinance()
    bot.refresh_symbols(force=True)

    assert bot.symbols == ["BTCUSDT", "SOLUSDT"]


def test_missing_exclude_file_is_harmless(tmp_path, monkeypatch):
    import zigzag_bot

    monkeypatch.setattr(zigzag_bot, "ZZ_EXCLUDE_FILE", str(tmp_path / "nope.txt"))
    bot = make_bot(tmp_path)
    bot.binance = FakeUniverseBinance()
    bot.refresh_symbols(force=True)
    assert "SPYBUSDT" in bot.symbols


def test_isolation_still_holds(tmp_path):
    """التعديل الجديد لا ينشئ أي ملف يخص أنظمة أخرى."""
    bot = make_bot(tmp_path)
    bot.settings.ema_filter = 1
    bot.symbols = ["AAAUSDT"]
    df = trend_df("up")
    bot.process_new_bottoms({"AAAUSDT": df}, server_time_for(df))

    assert (tmp_path / "state_zigzag.json").exists()
    for other in ("state.json", "trades_log.csv", "state_qaa.json", "state_sa.json", "trades_log_sa.csv"):
        assert not (tmp_path / other).exists()
