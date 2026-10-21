# -*- coding: utf-8 -*-
"""نظام القاع (سهم يومي) — تحقق ورقي مسبق على بيانات تداول الحقيقية.

يحاكي **حرفيًا** منطق `sa_bot._process_one_trading_date` + `src.saudi_strategy` على
سنوات من الشموع اليومية المعدّلة (Yahoo)، بنفس الترتيب المُختبر:
    رفع الوقف (من قمم سابقة) ← فحص الوقف (تنفيذ الفجوة) ← المهلة (عدّ فهرس) ← تحديث القمة
وإدخال: شمعة خضراء فوق قاع صارم (يمين1/يسار3) وفوق EMA200، مهلة ≥ cooldown شموع،
ولا إعادة دخول في يوم الخروج. الفريم يومي حصراً.

⚠️⚠️ تحذيرات إلزامية قبل قراءة أي رقم ⚠️⚠️
  1) انحياز البقاء (survivorship bias): القائمة = 383 سهمًا مدرجة **الآن**.
     الشركات المُسقطة/المُغلقة غير موجودة → النتائج أفضل من الواقع بطبيعتها.
  2) تنفيذ ورقي (paper): لا انزلاق فعلي، لا حدود سيولة، لا أوراق معلّقة؛ التنفيذ
     مفترض نظيفًا عند سعر الإغلاق أو الوقف. العمولة 0.15% لكل جهة (0.30% ذهابًا
     وإيابًا) افتراضية وقد تختلف عند وسيطك.
  3) نافذة Yahoo محدودة (افتراضيًا 5 سنوات) وأول ~210 شمعة تُستهلك في تمهيد EMA200.
  4) كل أرقام هنا **ورقية** وليست نتائج حيّة ولا ضمان للربح — تحقق مسبق فقط،
     ولا تُستخدم لإرسال أي إشارة حقيقية قبل موافقة المستخدم الصريحة.

التشغيل:
    python backtest_sa.py                # كامل القائمة (383) مع كاش
    python backtest_sa.py --limit 40     # عيّنة أسرع
    python backtest_sa.py --refresh      # تجاهل الكاش وإعادة الجلب
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from src.saudi_data import TadawulClient  # noqa: E402
from src.saudi_strategy import (  # noqa: E402
    SaSettings,
    ema,
    is_strict_pivot_low,
    is_timeout,
    next_stop,
    stop_fill,
)

CAVEATS = [
    "انحياز البقاء: القائمة سهمًا مدرجة اليوم فقط (383) — المُسقطة غائبة ⇒ نتائج أفضل من الواقع.",
    "تنفيذ ورقي: لا انزلاق/سيولة حقيقية؛ العمولة 0.15% لكل جهة (0.30% إجمالًا) افتراضية.",
    "نافذة بيانات محدودة بـYahoo (حدّدها --range) + تمهيد EMA200 يستهلك أول ~210 شمعة.",
    "ورقي وليس نتائج حيّة ولا ضمان للربح — تحقق مسبق قبل أي إشارة حقيقية.",
]


# ───────────────────────────────────────────── إشارة دخول (نسخة O(1) مطابقة لـ evaluate_entry)
def entry_signal_at(
    df: pd.DataFrame,
    i: int,
    settings: SaSettings,
    ema_at: float,
    open_time_to_idx: dict[int, int],
    last_entry_open: int = 0,
    last_exit_open: int = 0,
) -> dict | None:
    """إشارة الدخول على الشمعة المغلقة رقم `i` — نفس شروط evaluate_entry بلا نظر للمستقبل.

    مُتحقَّق منها بالتوازي في tests/test_sa_flow.py::test_backtest_entry_parity
    مقارنةً بـ evaluate_entry على نفس البادئة.
    """
    n = i + 1
    if n < settings.min_bars:
        return None

    lows = df["low"].to_numpy(dtype="float64")
    bar_open = int(df["open_time"].iat[i])

    # ممنوع إعادة دخول في يوم الخروج (مطابقة اكتمال open_time للخروج)
    if last_exit_open and bar_open == last_exit_open:
        return None

    # مهلة ≥ cooldown شموع منذ آخر دخول (عدّ فهرس)
    if last_entry_open:
        li = open_time_to_idx.get(int(last_entry_open))
        if li is not None and (i - li) <= settings.cooldown_bars:
            return None

    # القاع: شمعة الإشارة = شمعة التأكيد (القاع بعدها right_bars للخلف)
    pivot_idx = i - settings.right_bars
    if not is_strict_pivot_low(lows, pivot_idx, settings.left_bars, settings.right_bars):
        return None
    pivot_value = float(lows[pivot_idx])

    entry_price = float(df["close"].iat[i])
    if not (entry_price > float(df["open"].iat[i])):   # خضراء
        return None
    if not (entry_price > pivot_value):                # فوق القاع
        return None
    if not np.isfinite(entry_price) or entry_price <= 0:
        return None
    if not np.isfinite(ema_at) or not (entry_price > ema_at):  # فوق EMA200
        return None

    stop_price = pivot_value
    stop_dist_pct = (entry_price - stop_price) / entry_price * 100.0
    if stop_dist_pct <= 0:
        return None
    return {
        "bar_open_time": bar_open,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "stop_dist_pct": stop_dist_pct,
        "peak_price": float(df["high"].iat[i]),
    }


# ───────────────────────────────────────────── محاكاة رمز واحد (نفس ترتيب الإنتاج)
def simulate_symbol(df: pd.DataFrame, settings: SaSettings) -> tuple[list[dict], float]:
    """يعيد (قائمة صفقات مغلقة، عائد الشراء والاحتفاظ) لرمز واحد.

    يحاكي sa_bot._process_one_trading_date بالترتيب: رفع ← فحص ← مهلة ← قمة،
    ثم إشارة دخول على نفس الشمعة (لا إعادة في يوم الخروج).
    """
    if df is None or df.empty or "open_time" not in df.columns:
        return [], 0.0
    # تنظيف: Yahoo قد يعيد شموعًا ناقصة (NaN) — نُسقطها كي لا تُفسد EMA/القف/B&H
    df = (
        df.replace([np.inf, -np.inf], np.nan)
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index(drop=True)
    )
    if len(df) < settings.min_bars:
        return [], 0.0

    opens = df["open"].to_numpy(dtype="float64")
    highs = df["high"].to_numpy(dtype="float64")
    lows = df["low"].to_numpy(dtype="float64")
    closes = df["close"].to_numpy(dtype="float64")
    open_times = df["open_time"].to_numpy(dtype="int64")
    n = len(df)

    ema_series = ema(df["close"]).to_numpy(dtype="float64")
    open_time_to_idx = {int(t): k for k, t in enumerate(open_times)}

    trades: list[dict] = []
    open_trade: dict | None = None
    last_entry_open = 0
    last_exit_open = 0

    for i in range(n):
        # ── 1) متابعة الصفقة المفتوحة (الترتيب المُختبر) ──
        if open_trade is not None:
            # 1a) رفع الوقف من قمم الشموع السابقة (بلا انتحار نفس الشمعة)
            new_stop = next_stop(open_trade["stop"], open_trade["peak"], settings)
            raised = new_stop > open_trade["stop"]
            if raised:
                open_trade["stop"] = new_stop

            # 1b) فحص الوقف بالقيمة المرفوعة — تنفيذ الفجوة عند الفتح إن فاقته
            if lows[i] <= new_stop:
                fill = stop_fill(new_stop, opens[i])
                reason = "stop_trail" if new_stop > open_trade["pivot_stop"] else "stop_pivot"
                trades.append(_finish(open_trade, fill, reason, i, open_times, closes, settings))
                last_exit_open = int(open_times[i])
                open_trade = None
            else:
                # 1c) المهلة: ≥ max_hold شموع تداول (عدّ فهرس) = خروج عند الإغلاق
                bars_held = i - open_trade["entry_idx"]
                if is_timeout(bars_held, settings.max_hold):
                    trades.append(_finish(open_trade, closes[i], "timeout", i, open_times, closes, settings))
                    last_exit_open = int(open_times[i])
                    open_trade = None
                else:
                    # 1d) قمة الشمعة — تُرفع الوقف منها في اليوم التالي
                    open_trade["peak"] = max(open_trade["peak"], highs[i])

        # ── 2) إشارة دخول على هذه الشمعة المغلقة (لا حدّ للمراكز) ──
        if open_trade is None:
            sig = entry_signal_at(
                df, i, settings, float(ema_series[i]), open_time_to_idx,
                last_entry_open=last_entry_open, last_exit_open=last_exit_open,
            )
            if sig:
                open_trade = {
                    "entry_idx": i,
                    "entry_price": sig["entry_price"],
                    "stop": sig["stop_price"],
                    "pivot_stop": sig["stop_price"],
                    "peak": sig["peak_price"],
                    "entry_open": sig["bar_open_time"],
                }
                last_entry_open = sig["bar_open_time"]

    # (لا إغلاق افتراضي: الصفقات المفتوحة في نهاية النافذة تُترك معلّقة ولا تُحتسب ربحها)
    bh = (closes[-1] / closes[0] - 1.0) if (closes[0] > 0 and np.isfinite(closes[-1])) else 0.0
    return trades, bh


def _finish(
    trade: dict, exit_price: float, reason: str, exit_idx: int,
    open_times: np.ndarray, closes: np.ndarray, settings: SaSettings,
) -> dict:
    entry = float(trade["entry_price"])
    gross = float(exit_price) / entry - 1.0                 # كسر
    # عمولة بالواحد المئوي (0.15% لكل جهة) → نحوّلها إلى كسر لتطابق gross
    cost_frac = (2.0 * settings.commission_side_pct) / 100.0  # ذهابًا وإيابًا
    net = gross - cost_frac
    peak = float(trade["peak"])
    return {
        "entry_idx": trade["entry_idx"],
        "exit_idx": exit_idx,
        "entry_price": entry,
        "exit_price": float(exit_price),
        "peak_price": peak,
        "peak_gain_pct": (peak / entry - 1.0) * 100.0,
        "reason": reason,
        "gross_pct": gross * 100.0,
        "net_pct": net * 100.0,
        "bars_held": exit_idx - trade["entry_idx"],
        "entry_time": int(open_times[trade["entry_idx"]]),
        "exit_time": int(open_times[exit_idx]),
    }


# ───────────────────────────────────────────── جلب البيانات مع كاش
def load_universe(path: str = "config/sa_universe.txt") -> list[str]:
    codes: list[str] = []
    p = Path(path)
    if not p.exists():
        return codes
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        code = line.split(None, 1)[0].upper().removesuffix(".SR")
        if code.isdigit() and code not in codes:
            codes.append(code)
    return codes


def fetch_all(codes: list[str], data_range: str, refresh: bool, workers: int = 8) -> dict[str, pd.DataFrame]:
    cache_path = Path(f"cache_sa_{data_range}.json")
    cache: dict = {}
    if not refresh and cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    todo = [c for c in codes if c not in cache]
    print(f"Cache: {len(cache)} hit, {len(todo)} to fetch (range={data_range})")
    if todo:
        client = TadawulClient(timeout=20, max_workers=workers)
        fetched = client.get_daily_for_symbols(todo, range=data_range)
        for k, code in enumerate(todo, 1):
            df = fetched.get(code)
            if df is None or df.empty:
                cache[code] = None
            else:
                cache[code] = df.to_dict("list")
            if k % 50 == 0 or k == len(todo):
                print(f"  fetched {k}/{len(todo)}")
                cache_path.write_text(json.dumps(cache), encoding="utf-8")
        cache_path.write_text(json.dumps(cache), encoding="utf-8")

    out: dict[str, pd.DataFrame] = {}
    for code in codes:
        rec = cache.get(code)
        if rec:
            out[code] = pd.DataFrame(rec)
    return out


# ───────────────────────────────────────────── تجميع وتقرير
def _stats(trades: list[dict]) -> dict:
    """إحصاءات صفقات: فوز/خسارة، متوسط، وسيط، نسبة العائد/الخسارة، ومتوسط مدة.""" 
    if not trades:
        return {"n": 0, "win": 0.0, "avg": 0.0, "median": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0, "payoff": 0.0, "avg_bars": 0.0}
    nets = [t["net_pct"] for t in trades]
    wins = [t["net_pct"] for t in trades if t["net_pct"] > 0]
    losses = [t["net_pct"] for t in trades if t["net_pct"] <= 0]
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    payoff = (avg_win / abs(avg_loss)) if avg_loss < 0 else float("inf") if avg_win > 0 else 0.0
    return {
        "n": len(trades),
        "win": len(wins) / len(trades) * 100.0,
        "avg": float(np.mean(nets)),
        "median": float(np.median(nets)),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": payoff,
        "avg_bars": float(np.mean([t["bars_held"] for t in trades])),
    }


def _equity_maxdd(trades: list[dict]) -> float:
    """أقصى تراجع لمنحنى رموز واحد (صفقاته المتسلسلة) — معنى حقيقي لكل رمز."""
    eq = peak = 1.0
    maxdd = 0.0
    for t in trades:
        eq *= 1.0 + t["net_pct"] / 100.0
        peak = max(peak, eq)
        maxdd = max(maxdd, 1.0 - eq / peak)
    return maxdd


def main() -> None:
    # ويندوز: وحدة تحكم cp1256 لا تحمل رموزًا مثل ⚠️ — نعيد ضبط المخرجات إلى UTF-8
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="نظام القاع — تحقق ورقي على تداول")
    ap.add_argument("--range", default="5y", help="نافذة بيانات Yahoo (5y/10y/max)")
    ap.add_argument("--limit", type=int, default=0, help="عدد رموز كحد أقصى (0 = الكل)")
    ap.add_argument("--refresh", action="store_true", help="تجاهل الكاش وإعادة الجلب")
    args = ap.parse_args()

    codes = load_universe()
    if args.limit:
        codes = codes[: args.limit]
    print(f"Symbols: {len(codes)} | range={args.range}")
    t0 = time.time()
    data = fetch_all(codes, args.range, args.refresh)
    print(f"Fetched in {time.time() - t0:.1f}s | usable: {sum(1 for v in data.values() if not v.empty)}")

    settings = SaSettings()
    print(f"\n{settings.describe()} | عمولة {settings.commission_side_pct:g}% لكل جهة")

    per_symbol: dict[str, dict] = {}
    all_trades: list[dict] = []
    usable = 0
    for code, df in data.items():
        if df is None or df.empty or len(df) < settings.min_bars:
            continue
        usable += 1
        trades, bh = simulate_symbol(df, settings)
        st = _stats(trades)
        st["bh_pct"] = bh * 100.0
        # تجميع رمز واحد (صفقاته المتسلسلة) = منحنى معنى لهذا الرمز
        eq = 1.0
        for t in trades:
            eq *= 1.0 + t["net_pct"] / 100.0
        st["compounded"] = (eq - 1.0) * 100.0
        st["maxdd"] = _equity_maxdd(trades) * 100.0
        per_symbol[code] = {"stats": st, "trades": trades}
        all_trades.extend(trades)

    print(f"Usable symbols: {usable} | total closed trades: {len(all_trades)}")

    # ── مجمّع عبر كل الصفقات (إحصاء صفقة-بصفقة — لا تجميع رأس مال) ──
    pooled = _stats(all_trades)
    by_reason: dict[str, int] = {}
    for t in all_trades:
        by_reason[t["reason"]] = by_reason.get(t["reason"], 0) + 1

    # ── متوسط متساوٍ الأوزان لعوائد الرموز ومقارنتها بالشراء والاحتفاظ ──
    traded = [s["stats"] for s in per_symbol.values() if s["stats"]["n"] > 0]
    sym_comp = [s["compounded"] for s in traded]
    sym_bh = [s["bh_pct"] for s in traded]
    sym_maxdd = [s["maxdd"] for s in traded]
    mean_sym = float(np.mean(sym_comp)) if sym_comp else 0.0
    median_sym = float(np.median(sym_comp)) if sym_comp else 0.0
    mean_bh = float(np.mean(sym_bh)) if sym_bh else 0.0
    mean_maxdd = float(np.mean(sym_maxdd)) if sym_maxdd else 0.0
    beat_bh = sum(1 for s in traded if s["compounded"] > s["bh_pct"])
    profitable = sum(1 for c in sym_comp if c > 0)

    # ── تقسيم تطوير/تحقّق 60/40 حسب فهرس الشمعة (حراسة ضد الافتقاط) ──
    dev_trades: list[dict] = []
    val_trades: list[dict] = []
    for code, s in per_symbol.items():
        df = data[code]
        split = int(len(df) * 0.60)
        for t in s["trades"]:
            (dev_trades if t["entry_idx"] < split else val_trades).append(t)
    dev_st = _stats(dev_trades)
    val_st = _stats(val_trades)

    report = {
        "caveats": CAVEATS,
        "settings": settings.describe(),
        "commission_side_pct": settings.commission_side_pct,
        "usable_symbols": usable,
        "closed_trades": len(all_trades),
        "pooled_per_trade": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in pooled.items()},
        "by_exit_reason": by_reason,
        "mean_symbol_compounded_pct": round(mean_sym, 2),
        "median_symbol_compounded_pct": round(median_sym, 2),
        "mean_symbol_buyhold_pct": round(mean_bh, 2),
        "symbols_profitable": f"{profitable}/{len(sym_comp)}",
        "symbols_beating_buyhold": f"{beat_bh}/{len(sym_comp)}",
        "mean_symbol_maxdd_pct": round(mean_maxdd, 2),
        "dev_60_pct": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in dev_st.items()},
        "val_40_pct": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in val_st.items()},
        "sample_trades": all_trades[:15],
    }
    Path("backtest_sa_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── طباعة تقرير ──
    print("\n" + "=" * 70)
    print("⚠️  تحذيرات (اقرأها قبل الأرقام):")
    for c in CAVEATS:
        print(f"   • {c}")
    print("=" * 70)
    print(f"رموز صالحة: {usable} | صفقات مغلقة: {len(all_trades)}")
    print(f"أسباب الخروج: {by_reason}")
    print(f"\n— لكل صفقة (مجمّع عبر {len(all_trades)} صفقة) —")
    print(f"  فوز {pooled['win']:.1f}% | متوسط {pooled['avg']:+.2f}% | وسيط {pooled['median']:+.2f}% | "
          f"متوسط رابحة {pooled['avg_win']:+.2f}% / خاسرة {pooled['avg_loss']:+.2f}% | "
          f"عائد/خسارة {pooled['payoff']:.2f} | مدة {pooled['avg_bars']:.1f} شمعة")
    print(f"\n— لكل رمز (وزن متساوٍ، صفقاته المتسلسلة) —")
    print(f"  متوسط تجميعي {mean_sym:+.1f}% | وسيط {median_sym:+.1f}% | "
          f"شراء واحتفاظ {mean_bh:+.1f}%")
    print(f"  رموز رابحة: {profitable}/{len(sym_comp)} | تفوّق على B&H: {beat_bh}/{len(sym_comp)} | "
          f"متوسط أقصى تراجع {mean_maxdd:.1f}%")
    print(f"\n— تطوير 60% / تحقّق 40% (حسب الشمعة، حارس ضد الافتقاط) —")
    print(f"  تطوير: {dev_st['n']} صفقة، فوز {dev_st['win']:.1f}%، متوسط {dev_st['avg']:+.2f}%")
    print(f"  تحقّق: {val_st['n']} صفقة، فوز {val_st['win']:.1f}%، متوسط {val_st['avg']:+.2f}%")
    print("\n(لا يوجد تجميع رأس مال هنا عمديًا: لا حدّ للمراكز، وحجم كل صفقة قرار المستخدم.)")
    print("حُفظ التقرير: backtest_sa_results.json")


if __name__ == "__main__":
    main()
