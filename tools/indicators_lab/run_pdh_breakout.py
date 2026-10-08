"""اختبار استراتيجية «اختراق قمة الأمس» (Previous Day High Breakout).

الفكرة (كما طلب المستخدم):
  - نحسب من شمعة الأمس اليومية (المكتملة) خطين: قمة الأمس (High) وإغلاق الأمس (Close).
  - عندما تُغلق شمعة ساعة **فوق قمة الأمس** (أول اختراق) → إشارة شراء.
  - الخروج بهدف صغير (1–1.5%) أو وقف أو (في نسخة أخرى) النزول تحت إغلاق الأمس.

المنهج: نفس محرك المختبر (دخول عند إغلاق الشمعة، خروج من الشمعة التالية،
الوقف أولًا عند لمس الاثنين، عمولة 0.1%/جهة) + **فحص النصفين** كما في كل قراراتنا.

المخرج: reports/pdh_breakout.md + reports/pdh_breakout_grid.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.strategy import StrategySettings, prepare_strategy_frame  # noqa: E402

CACHE = ROOT / "tools" / "indicators_lab" / "data_cache" / "1h"
FEE = 0.1  # % لكل جهة
ATR_MULT = 1.5
MIN_STOP, MAX_STOP = 1.2, 2.5
MAX_HOLD_BARS = 48  # مهلة قصوى (ساعتان يوميًا × يومين) لمنع الصفقات المعلّقة


def prev_day_levels(open_ms: np.ndarray, high: np.ndarray, close: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """قمة الأمس وإغلاق الأمس لكل شمعة — من الأيام المكتملة فقط (توقيت UTC = أيام Binance)."""
    day = open_ms // 86_400_000
    df = pd.DataFrame({"day": day, "high": high, "close": close})
    daily = df.groupby("day").agg(h=("high", "max"), c=("close", "last"))
    prev = daily.shift(1)
    lvl_high = prev["h"].reindex(day).to_numpy()
    lvl_close = prev["c"].reindex(day).to_numpy()
    return lvl_high, lvl_close


def simulate_symbol(
    df: pd.DataFrame,
    atr: np.ndarray,
    target_pct: float,
    stop_mode: str,          # atr | fixed_1.0 | fixed_1.5
    exit_on_pdclose: bool,   # خروج عند إغلاق تحت إغلاق الأمس (نسخة «النزول عنها خروج»)
) -> list[dict]:
    close = df["close"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    open_ms = df["open_time"].to_numpy(dtype=np.int64)
    lvl_high, lvl_close = prev_day_levels(open_ms, high, close)
    n = len(df)
    trades: list[dict] = []
    last_exit = -10**9
    for i in range(1, n - 1):
        if np.isnan(lvl_high[i]) or np.isnan(atr[i]) or (i - last_exit) < 1:
            continue
        # أول اختراق: الشمعة الحالية أغلقت فوق قمة الأمس والشمعة السابقة كانت تحتها
        if not (close[i] > lvl_high[i] and close[i - 1] <= lvl_high[i - 1]):
            continue
        entry = close[i]
        if stop_mode == "atr":
            stop_pct = float(np.clip(ATR_MULT * atr[i] / entry * 100.0, MIN_STOP, MAX_STOP))
        elif stop_mode == "fixed_1.0":
            stop_pct = 1.0
        else:
            stop_pct = 1.5
        stop = entry * (1 - stop_pct / 100.0)
        target = entry * (1 + target_pct / 100.0)
        outcome = exit_price = None
        exit_i = None
        exit_reason = None
        for j in range(i + 1, min(i + 1 + MAX_HOLD_BARS, n)):
            if low[j] <= stop:
                outcome, exit_price, exit_i, exit_reason = "stop", stop, j, "stop"
                break
            if high[j] >= target:
                outcome, exit_price, exit_i, exit_reason = "target", target, j, "target"
                break
            if exit_on_pdclose and close[j] < lvl_close[j]:
                outcome = "signal_win" if close[j] > entry else "signal_loss"
                exit_price, exit_i, exit_reason = float(close[j]), j, "below_prev_close"
                break
        if outcome is None:
            j = min(i + MAX_HOLD_BARS, n - 1)
            outcome = "timeout_win" if close[j] > entry else "timeout_loss"
            exit_price, exit_i, exit_reason = float(close[j]), j, "timeout"
        net = (exit_price / entry - 1.0) * 100.0 - 2 * FEE
        trades.append({
            "entry_index": i, "entry_time": int(open_ms[i]), "exit_time": int(open_ms[exit_i]),
            "entry_price": entry, "exit_price": exit_price, "stop_pct": stop_pct,
            "outcome": outcome, "exit_reason": exit_reason, "net_pct": net,
            "bars_held": exit_i - i,
        })
        last_exit = exit_i
    return trades


def main() -> None:
    settings = StrategySettings()
    files = sorted(CACHE.glob("*.csv.gz"))
    variants = [
        ("هدف 1.0% • وقف ATR", 1.0, "atr", False),
        ("هدف 1.2% • وقف ATR", 1.2, "atr", False),
        ("هدف 1.5% • وقف ATR", 1.5, "atr", False),
        ("هدف 1.5% • وقف ثابت 1.0%", 1.5, "fixed_1.0", False),
        ("هدف 1.5% • وقف ثابت 1.5%", 1.5, "fixed_1.5", False),
        ("هدف 2.0% • وقف ATR", 2.0, "atr", False),
        ("بلا هدف • خروج تحت إغلاق الأمس • وقف ATR", 50.0, "atr", True),
    ]
    collected: dict[str, list[pd.DataFrame]] = {name: [] for name, *_ in variants}

    for n, path in enumerate(files, 1):
        symbol = path.name.replace(".csv.gz", "")
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        if len(df) < 500:
            continue
        frame = prepare_strategy_frame(df, settings)
        atr = frame["atr"].to_numpy(dtype=float)
        for name, tgt, smode, exit_pdc in variants:
            tr = simulate_symbol(df, atr, tgt, smode, exit_pdc)
            if tr:
                t = pd.DataFrame(tr)
                t["symbol"] = symbol
                collected[name].append(t)
        if n % 100 == 0:
            print(f"  ... {n}/{len(files)}")

    results = []
    details: dict[str, pd.DataFrame] = {}
    for name, *_ in variants:
        if not collected[name]:
            continue
        t = pd.concat(collected[name], ignore_index=True)
        t["entry_dt"] = pd.to_datetime(t["entry_time"], unit="ms", utc=True)
        t = t.sort_values("entry_dt").reset_index(drop=True)
        details[name] = t
        mid = t["entry_dt"].quantile(0.5, interpolation="nearest")
        h1, h2 = t[t["entry_dt"] < mid], t[t["entry_dt"] >= mid]
        net = t["net_pct"]
        eq = np.cumsum(net.to_numpy())
        results.append({
            "النسخة": name, "n": len(t),
            "نجاح%": (net > 0).mean() * 100,
            "متوسط%": net.mean(),
            "مجموع%": net.sum(),
            "نصف1%": h1["net_pct"].mean(), "نصف2%": h2["net_pct"].mean(),
            "هدف%": (t["exit_reason"] == "target").mean() * 100,
            "وقف%": (t["exit_reason"] == "stop").mean() * 100,
            "مهلة%": (t["exit_reason"] == "timeout").mean() * 100,
            "أسوأ تراجع%": (eq - np.maximum.accumulate(eq)).min(),
        })
    res = pd.DataFrame(results)
    res.to_csv(ROOT / "reports" / "pdh_breakout_grid.csv", index=False, float_format="%.3f")
    print("\n=== النتائج ===")
    print(res.to_string(index=False, float_format=lambda x: f"{x:+.3f}"))

    best = res.sort_values("متوسط%", ascending=False).iloc[0]
    lines = [
        "# اختبار «اختراق قمة الأمس» (Previous Day High Breakout)",
        "",
        "> الفكرة: عندما تُغلق شمعة ساعة فوق **قمة الأمس اليومية** (أول اختراق) → شراء، مع هدف صغير ووقف.",
        f"> المنهج: {len(files)} عملة من كاش الساعة (2000 شمعة) • دخول بإغلاق شمعة الاختراق • خروج من الشمعة التالية • الوقف أولًا عند لمس الاثنين • عمولة 0.1%/جهة • مهلة قصوى {MAX_HOLD_BARS} ساعة.",
        "",
        "## جدول النتائج",
        "",
        "| النسخة | n | نجاح% | متوسط%/صفقة | مجموع% | نصف1 | نصف2 | بلغ الهدف% | وقف% | مهلة% | أسوأ تراجع% |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for _, r in res.iterrows():
        lines.append(
            f"| {r['النسخة']} | {int(r['n'])} | {r['نجاح%']:.1f} | {r['متوسط%']:+.3f} | {r['مجموع%']:+.0f} | "
            f"{r['نصف1%']:+.3f} | {r['نصف2%']:+.3f} | {r['هدف%']:.0f} | {r['وقف%']:.0f} | {r['مهلة%']:.0f} | {r['أسوأ تراجع%']:+.0f} |"
        )
    lines += [
        "",
        "## الحكم",
        "",
        f"- أفضل نسخة: **{best['النسخة']}** بمتوسط {best['متوسط%']:+.3f}%/صفقة ونجاح {best['نجاح%']:.1f}%.",
        "- القاعدة الحاكمة (نصفان): أي نسخة لا تكون موجبة في **النصفين** لا تُرقّى للحياة.",
        f"- معدل الإشارات: {res['n'].max() / len(files) / 83:.2f} لكل عملة يوميًا تقريبًا (83 يومًا من البيانات).",
        "",
        "التفاصيل الكاملة في reports/pdh_breakout_grid.csv",
    ]
    (ROOT / "reports" / "pdh_breakout.md").write_text("\n".join(lines), encoding="utf-8")
    print("\nحُفظ: reports/pdh_breakout.md + pdh_breakout_grid.csv")


if __name__ == "__main__":
    main()
