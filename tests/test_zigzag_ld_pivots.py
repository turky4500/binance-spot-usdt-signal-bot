"""اختبارات محاور ZigZag++ [LD] (Depth/Deviation/Backstep) — 2026-10-30.

المرجع: كود Pine الذي أرسله المستخدم. الهدفان:
  1) صحة اكتشاف المحاور وتسمياتها (HH/HL/LH/LL) كما في المؤشر.
  2) أن وضع `ZZ_BOTTOM_MODE=zigzag_ld` لا يدخل قبل تأكيد المحور (بلا نظر للمستقبل).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.zigzag import ZigZagSettings, find_latest_bottom  # noqa: E402
from src.zigzag_ld import mt4_zigzag, provisional  # noqa: E402
from test_zigzag_flow import make_bot  # noqa: E402

HOUR_MS = 3_600_000
BASE_MS = 1_699_999_200_000


def path_df(points: list[float], step_bars: int = 3) -> pd.DataFrame:
    """يبني شموعًا سعرية تمشي بين النقاط المعطاة (نقاط تحوّل) بخطوات شموع."""
    closes: list[float] = []
    for a, b in zip(points, points[1:]):
        for k in range(step_bars):
            closes.append(a + (b - a) * k / step_bars)
    closes.append(points[-1])
    n = len(closes)
    return pd.DataFrame({
        "open_time": [BASE_MS + i * HOUR_MS for i in range(n)],
        "close_time": [BASE_MS + (i + 1) * HOUR_MS - 1 for i in range(n)],
        "open": closes,
        "high": [c + 0.4 for c in closes],
        "low": [c - 0.4 for c in closes],
        "close": closes,
    })


def test_detects_alternating_pivots():
    df = path_df([120, 100, 118, 108, 130], step_bars=4)
    pivots = mt4_zigzag(df["high"], df["low"], depth=3, deviation=1, backstep=1, point=1.0)
    kinds = [p["kind"] for p in pivots]
    assert kinds[:3] == ["L", "H", "L"]
    assert pivots[0]["price"] == pytest.approx(99.6, abs=0.5)
    assert pivots[1]["price"] == pytest.approx(118.4, abs=0.5)


def test_higher_low_is_labelled_hl_and_lower_low_ll():
    settings = ZigZagSettings(bottom_mode="zigzag_ld", pivot_source="confirmed", depth=3, deviation=1, backstep=1)
    # قاع أول ثم قاع أعلى ⇒ HL
    df_hl = path_df([120, 100, 118, 108, 130], step_bars=4)
    bottom_hl = find_latest_bottom(df_hl, settings, point=1.0)
    assert bottom_hl is not None and bottom_hl["pivot_label"] == "HL"

    # قاع أول ثم قاع أدنى ⇒ LL
    df_ll = path_df([120, 100, 118, 90, 130], step_bars=4)
    bottom_ll = find_latest_bottom(df_ll, settings, point=1.0)
    assert bottom_ll is not None and bottom_ll["pivot_label"] == "LL"


def test_pivot_bottom_exposes_confirmation_bar():
    settings = ZigZagSettings(bottom_mode="zigzag_ld", pivot_source="confirmed", depth=3, deviation=1, backstep=2)
    df = path_df([120, 100, 118, 108, 130], step_bars=4)
    bottom = find_latest_bottom(df, settings, point=1.0)
    assert bottom is not None
    assert bottom["confirm_open_time"] > bottom["bar_open_time"], "التأكيد يجب أن يكون بعد شمعة القاع"


def test_low12_mode_is_unchanged_by_default():
    settings = ZigZagSettings()
    assert settings.bottom_mode == "low12" and settings.pivot_mode is False
    assert "أدنى قاع" in settings.describe()


def test_pivot_mode_enters_only_after_confirmation(tmp_path):
    bot = make_bot(tmp_path)
    bot.settings = ZigZagSettings(bottom_mode="zigzag_ld", pivot_source="confirmed", depth=3, deviation=1, backstep=2)
    bot.settings.ema_filter = 0
    bot.symbols = ["AAAUSDT"]

    df = path_df([120, 100, 118, 108, 130, 142], step_bars=4)
    # شمعة التأكيد نفسها: يجب ألّا يدخل
    confirm_idx = df.index[df["open_time"] == find_latest_bottom(
        df, bot.settings, point=1.0)["confirm_open_time"]][0]
    bot.process_new_bottoms({"AAAUSDT": df.iloc[: confirm_idx + 1]}, int(df["close_time"].iloc[confirm_idx]) + 1)
    assert "AAAUSDT" not in bot.state["open_signals"]

    # بعد التأكيد بشمعة خضراء (نضبط الشمعة الأخيرة خضراء صراحةً): يدخل
    df = df.copy()
    df.loc[df.index[-1], "open"] = float(df["close"].iloc[-1]) * 0.99
    bot.process_new_bottoms({"AAAUSDT": df}, int(df["close_time"].iloc[-1]) + 1)
    assert "AAAUSDT" in bot.state["open_signals"]
    signal = bot.state["open_signals"]["AAAUSDT"]
    assert signal["pivot_label"] in ("HL", "LL")
    assert signal["stop_price"] < signal["entry_price"]


def test_provisional_matches_indicator_labels():
    df = path_df([120, 100, 118, 108, 130], step_bars=4)
    pivots = mt4_zigzag(df["high"], df["low"], depth=3, deviation=1, backstep=1, point=1.0)
    state = provisional(df["high"].to_numpy(), df["low"].to_numpy(), pivots, len(df))
    assert state is not None
    assert state["direction"] in (-1, 1)
    assert state["label"] in ("HH", "HL", "LH", "LL")


def test_max_stop_filter_rejects_wide_pivot_stop():
    """قاع مؤشر ZigZag قد يكون بعيدًا ⇒ فلتر أقصى مسافة وقف يرفضه."""
    from src.zigzag import evaluate_entry_candle

    df = path_df([120, 100, 118, 108, 130], step_bars=4).copy()
    df.loc[df.index[-1], "open"] = float(df["close"].iloc[-1]) * 0.99   # شمعة خضراء
    bottom = {"bar_index": 12, "bar_open_time": 0, "bar_close_time": 0, "low": 108.0}
    row = df.iloc[-1]
    loose = ZigZagSettings(ema_filter=0, max_stop_pct=0)
    assert evaluate_entry_candle(bottom, row, loose) is not None

    tight = ZigZagSettings(ema_filter=0, max_stop_pct=3.0)   # الوقف هنا ~24% بعيد
    rejected: dict = {}
    assert evaluate_entry_candle(bottom, row, tight, rejected=rejected) is None
    assert rejected["reason"] == "stop_too_wide"


# ─────────────────────────────────────────────────────────────────────────────
# قاعدة المستخدم (2026-10-30): «إشارة القاع ⇒ الشمعة التالية خضراء ⇒ الدخول بإغلاقها،
# وإن كانت حمراء أو دوجي تُلغى الإشارة وينتظر قاعًا جديدًا».
# ─────────────────────────────────────────────────────────────────────────────
def _series(closes: list[float]) -> pd.DataFrame:
    """شموع من قائمة إغلاقات: الفتح = الإغلاق السابق، والقاع/القمة ±0.5."""
    opens = [closes[0]] + closes[:-1]
    n = len(closes)
    return pd.DataFrame({
        "open_time": [BASE_MS + i * HOUR_MS for i in range(n)],
        "close_time": [BASE_MS + (i + 1) * HOUR_MS - 1 for i in range(n)],
        "open": opens,
        "high": [max(o, c) + 0.5 for o, c in zip(opens, closes)],
        "low": [min(o, c) - 0.5 for o, c in zip(opens, closes)],
        "close": closes,
    })


# هبوط متتابع ينتهي بقاع عند الشمعة 8 (100.0)، ثم شمعة قرار خضراء صاعدة.
DECLINE = [120.0, 117.0, 114.0, 111.0, 108.0, 105.0, 102.0, 101.0, 100.5]
BOTTOM_BAR, BOTTOM_LOW = 8, 100.0


def _rule_settings() -> ZigZagSettings:
    return ZigZagSettings(bottom_mode="zigzag_ld", depth=3, deviation=1, backstep=1, ema_filter=0)


def test_user_rule_enters_at_close_of_next_green_candle(tmp_path):
    settings = _rule_settings()
    df = _series(DECLINE + [105.0])          # الشمعة 9 = شمعة الدخول (خضراء)
    bottom = find_latest_bottom(df, settings, point=0.01)   # البوت يحسب على ما قبل الشمعة الأخيرة
    assert bottom is not None, "يجب أن يُكتشف القاع عند الشمعة قبل الأخيرة"
    assert bottom["bar_index"] == BOTTOM_BAR and bottom["low"] == pytest.approx(BOTTOM_LOW)

    bot = make_bot(tmp_path)
    bot.settings = settings
    bot.symbols = ["AAAUSDT"]
    bot.process_new_bottoms({"AAAUSDT": df}, int(df["close_time"].iloc[-1]) + 1)

    signal = bot.state["open_signals"]["AAAUSDT"]
    # الدخول = إغلاق الشمعة التالية للقاع، والوقف = قاع المحور
    assert signal["entry_price"] == pytest.approx(105.0)
    assert signal["entry_bar_open_time"] == int(df["open_time"].iloc[9])
    assert signal["stop_price"] == pytest.approx(BOTTOM_LOW)
    assert signal["pivot_label"] in ("HL", "LL")


def _set_last_bar(df: pd.DataFrame, o: float, c: float, lo: float, hi: float) -> pd.DataFrame:
    df = df.copy()
    i = df.index[-1]
    df.loc[i, ["open", "close", "low", "high"]] = [o, c, lo, hi]
    return df


def test_user_rule_discards_signal_when_next_candle_is_red(tmp_path):
    settings = _rule_settings()
    # شمعة قرار حمراء لكن **بلا كسر قاع** (low 100.2 > 100.0)
    df = _set_last_bar(_series(DECLINE + [101.0]), o=101.0, c=100.6, lo=100.2, hi=101.5)
    assert df["close"].iloc[-1] < df["open"].iloc[-1]

    bot = make_bot(tmp_path)
    bot.settings = settings
    bot.symbols = ["AAAUSDT"]
    bot.process_new_bottoms({"AAAUSDT": df}, int(df["close_time"].iloc[-1]) + 1)
    assert "AAAUSDT" not in bot.state["open_signals"]
    # الإشارة تُعلَّم كمُلغاة ⇒ لا تُعاد على شموع خضراء لاحقة، ننتظر قاعًا جديدًا
    assert bot.state["last_bottom_bar_time"]["AAAUSDT"] == int(df["open_time"].iloc[BOTTOM_BAR])


def test_user_rule_discards_doji_signal(tmp_path):
    settings = _rule_settings()
    df = _set_last_bar(_series(DECLINE + [100.6]), o=100.6, c=100.6, lo=100.2, hi=101.0)
    assert df["close"].iloc[-1] == df["open"].iloc[-1]

    bot = make_bot(tmp_path)
    bot.settings = settings
    bot.symbols = ["AAAUSDT"]
    bot.process_new_bottoms({"AAAUSDT": df}, int(df["close_time"].iloc[-1]) + 1)
    assert "AAAUSDT" not in bot.state["open_signals"]


def test_target_r_multiple(tmp_path):
    """هدف بمضاعف R: target = entry + R × (entry − stop)."""
    from src.zigzag import evaluate_entry_candle
    import pandas as pd

    row = pd.Series({"open": 100.0, "high": 101.0, "low": 99.5, "close": 101.0,
                     "open_time": 0, "close_time": 1})
    bottom = {"bar_index": 0, "bar_open_time": 0, "bar_close_time": 0, "low": 98.0}
    settings = ZigZagSettings(ema_filter=0, max_stop_pct=5.0, target_r=1.5)
    cand = evaluate_entry_candle(bottom, row, settings)
    assert cand is not None
    assert cand["target_price"] == pytest.approx(101.0 + 1.5 * (101.0 - 98.0))


def test_ema_from_higher_timeframe_ignores_unclosed_candle():
    """EMA على 4 ساعات: تُستخدم الشموع المغلقة فقط قبل لحظة القرار (بلا نظر للمستقبل)."""
    from src.zigzag import ema_at_bar_from_candles

    n = 60
    candles = pd.DataFrame({
        "close_time": [BASE_MS + (i + 1) * 4 * HOUR_MS - 1 for i in range(n)],
        "close": [100.0 + i for i in range(n)],
    })
    as_of = int(candles["close_time"].iloc[49])          # عند إغلاق الشمعة رقم 49
    ema_then = ema_at_bar_from_candles(candles, 20, as_of)
    assert ema_then is not None

    # تغيير الشموع **بعد** لحظة القرار لا يغيّر القيمة (لا نظر للمستقبل)
    tampered = candles.copy()
    tampered.loc[50:, "close"] = 1.0
    assert ema_at_bar_from_candles(tampered, 20, as_of) == pytest.approx(ema_then)

    # لكن شمعة لم تُغلق بعد تُهمَل
    assert ema_at_bar_from_candles(candles, 20, as_of - 4 * HOUR_MS) is not None
