"""تصنيف العملات العملي: أي عملة ينجح فيها نظام «ارتداد الاتجاه اليومي» فعلًا؟

المنهج (نفس منطق النزاهة في كل المشروع):
1) نرتّب العملات على **النصف الأول فقط**.
2) نتساءل: هل الربع الأعلى من الترتيب يتفوّق فعلًا في **النصف الثاني**؟
3) إن لم يتفوّق → الترتيب ضجيج ولا نعتمده (ونقولها بصراحة).

المخرج: `reports/coin_rank.md` + `reports/coin_rank.csv`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from .data import load_universe  # noqa: E402

SRC = Path("reports/quant_validate/qd_d1h1_pullback.csv.gz")


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"ملف الصفقات مفقود: {SRC} — شغّل run_quant_validate أولًا")
    trades = pd.read_csv(SRC)
    trades["is_win"] = trades["net_pct"] > 0
    mid = int(np.median(trades["entry_time"]))

    def per_symbol(sub: pd.DataFrame) -> pd.DataFrame:
        g = sub.groupby("symbol").agg(
            n=("net_pct", "size"), avg=("net_pct", "mean"), win=("is_win", "mean"),
            total=("net_pct", "sum"), bars=("bars_held", "mean"),
        )
        g["win"] *= 100.0
        return g

    train = per_symbol(trades[trades["entry_time"] <= mid])
    train = train[train["n"] >= 3]
    test = per_symbol(trades[trades["entry_time"] > mid])
    test = test[test["n"] >= 2]

    both = train.join(test, lsuffix="_train", rsuffix="_test", how="inner")
    print(f"عملات مشتركة: {len(both)}")

    # فحص النزاهة: هل ترتيب النصف الأول يتنبأ بالنصف الثاني؟
    q = both["avg_train"].quantile([0.25, 0.5, 0.75]).to_numpy()
    top = both[both["avg_train"] >= q[2]]
    mid_q = both[(both["avg_train"] >= q[1]) & (both["avg_train"] < q[2])]
    low = both[both["avg_train"] <= q[0]]
    rows = [
        ("الربع الأعلى (الأفضل في التدريب)", top),
        ("الربع الثاني", mid_q),
        ("الربع الأسفل (الأسوأ في التدريب)", low),
    ]
    print(f"\n{'=' * 92}\nهل الترتيب يصمد خارج العينة؟\n{'=' * 92}")
    print(f"{'المجموعة':<38}{'عملات':>7}{'متوسط التدريب':>16}{'متوسط الاختبار':>16}{'نسبة نجاح (اختبار)':>20}")
    for label, grp in rows:
        if grp.empty:
            continue
        corr_test = np.average(grp["avg_test"], weights=grp["n_test"])
        win_test = np.average(grp["win_test"], weights=grp["n_test"])
        print(f"{label:<38}{len(grp):>7}{grp['avg_train'].mean():>16.3f}{corr_test:>16.3f}{win_test:>20.1f}")
    corr = both["avg_train"].corr(both["avg_test"])
    print(f"\nارتباط ترتيب التدريب بترتيب الاختبار (Spearman): {both['avg_train'].corr(both['avg_test'], method='spearman'):+.3f} | بيرسون: {corr:+.3f}")

    # المخرج العملي: ترتيب كامل للمعلومات (بحذر)
    rank = both.sort_values("avg_train", ascending=False)
    out_dir = Path("reports")
    rank.to_csv(out_dir / "coin_rank.csv", float_format="%.4f")

    lines = [
        "# ترتيب العملات على نظام «ارتداد الاتجاه اليومي»",
        "",
        "**المصدر:** `reports/quant_validate/qd_d1h1_pullback.csv.gz` — 7,573 صفقة على 478 عملة (83 يومًا، فريم الساعة).",
        f"**التدريب:** حتى {pd.to_datetime(mid, unit='ms', utc=True).strftime('%Y-%m-%d')} • **الاختبار:** بعدها.",
        "",
        "## هل يمكن الاعتماد على الترتيب؟ (فحص النزاهة)",
        "",
        "| المجموعة (حسب ترتيب التدريب) | عملات | متوسط التدريب | **متوسط الاختبار** | نجاح الاختبار |",
        "|---|---:|---:|---:|---:|",
    ]
    for label, grp in rows:
        if grp.empty:
            continue
        corr_test = np.average(grp["avg_test"], weights=grp["n_test"])
        win_test = np.average(grp["win_test"], weights=grp["n_test"])
        lines.append(f"| {label} | {len(grp)} | {grp['avg_train'].mean():+.3f}% | **{corr_test:+.3f}%** | {win_test:.1f}% |")
    lines += [
        "",
        f"**الارتباط بين ترتيب النصفين (Spearman): {both['avg_train'].corr(both['avg_test'], method='spearman'):+.3f}**",
        "",
    ]

    top_test = np.average(top["avg_test"], weights=top["n_test"]) if len(top) else float("nan")
    low_test = np.average(low["avg_test"], weights=low["n_test"]) if len(low) else float("nan")
    spearman = both["avg_train"].corr(both["avg_test"], method="spearman")
    # الحكم: يجب أن يكون الارتباط موجبًا معقولًا **وأن** يتفوّق الربع الأعلى فعليًا بهامش غير تافه
    if spearman > 0.15 and top_test - low_test > 0.10:
        lines.append(f"→ الترتيب **يتنبأ جزئيًا** (ارتباط {spearman:+.2f} وفرق {top_test - low_test:+.3f}%): اختيار مجموعة عملات قد يفيد.")
    else:
        lines.append(
            f"→ ⚠️ **الترتيب لا يتنبأ** (ارتباط {spearman:+.2f} فقط، والفرق بين الربع الأعلى والأسفل "
            f"{top_test - low_test:+.3f}% = لا فرق عملي). "
            "**لا تستخدم هذا الترتيب كفلتر** — اختيار «العملات الرابحة سابقًا» يعطي نتائج كاختيار العشوائي. "
            "استخدم الجدولين أدناه كمعلومة عامة فقط."
        )

    lines += [
        "",
        "## أعلى 20 عملة (بالنصف الأول) — مع أدائها الفعلي في النصف الثاني",
        "",
        "| العملة | صفقات (تدريب) | متوسط تدريب | صفقات (اختبار) | **متوسط اختبار** | نجاح اختبار |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for sym, r in rank.head(20).iterrows():
        lines.append(f"| {sym} | {int(r['n_train'])} | {r['avg_train']:+.3f}% | {int(r['n_test'])} | **{r['avg_test']:+.3f}%** | {r['win_test']:.0f}% |")

    lines += [
        "",
        "## أسوأ 10 عملات (يفضَّل تجنّبها إن كانت الخصائص مستمرة)",
        "",
        "| العملة | متوسط تدريب | متوسط اختبار | نجاح اختبار |",
        "|---|---:|---:|---:|",
    ]
    for sym, r in rank.tail(10).iloc[::-1].iterrows():
        lines.append(f"| {sym} | {r['avg_train']:+.3f}% | {r['avg_test']:+.3f}% | {r['win_test']:.0f}% |")

    lines += [
        "",
        "## كيف تستخدم هذا عمليًا",
        "1. **لا تستخدمه كفلتر آلي في البوت** حتى يثبت التتبع الحي أن أداء العملة يستمر (قلب الظلّي سيقيس ذلك).",
        "2. كن واعيًا: عملة رابحة سابقًا قد تصبح خاسرة — والترتيب أعلاه يُظهر حجم هذا التشتت.",
        "3. الأهم عمليًا: **راجع أنواع العملات الرابحة** (سيولة، قطاع، حجم تداول) واستخدم ذلك كفرز يدوي.",
    ]

    (out_dir / "coin_rank.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nحُفظ: reports/coin_rank.md + reports/coin_rank.csv")


if __name__ == "__main__":
    main()
