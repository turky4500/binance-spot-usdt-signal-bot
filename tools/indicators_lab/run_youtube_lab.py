"""مختبر استراتيجيات يوتيوب — دفعة أولى.

الاستراتيجيات الشائعة على يوتيوب/المقالات، مُحوَّلة لقواعد صارمة بلا نظر للمستقبل:
  1) IBS (قوة الشمعة الداخلية): شراء عند IBS<0.2 (الكلاسيكي)، خروج عند IBS>0.8 أو بالهدف/الوقف.
  2) Williams %R: شراء عند Oversold (< -80) على الساعة مع اتجاه يومي صاعد.
  3) ORB (اختراق أعلى نقطة في أول ساعة من اليوم UTC): شراء عند أول إغلاق فوقها.
  4) VWAP مرجّح بالحجم (مرساة يومية): شراء عند النزول 1% تحت VWAP مع اتجاه صاعد.
  5) تقاطع EMA 9/21 مع فلتر EMA200 ساعة.
  6) ثلاثة إغلاقات هابطة متتالية + اتجاه يومي صاعد.

كل نظام يُختبر بإعدادين للخروج: (أ) هدف 2% (قياسنا القياسي) و(ب) هدف 1.2% (تفضيل المستخدم).
الحكم بقاعدة النصفين: لا ترقية إلا بإيجابية النصفين معًا وعدد كافٍ.
المخرج: reports/youtube_lab.md + reports/youtube_lab.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.strategy import StrategySettings, prepare_strategy_frame  # noqa: E402
from tools.indicators_lab.engine import simulate  # noqa: E402
from tools.indicators_lab.quant_systems import daily_context  # noqa: E402

CACHE = ROOT / "tools" / "indicators_lab" / "data_cache" / "1h"
MIN_SUBSET = 300
TARGETS = [2.0, 1.2]  # أ) هدف قياسي • ب) هدف صغير (تفضيل المستخدم)


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def williams_r(df: pd.DataFrame, n: int = 14) -> pd.Series:
    hh = df["high"].rolling(n).max()
    ll = df["low"].rolling(n).min()
    return -100.0 * (hh - df["close"]) / (hh - ll)


def day_running_range(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """أعلى/أدنى اليوم الجاري حتى الشمعة الحالية (معلوم لحظة الإغلاق — بلا مستقبل)."""
    day = df["open_time"].astype("int64") // 86_400_000
    g = df.groupby(day)
    hi = g["high"].cummax()
    lo = g["low"].cummin()
    return hi, lo


def anchored_vwap(df: pd.DataFrame) -> pd.Series:
    day = df["open_time"].astype("int64") // 86_400_000
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = (tp * df["volume"]).groupby(day).cumsum()
    vv = df["volume"].groupby(day).cumsum().replace(0, np.nan)
    return pv / vv


def orb_first_hour_levels(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """قمة/أدنى أول شمعة ساعة في كل يوم UTC، ممدودة لكل شمعات اليوم التالي."""
    day = df["open_time"].astype("int64") // 86_400_000
    first_idx = df.groupby(day).head(1).index
    fh = pd.Series(np.nan, index=df.index)
    fl = pd.Series(np.nan, index=df.index)
    fh.loc[first_idx] = df.loc[first_idx, "high"].to_numpy()
    fl.loc[first_idx] = df.loc[first_idx, "low"].to_numpy()
    tmp = pd.DataFrame({"day": day, "fh": fh, "fl": fl})
    grp = tmp.groupby("day").agg(fh=("fh", "last"), fl=("fl", "last"))
    return grp["fh"].reindex(day).to_numpy(), grp["fl"].reindex(day).to_numpy()


def build_signals(df: pd.DataFrame) -> dict[str, pd.Series]:
    close = df["close"].astype(float)
    d1 = daily_context(df)
    d1_up = d1["d1_trend_up"].fillna(False)
    out: dict[str, pd.Series] = {}

    # 1) IBS — قوة الشمعة الداخلية (نطاق اليوم الجاري)
    hi_run, lo_run = day_running_range(df)
    ibs = (close - lo_run) / (hi_run - lo_run).replace(0, np.nan)
    out["IBS<0.2 + اتجاه صاعد"] = (ibs < 0.2) & d1_up
    out["IBS<0.1 + اتجاه صاعد"] = (ibs < 0.1) & d1_up

    # 2) Williams %R
    wr = williams_r(df, 14)
    out["Williams%R<-80 + اتجاه صاعد"] = (wr < -80) & d1_up
    out["Williams%R<-90 + اتجاه صاعد"] = (wr < -90) & d1_up

    # 3) ORB — اختراق قمة أول ساعة من اليوم
    fh, fl = orb_first_hour_levels(df)
    fh_s, fl_s = pd.Series(fh, index=df.index), pd.Series(fl, index=df.index)
    first_bar = ~df.index.isin(df.groupby(df["open_time"].astype("int64") // 86_400_000).head(1).index)
    out["ORB فوق أول ساعة + اتجاه"] = (close > fh_s) & first_bar & d1_up
    out["ORB فوق أول ساعة (بلا فلتر)"] = (close > fh_s) & first_bar

    # 4) VWAP مرساة يومية
    vwap = anchored_vwap(df)
    out["تحت VWAP بـ1% + اتجاه"] = (close < vwap * 0.99) & d1_up

    # 5) تقاطع EMA 9/21 مع EMA200
    e9, e21, e200 = ema(close, 9), ema(close, 21), ema(close, 200)
    cross_up = (e9 > e21) & (e9.shift(1) <= e21.shift(1))
    out["تقاطع 9/21 + EMA200"] = cross_up & (close > e200)

    # 6) ثلاثة إغلاقات هابطة
    red3 = (close < close.shift(1)) & (close.shift(1) < close.shift(2)) & (close.shift(2) < close.shift(3))
    out["3 إغلاقات هابطة + اتجاه"] = red3 & d1_up
    return out


def main() -> None:
    settings = StrategySettings()
    files = sorted(CACHE.glob("*.csv.gz"))
    collected: dict[str, list[pd.DataFrame]] = {}

    for n, path in enumerate(files, 1):
        symbol = path.name.replace(".csv.gz", "")
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        if len(df) < 500:
            continue
        frame = prepare_strategy_frame(df, settings)
        atr = frame["atr"]
        try:
            signals = build_signals(df)
        except Exception as exc:
            print("  skip", symbol, exc)
            continue
        for name, sig in signals.items():
            for target in TARGETS:
                if sig.sum() == 0:
                    continue
                trades = simulate(df, sig.fillna(False), atr, cooldown_bars=1, target_pct=target)
                if trades.empty:
                    continue
                t = trades.copy()
                t["symbol"] = symbol
                t["target_used"] = target
                collected.setdefault(name, []).append(t)
        if n % 120 == 0:
            print(f"  ... {n}/{len(files)}")

    results = []
    for name, parts in collected.items():
        t = pd.concat(parts, ignore_index=True)
        t["entry_dt"] = pd.to_datetime(t["entry_time"], unit="ms", utc=True)
        t = t.sort_values("entry_dt").reset_index(drop=True)
        mid = t["entry_dt"].quantile(0.5, interpolation="nearest")
        for target in TARGETS:
            sub = t[t["target_used"] == target]
            if len(sub) < MIN_SUBSET:
                continue
            h1, h2 = sub[sub["entry_dt"] < mid], sub[sub["entry_dt"] >= mid]
            net = sub["net_pct"]
            results.append({
                "النظام": name, "الهدف%": target, "n": len(sub),
                "نجاح%": (net > 0).mean() * 100,
                "متوسط%": net.mean(),
                "نصف1%": h1["net_pct"].mean(), "نصف2%": h2["net_pct"].mean(),
            })
    res = pd.DataFrame(results)
    # أفضل هدف لكل نظام
    res["مفتاح"] = res["النظام"] + "|" + res["الهدف%"].astype(str)
    res = res.sort_values(["النظام", "متوسط%"], ascending=[True, False])
    res.to_csv(ROOT / "reports" / "youtube_lab.csv", index=False, float_format="%.3f")

    print("\n=== النتائج الكاملة ===")
    print(res.to_string(index=False, float_format=lambda x: f"{x:+.3f}"))

    winners = res[(res["نصف1%"] > 0) & (res["نصف2%"] > 0) & (res["متوسط%"] > 0)]
    lines = [
        "# مختبر استراتيجيات يوتيوب — الدفعة الأولى",
        "",
        "> 495 عملة × 2000 شمعة ساعة • نفس محرك القياس (دخول بإغلاق الشمعة، خروج من التالية، الوقف أولًا، عمولة 0.1%/جهة) • **قاعدة النصفين**: لا توصية إلا بإيجابية النصفين معًا.",
        "> كل نظام بإعدادين: هدف +2% (قياسنا القياسي) وهدف +1.2% (تفضيل المستخدم).",
        "",
        "| النظام | الهدف | n | نجاح% | متوسط%/صفقة | نصف1 | نصف2 |",
        "|---|---|---|---|---|---|---|",
    ]
    for _, r in res.iterrows():
        lines.append(f"| {r['النظام']} | {r['الهدف%']:.1f}% | {int(r['n'])} | {r['نجاح%']:.1f} | {r['متوسط%']:+.3f} | {r['نصف1%']:+.3f} | {r['نصف2%']:+.3f} |")
    lines += ["", "## الحكم", ""]
    if winners.empty:
        lines += ["**لا نظام واحد اجتاز فحص النصفين.** لا نُوصي بشيء من هذه الدفعة للحياة (قد تُسجَّل ظلّيًا عند الطلب)."]
    else:
        for _, r in winners.iterrows():
            lines.append(f"- 🟢 **{r['النظام']}** (هدف {r['الهدف%']:.1f}%): متوسط {r['متوسط%']:+.3f}% • نجاح {r['نجاح%']:.1f}% • ن={int(r['n'])}")
    lines += [
        "",
        "## مراجع القواعد (من مصادر يوتيوب/المقالات)",
        "",
        "- IBS: شراء عند IBS<0.2 (=(إغلاق−أدنى)/(أعلى−أدنى) لنطاق اليوم) وبيع عند IBS>0.8 — quantifiedstrategies.com",
        "- Williams %R: (أعلى قمة − إغلاق)/(أعلى قمة − أدنى قاع) × −100، الشراء في < −80 — quantifiedstrategies.com",
        "- ORB: إغلاق خارج نطاق الافتتاح مع حجم أعلى واتجاه — forextester.com",
        "",
        "ملاحظة: الخروج في الاختبار هو خروجنا القياسي (هدف/وقف 1.5×ATR) وليس خروج المصدر الأصلي — للاتساق والمقارنة العادلة.",
    ]
    (ROOT / "reports" / "youtube_lab.md").write_text("\n".join(lines), encoding="utf-8")
    print("\nحُفظ: reports/youtube_lab.md + youtube_lab.csv")


if __name__ == "__main__":
    main()
