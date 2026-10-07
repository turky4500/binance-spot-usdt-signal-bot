"""أنظمة كمية وفنية جديدة — مُستخرجة من أبحاث فعلية لم تُختبر عندنا بعد.

المصادر الملهمة (كلها قابلة للاختبار بنفس المحرك):
1. قناة Donchian (اختراق أعلى قمة 20 شمعة) — Trend Following كلاسيكي.
   أوراق/اختبارات: BTC 4H +94.8% (Sharpe 1.95) • EUR/USD 4H 52% → 63% مع فلتر ADX والحجم.
2. فلتر الاتجاه اليومي (D1) + دخول ساعي (H1) — Quantpedia:
   MACD ساعة وحده Sharpe 0.33 → مع فلتر D1 صار 0.80 → مع تتبع 1.07.
   (وهو نفس ما وصفه المستخدم: «يحلل على اليوم ويدخل على الساعة»)
3. ارتداد RSI(2) — Larry Connors (شراء في اتجاه صاعد عند تشبع بيعي حاد) وخروج عند العودة.
4. اختراق جلسة لندن (Session breakout) — اختراق مدى ما قبل لندن خلال ساعات لندن.
5. دمج إشارة البوت الحالية مع فلتر يومي (لتحسين أساس البوت لا استبداله).

قواعد مشتركة (نفس engine.py): هدف 2% • وقف 1.5×ATR مقيّد 1.2–2.5% • عمولة 0.1%/جهة.
الأنظمة ذات exit_signal تستخدم «خروج بإشارة» مع بقاء الوقف/الهدف شبكة أمان.

⚠️ منع النظر للمستقبل: كل القنوات والمتوسطات مُزاحة شمعة/يومًا واحدًا (shift(1)).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.strategy import (  # noqa: E402
    StrategySettings,
    bollinger,
    dmi,
    ema,
    macd,
    prepare_strategy_frame,
    rsi,
)

TZ_OFFSET_MS = 3 * 3_600_000  # توقيت الرياض
GOOD_HOURS = {0, 2, 5, 6, 14, 21, 23}  # من قياس الحافة الزمنية (خارج العينة t=+3.15)
LONDON_START, LONDON_END = 10, 13  # نافذة لندن بتوقيت الرياض


def local_hours(open_ms: pd.Series) -> pd.Series:
    return ((open_ms.astype("int64") + TZ_OFFSET_MS) // 3_600_000) % 24


def local_day(open_ms: pd.Series) -> pd.Series:
    return ((open_ms.astype("int64") + TZ_OFFSET_MS) // 86_400_000).astype("int64")


def daily_context(df: pd.DataFrame) -> pd.DataFrame:
    """سياق الإطار اليومي لكل شمعة ساعية — من الأيام المكتملة فقط (بلا نظر للمستقبل).

    لكل شمعة ساعية في اليوم D: نستخدم قيم اليوم D-1 المكتمل.
    """
    hours = local_hours(df["open_time"])
    days = local_day(df["open_time"])
    tmp = pd.DataFrame({"day": days.to_numpy(), "close": df["close"].to_numpy(dtype=float)})
    daily = tmp.groupby("day")["close"].last().to_frame("close")
    daily["ema20"] = ema(daily["close"], 20)
    daily["sma50"] = daily["close"].rolling(50, min_periods=20).mean()
    macd_line, signal_line, _ = macd(daily["close"])
    daily["macd_line"], daily["macd_signal"] = macd_line, signal_line
    # إزاحة يوم كامل: قيمة اليوم السابق فقط
    shifted = daily.shift(1)
    frames = shifted.reindex(days.to_numpy()).reset_index(drop=True)
    frames.index = df.index
    out = pd.DataFrame({
        "d1_close": frames["close"],
        "d1_ema20": frames["ema20"],
        "d1_sma50": frames["sma50"],
        "d1_macd_up": frames["macd_line"] > frames["macd_signal"],
    }, index=df.index)
    out["d1_trend_up"] = (out["d1_close"] > out["d1_sma50"]).fillna(False)
    out["d1_above_ema20"] = (out["d1_close"] > out["d1_ema20"]).fillna(False)
    out["hour"] = hours.to_numpy()
    return out


def donchian(df: pd.DataFrame, length: int = 20) -> pd.DataFrame:
    """قناة Donchian مُزاحة شمعة — أعلى/أدنى/منتصف آخر `length` شمعة قبل الشمعة الحالية."""
    upper = df["high"].rolling(length, min_periods=length).max().shift(1)
    lower = df["low"].rolling(length, min_periods=length).min().shift(1)
    mid = (upper + lower) / 2.0
    return pd.DataFrame({
        "upper": upper, "lower": lower, "mid": mid,
        "break_up": (df["close"] > upper).fillna(False),
        "below_mid": (df["close"] < mid).fillna(False),
    }, index=df.index)


def build_quant_systems(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """كل الأنظمة الجديدة: اسم → DataFrame فيه entry واختياريًا exit_signal و stop_pct."""
    settings = StrategySettings()
    frame = prepare_strategy_frame(df, settings)
    close = df["close"]

    d1 = daily_context(df)
    dc20 = donchian(df, 20)
    dc55 = donchian(df, 55)

    adx = frame["adx"]
    rvol = frame["relative_volume"]

    systems: dict[str, pd.DataFrame] = {}

    def add(name: str, entry: pd.Series, exit_signal: pd.Series | None = None, stop_pct: pd.Series | None = None) -> None:
        spec = pd.DataFrame({"entry": entry.fillna(False)}, index=df.index)
        if exit_signal is not None:
            spec["exit_signal"] = exit_signal.fillna(False)
        if stop_pct is not None:
            spec["stop_pct"] = stop_pct
        systems[name] = spec

    # ============ 1) اختراق قناة Donchian 20 (بلا أي فلتر) ============
    add("qd_donchian20", dc20["break_up"])

    # ============ 2) اختراق قناة Donchian 55 ============
    add("qd_donchian55", dc55["break_up"])

    # ============ 3) Donchian20 + ADX>20 (فلتر اتجاه قوي) ============
    add("qd_donchian20_adx", dc20["break_up"] & (adx > 20))

    # ============ 4) Donchian20 + تأكيد حجم نسبي ============
    add("qd_donchian20_rvol", dc20["break_up"] & (rvol >= 1.2))

    # ============ 5) Donchian20 + الساعات الجيدة فقط ============
    add("qd_donchian20_hours", dc20["break_up"] & d1["hour"].isin(GOOD_HOURS))

    # ============ 6) Donchian20 بمنطق كلاسيكي كامل: دخول عند الاختراق، خروج تحت منتصف القناة ============
    add("qd_donchian20_exitmid", dc20["break_up"], exit_signal=dc20["below_mid"])

    # ============ 7) فلتر يومي صاعد + اختراق ساعي ============
    add("qd_d1h1_donchian", dc20["break_up"] & d1["d1_trend_up"])

    # ============ 8) فلتر يومي + اختراق + خروج بمنتصف القناة ============
    add("qd_d1h1_donchian_exitmid", dc20["break_up"] & d1["d1_trend_up"], exit_signal=dc20["below_mid"])

    # ============ 9) اتجاه يومي + ارتداد لمتوسط 20 ساعة (شراء التراجع في اتجاه صاعد) ============
    h1_ema20 = ema(close, 20)
    pullback = (close < h1_ema20) & (close.shift(1) >= h1_ema20.shift(1))  # أول إغلاق تحت المتوسط
    add("qd_d1h1_pullback", pullback & d1["d1_trend_up"])

    # ============ 10) MACD يومي صاعد + تقاطع MACD ساعي (وصفة Quantpedia) ============
    m_line, m_sig, _ = macd(close)
    macd_cross = (m_line > m_sig) & (m_line.shift(1) <= m_sig.shift(1))
    add("qd_d1h1_macd", macd_cross & d1["d1_macd_up"])

    # ============ 11) ارتداد RSI(2) — Connors: تشبع بيعي حاد، خروج عند العودة فوق المتوسط ============
    rsi2 = rsi(close, 2)
    sma5 = close.rolling(5, min_periods=5).mean()
    add("qd_rsi2_connors", rsi2 < 10, exit_signal=(rsi2 > 70) | (close > sma5))

    # ============ 11-ب) نسخة Connors الكلاسيكية: الشراء في الاتجاه الصاعد فقط ============
    add("qd_rsi2_d1up", (rsi2 < 10) & d1["d1_trend_up"], exit_signal=(rsi2 > 70) | (close > sma5))

    # ============ 11-ج) تشبع بيعي متطرف RSI(2)<5 ============
    add("qd_rsi2_extreme", rsi2 < 5, exit_signal=(rsi2 > 70) | (close > sma5))

    # ============ 12) ارتداد من حد بولنجر السفلي، خروج عند العودة للأساس ============
    bb_mid, bb_up, bb_low = bollinger(close, 20, 2.0)
    add("qd_bb_lower_revert", close < bb_low, exit_signal=close > bb_mid)

    # ============ 12-ب) دمج: اتجاه يومي + ارتداد ساعي + تأكيد تشبع بيعي ============
    stoch = frame["stoch"]
    add("qd_pullback_rsi", pullback & d1["d1_trend_up"] & (rsi(close, 14) < 40))
    add("qd_pullback_stoch", pullback & d1["d1_trend_up"] & (stoch < 35))
    add("qd_pullback_rsi2", pullback & d1["d1_trend_up"] & (rsi2 < 25))

    # ============ 12-ج) ارتداد ساعي أقوى: إغلاق تحت EMA20 بمسافة معتبرة ============
    deep_pullback = (close < h1_ema20 * 0.985) & d1["d1_trend_up"] & (rsi(close, 14) < 45)
    add("qd_d1h1_deep_pullback", deep_pullback)

    # ============ 13) اختراق جلسة لندن: اختراق أعلى مدى 12 ساعة السابقة خلال ساعات لندن ============
    dc12 = donchian(df, 12)
    in_london = (d1["hour"] >= LONDON_START) & (d1["hour"] < LONDON_END)
    add("qd_london_breakout", dc12["break_up"] & in_london)

    # ============ 14) إشارة البوت الحالية + فلتر يومي صاعد ============
    bot_stop_pct = ((close - frame["pivot_stop_candidate"]) / close * 100.0).clip(upper=settings.max_stop_pct)
    add("qd_bot_plus_d1", frame["buy_setup"].fillna(False) & d1["d1_trend_up"], stop_pct=bot_stop_pct)

    return systems


QUANT_SYSTEMS = [
    "qd_donchian20", "qd_donchian55", "qd_donchian20_adx", "qd_donchian20_rvol",
    "qd_donchian20_hours", "qd_donchian20_exitmid", "qd_d1h1_donchian",
    "qd_d1h1_donchian_exitmid", "qd_d1h1_pullback", "qd_d1h1_macd",
    "qd_rsi2_connors", "qd_rsi2_d1up", "qd_rsi2_extreme",
    "qd_bb_lower_revert", "qd_london_breakout", "qd_bot_plus_d1",
    "qd_pullback_rsi", "qd_pullback_stoch", "qd_pullback_rsi2", "qd_d1h1_deep_pullback",
]

# الأنظمة التي تستخدم الخروج بإشارة (بدل هدف/وقف فقط)
SIGNAL_EXIT_SYSTEMS = {
    "qd_donchian20_exitmid", "qd_d1h1_donchian_exitmid", "qd_bb_lower_revert",
    "qd_rsi2_connors", "qd_rsi2_d1up", "qd_rsi2_extreme",
}
