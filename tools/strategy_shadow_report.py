"""مراجعة الوضع الظلّي في أي وقت: ملخص نظام «ارتداد الاتجاه اليومي» من الدفتر الحقيقي.

الاستخدام:
    python -m tools.strategy_shadow_report                     # كل الفترة
    python -m tools.strategy_shadow_report --days 7             # آخر 7 أيام
    python -m tools.strategy_shadow_report --symbols            # أفضل/أسوأ العملات فعلًا (بالأرقام الحيّة)
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategy_shadow import load_rows  # noqa: E402

DATA_DIR = Path("data")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--days", type=int, default=0)
    parser.add_argument("--symbols", action="store_true")
    args = parser.parse_args()

    rows = load_rows(args.data_dir)
    if not rows:
        print("لا توجد إشارات ظلّية بعد — الدفتر يُنشأ عند أول إشارة تحقق الشروط.")
        return

    if args.days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
        rows = [r for r in rows if str(r.get("entry_date_local") or "") >= cutoff]

    closed = [r for r in rows if r.get("outcome")]
    wins = [r for r in closed if r["outcome"] in ("target", "win")]
    losses = [r for r in closed if r["outcome"] == "loss"]
    nets = [float(r["net_return_pct"]) for r in closed if r.get("net_return_pct") not in ("", None)]
    durations = [float(r["duration_minutes"]) for r in closed if r.get("duration_minutes") not in ("", None)]

    rate = (len(wins) / len(closed) * 100.0) if closed else 0.0
    avg = (sum(nets) / len(nets)) if nets else 0.0
    total = sum(nets)
    print("=" * 74)
    print("النظام التجريبي (ظلّي): ارتداد الاتجاه اليومي")
    print("=" * 74)
    print(f"الإشارات           : {len(rows)}")
    print(f"مغلقة             : {len(closed)} (هدف {len(wins)} / وقف {len(losses)})")
    print(f"مفتوحة            : {len(rows) - len(closed)}")
    print(f"نسبة النجاح       : {rate:.1f}%")
    print(f"متوسط الصفقة      : {avg:+.3f}%")
    print(f"مجموع النسب       : {total:+.3f}% (مجموع حسابي بلا مركب)")
    if durations:
        print(f"متوسط المدة       : {sum(durations) / len(durations) / 60:.1f} ساعة")
    reasons = {}
    for r in closed:
        reasons[r.get("exit_reason", "?")] = reasons.get(r.get("exit_reason", "?"), 0) + 1
    if reasons:
        print("أسباب الخروج      : " + " • ".join(f"{k}={v}" for k, v in sorted(reasons.items())))

    print("\nللمقارنة: قياس المختبر كان +0.057%/صفقة (t=+2.52) والنصف الثاني +0.126%.")
    print("القاعدة: لا اعتماد قبل تراكم عيّنة كافية (≥100 صفقة مغلقة) وثبات الإشارة الموجبة.")

    if args.symbols:
        per: dict[str, list[float]] = {}
        for r in closed:
            try:
                per.setdefault(str(r["symbol"]), []).append(float(r["net_return_pct"]))
            except (TypeError, ValueError, KeyError):
                continue
        ranked = sorted(((s, sum(v) / len(v), len(v)) for s, v in per.items() if len(v) >= 2), key=lambda x: -x[1])
        print(f"\n{'=' * 74}\nأداء العملات فعليًا (≥2 صفقة مغلقة) — معلومة لا فلتر\n{'=' * 74}")
        for sym, avg_s, n in ranked[:10]:
            print(f"  ✅ {sym:<16} {avg_s:+.3f}% ({n} صفقة)")
        for sym, avg_s, n in ranked[-5:][::-1]:
            print(f"  ⚠️  {sym:<16} {avg_s:+.3f}% ({n} صفقة)")
        print("\nتذكير من الدراسة: ترتيب العملات السابق **لا يتنبأ** بالمستقبل (ارتباط ≈ 0) — راقب ولا تُفلتر.")


if __name__ == "__main__":
    main()
