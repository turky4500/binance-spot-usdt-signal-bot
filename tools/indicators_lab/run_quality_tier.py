"""مختبر طبقة الجودة: هل يوجد شرط إضافي يرفع جودة نظام «ارتداد الاتجاه اليومي»؟

القاعدة الحاكمة (منعًا للخداع الإحصائي):
  أي شرط يجب أن يُحسّن المتوسط **في نصفي الفترة معًا** (تقسيم زمني بالنصف) وبعدد
  كافٍ من الصفقات، وإلا يُرفض حتى لو كان رائعًا في المتوسط الكلي.

المدخلات: كاش شمعات الساعة في tools/indicators_lab/data_cache/1h (495 عملة × 2000 شمعة).
المخرج: reports/quality_tier.md
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
from tools.indicators_lab.quant_systems import GOOD_HOURS, daily_context  # noqa: E402

CACHE = ROOT / "tools" / "indicators_lab" / "data_cache" / "1h"
MIN_SUBSET = 250          # حد أدنى لصفقات الشرط كي يُنظر إليه أصلًا
MIN_HALF_SUBSET = 80      # حد أدنى في كل نصف
TARGET_MOVE = 0.10        # تحسّن يجب أن يبلغه الشرط في كل نصف (%/صفقة)


def rsi(series: pd.Series, length: int) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0.0).ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    down = (-delta.clip(upper=0.0)).ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    rs = up / down.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def collect_trades() -> pd.DataFrame:
    settings = StrategySettings()
    rows: list[dict] = []
    files = sorted(CACHE.glob("*.csv.gz"))
    for n, path in enumerate(files, 1):
        symbol = path.name.replace(".csv.gz", "")
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        if len(df) < 400:
            continue
        frame = prepare_strategy_frame(df, settings)
        atr = frame["atr"]
        d1 = daily_context(df)
        close = df["close"].astype(float)
        ema20 = close.ewm(span=20, adjust=False, min_periods=20).mean()
        ema200 = close.ewm(span=200, adjust=False, min_periods=200).mean()
        rsi2 = rsi(close, 2)
        rsi14 = rsi(close, 14)
        vol_sma = df["volume"].astype(float).rolling(20, min_periods=10).mean()
        rvol = (df["volume"].astype(float) / vol_sma).replace([np.inf, -np.inf], np.nan)

        entries = (
            (close < ema20) & (close.shift(1) >= ema20.shift(1))   # أول إغلاق تحت المتوسط
            & d1["d1_trend_up"].fillna(False)
            & d1["hour"].isin(GOOD_HOURS)
        ).fillna(False)
        if entries.sum() == 0:
            continue

        trades = simulate(df, entries, atr, cooldown_bars=1)
        if trades.empty:
            continue
        trades["symbol"] = symbol
        t = trades.copy()
        idx = t["entry_index"].astype(int)
        t["hour"] = d1["hour"].to_numpy()[idx]
        t["rsi2"] = rsi2.to_numpy()[idx]
        t["rsi14"] = rsi14.to_numpy()[idx]
        t["rvol"] = rvol.to_numpy()[idx]
        t["adx"] = frame["adx"].to_numpy()[idx]
        t["atr_pct"] = (atr / close * 100.0).to_numpy()[idx]
        t["depth_pct"] = ((close - ema20) / ema20 * 100.0).to_numpy()[idx]      # سالب = تحت المتوسط
        t["depth_atr"] = ((ema20 - close) / atr).to_numpy()[idx]                # عمق التراجع بوحدات ATR
        t["above_ema200"] = (close > ema200).to_numpy()[idx]
        t["d1_gap_pct"] = ((d1["d1_close"] - d1["d1_sma50"]) / d1["d1_sma50"] * 100.0).to_numpy()[idx]
        red = (close < close.shift(1)).astype(int)
        t["red_streak"] = red.rolling(5).sum().to_numpy()[idx]                   # كم إغلاق هابط في آخر 5 شمعات
        rows.append(t)
        if n % 50 == 0:
            print(f"  ... {n}/{len(files)} عملة | تراكم {sum(len(r) for r in rows)} صفقة")

    all_t = pd.concat(rows, ignore_index=True)
    all_t["entry_dt"] = pd.to_datetime(all_t["entry_time"], unit="ms", utc=True)
    return all_t.sort_values("entry_dt").reset_index(drop=True)


def evaluate(df: pd.DataFrame, name: str, mask: pd.Series, mid_time: pd.Timestamp) -> dict:
    sub = df[mask.fillna(False)]
    h1, h2 = sub[sub["entry_dt"] < mid_time], sub[sub["entry_dt"] >= mid_time]
    base_h1 = df[df["entry_dt"] < mid_time]["net_pct"].mean()
    base_h2 = df[df["entry_dt"] >= mid_time]["net_pct"].mean()
    return {
        "الشرط": name,
        "n": len(sub),
        "n1": len(h1), "n2": len(h2),
        "متوسط": sub["net_pct"].mean() if len(sub) else np.nan,
        "نجاح%": (sub["net_pct"] > 0).mean() * 100 if len(sub) else np.nan,
        "نصف1": h1["net_pct"].mean() if len(h1) else np.nan,
        "نصف2": h2["net_pct"].mean() if len(h2) else np.nan,
        "تحسن1": (h1["net_pct"].mean() - base_h1) if len(h1) else np.nan,
        "تحسن2": (h2["net_pct"].mean() - base_h2) if len(h2) else np.nan,
    }


def main() -> None:
    print("جمع الصفقات من كاش الشمعات...")
    df = collect_trades()
    print(f"إجمالي الصفقات: {len(df)} | من {df['symbol'].nunique()} عملة")
    mid = df["entry_dt"].quantile(0.5, interpolation="nearest")
    base = df["net_pct"].mean()
    print(f"خط الأساس: {base:+.3f}%/صفقة | نجاح {(df['net_pct']>0).mean()*100:.1f}% | نقطة المنتصف {mid.date()}")

    conditions: list[tuple[str, pd.Series]] = [
        ("كل الإشارات (خط الأساس)", pd.Series(True, index=df.index)),
        ("الساعات المركّزة 0/5/6", df["hour"].isin([0, 5, 6])),
        ("rsi2 < 10", df["rsi2"] < 10),
        ("rsi2 < 5", df["rsi2"] < 5),
        ("rsi2 < 2", df["rsi2"] < 2),
        ("rsi14 < 40", df["rsi14"] < 40),
        ("rsi14 < 35", df["rsi14"] < 35),
        ("عمق التراجع ≤ -1%", df["depth_pct"] <= -1),
        ("عمق التراجع ≤ -2%", df["depth_pct"] <= -2),
        ("عمق التراجع ≤ -3%", df["depth_pct"] <= -3),
        ("عمق التراجع بين 0 و-1% فقط", (df["depth_pct"] > -1) & (df["depth_pct"] < 0)),
        ("عمق ≥ 1×ATR تحت المتوسط", df["depth_atr"] >= 1),
        ("عمق ≥ 1.5×ATR تحت المتوسط", df["depth_atr"] >= 1.5),
        ("حجم نسبي 1.2-2.5 (فلتر البوت)", (df["rvol"] >= 1.2) & (df["rvol"] <= 2.5)),
        ("حجم نسبي > 1.5", df["rvol"] > 1.5),
        ("حجم نسبي < 1.5", df["rvol"] < 1.5),
        ("ADX > 20", df["adx"] > 20),
        ("ADX > 25", df["adx"] > 25),
        ("ADX < 20 (سوق هادئ)", df["adx"] < 20),
        ("فوق متوسط 200 ساعة", df["above_ema200"] == True),  # noqa: E712
        ("تقلب ATR% < 2", df["atr_pct"] < 2),
        ("تقلب ATR% > 2", df["atr_pct"] > 2),
        ("فارق اليوم > 0 حتى 10%", (df["d1_gap_pct"] > 0) & (df["d1_gap_pct"] <= 10)),
        ("فارق اليوم > 10%", df["d1_gap_pct"] > 10),
        ("إغلاقان هابطان على الأقل", df["red_streak"] >= 2),
        ("3 إغلاقات هابطة على الأقل", df["red_streak"] >= 3),
        ("5 إغلاقات هابطة (هبوط متصل)", df["red_streak"] >= 5),
    ]

    results = []
    for name, mask in conditions:
        r = evaluate(df, name, mask, mid)
        r["اجتاز؟"] = (
            r["n"] >= MIN_SUBSET and r["n1"] >= MIN_HALF_SUBSET and r["n2"] >= MIN_HALF_SUBSET
            and pd.notna(r["تحسن1"]) and pd.notna(r["تحسن2"])
            and r["تحسن1"] >= TARGET_MOVE and r["تحسن2"] >= TARGET_MOVE
        )
        results.append(r)
    res = pd.DataFrame(results)
    res.to_csv(ROOT / "reports" / "quality_tier_grid.csv", index=False)

    passed = res[res["اجتاز؟"]].sort_values("متوسط", ascending=False)
    print("\n=== الشروط التي اجتازت (تحسّن ≥ +0.10% في النصفين معًا) ===")
    if passed.empty:
        print("لا شيء — لا شرط اجتاز الفحص المزدوج.")
    else:
        print(passed[["الشرط", "n", "متوسط", "نجاح%", "تحسن1", "تحسن2"]].to_string(index=False))

    # الحكم النهائي
    lines = [
        "# مختبر طبقة الجودة — نظام «ارتداد الاتجاه اليومي»",
        "",
        "> السؤال: هل يوجد شرط إضافي يرفع الجودة (متوسط الصفقة) بشكل يصمد في **نصفي الفترة**؟",
        "> القاعدة الحاكمة: يُشترط تحسّن ≥ +0.10%/صفقة في النصفين معًا وعدد صفقات كافٍ — وإلا يُرفض.",
        "",
        f"- الصفقات: **{len(df):,}** من **{df['symbol'].nunique()}** عملة (2000 شمعة ساعة لكل عملة)",
        f"- خط الأساس: **{base:+.3f}%/صفقة** بنجاح **{(df['net_pct']>0).mean()*100:.1f}%**",
        f"- منتصف الفترة الزمني: **{mid.date()}**",
        "",
        "## النتيجة",
        "",
    ]
    if passed.empty:
        lines += [
            "**لا شرط إضافي اجتاز الفحص المزدوج.** أي شرط يبدو جيدًا في المتوسط الكلي يفشل في أحد النصفين.",
            "",
            "الاستنتاج: لا نضيف شروطًا جديدة من عندنا — أي «تحسين» إضافي سيكون خداعًا إحصائيًا (نفس درس ترتيب العملات).",
        ]
    else:
        lines += ["الشروط التي صمدت:", ""]
        for _, r in passed.iterrows():
            lines.append(
                f"- **{r['الشرط']}** — n={int(r['n'])} | متوسط {r['متوسط']:+.3f}% | نجاح {r['نجاح%']:.1f}% "
                f"| تحسّن النصف الأول {r['تحسن1']:+.3f}% والثاني {r['تحسن2']:+.3f}%"
            )
    lines += [
        "",
        "## الجدول الكامل (كل الشروط المجرَّبة)",
        "",
        "| الشرط | n | متوسط % | نجاح % | تحسن نصف1 | تحسن نصف2 | اجتاز؟ |",
        "|---|---|---|---|---|---|---|",
    ]
    for _, r in res.iterrows():
        lines.append(
            f"| {r['الشرط']} | {int(r['n'])} | {r['متوسط']:+.3f} | {r['نجاح%']:.1f} | "
            f"{r['تحسن1']:+.3f} | {r['تحسن2']:+.3f} | {'✅' if r['اجتاز؟'] else '—'} |"
        )
    (ROOT / "reports" / "quality_tier.md").write_text("\n".join(lines), encoding="utf-8")
    print("\nحُفظ: reports/quality_tier.md + reports/quality_tier_grid.csv")


if __name__ == "__main__":
    main()
