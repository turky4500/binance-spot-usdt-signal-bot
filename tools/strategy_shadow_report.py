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

from src.strategy_shadow import FOCUS_HOURS, GOOD_HOURS, load_rows  # noqa: E402

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

    def lane_stats(subset: list[dict]) -> dict:
        closed = [r for r in subset if r.get("outcome")]
        wins = [r for r in closed if r["outcome"] in ("target", "win")]
        losses = [r for r in closed if r["outcome"] == "loss"]
        nets = [float(r["net_return_pct"]) for r in closed if r.get("net_return_pct") not in ("", None)]
        durations = [float(r["duration_minutes"]) for r in closed if r.get("duration_minutes") not in ("", None)]
        return {
            "total": len(subset), "closed": len(closed), "wins": len(wins), "losses": len(losses),
            "open": len(subset) - len(closed),
            "rate": (len(wins) / len(closed) * 100.0) if closed else 0.0,
            "avg": (sum(nets) / len(nets)) if nets else None,
            "sum": sum(nets) if nets else 0.0,
            "avg_hours": (sum(durations) / len(durations) / 60.0) if durations else None,
            "reasons": {r.get("exit_reason", "?"): sum(1 for x in closed if x.get("exit_reason") == r.get("exit_reason")) for r in closed},
        }

    def show(label: str, st: dict) -> None:
        print(f"\n{'─' * 74}\n{label}\n{'─' * 74}")
        print(f"الإشارات: {st['total']} | مغلقة: {st['closed']} (هدف {st['wins']} / وقف {st['losses']}) | مفتوحة: {st['open']}")
        print(f"نسبة النجاح: {st['rate']:.1f}%" + (f" | متوسط الصفقة: {st['avg']:+.3f}% | مجموع: {st['sum']:+.2f}%" if st["avg"] is not None else " | (لا صفقات مغلقة بعد)"))
        if st["avg_hours"] is not None:
            print(f"متوسط المدة: {st['avg_hours']:.1f} ساعة")
        if st["reasons"]:
            print("أسباب الخروج: " + " • ".join(f"{k}={v}" for k, v in sorted(st["reasons"].items())))
        # حكم القاعدة المُسجَّلة مسبقًا
        if st["closed"] >= 100 and st["avg"] is not None:
            if st["avg"] >= 0.05 and st["rate"] >= 52:
                print("🟢 الحكم بحسب القاعدة المُسجَّلة: مؤهَّل للترقية (≥100 مغلقة، متوسط ≥ +0.05%، نجاح ≥52%)")
            else:
                print("🔴 الحكم بحسب القاعدة المُسجَّلة: غير مؤهَّل للترقية")
        elif st["closed"] >= 50 and st["avg"] is not None and st["avg"] <= -0.15:
            print("⚠️ إشارة إنذار مبكرة: متوسط ≤ −0.15% بعد 50+ صفقة — راجع القاعدة")
        else:
            print(f"⏳ قيد القياس: {st['closed']}/100 صفقة مغلقة — لا حكم قبل اكتمال العيّنة")

    pull_rows = [r for r in rows if str(r.get("lane") or "pullback") != "ibs"]
    ibs_rows = [r for r in rows if str(r.get("lane") or "pullback") == "ibs"]

    print("=" * 74)
    print("النظام التجريبي (ظلّي) — ثلاثة مسارات")
    print("=" * 74)
    show(f"أ) ارتداد — شامل ساعات {sorted(GOOD_HOURS)}", lane_stats(pull_rows))
    show(f"ب) ارتداد — مركّز ساعات {sorted(FOCUS_HOURS)}", lane_stats([r for r in pull_rows if str(r.get("in_focus_hours")) == "1"]))
    if ibs_rows:
        show("ج) IBS<0.2 + ساعات مركّزة (من استراتيجيات يوتيوب)", lane_stats(ibs_rows))
    else:
        print("\n(ج) مسار IBS: لا إشارات بعد — يُسجَّل عند أول تحقق (ساعات 0/5/6 فقط).")

    print("\nللمقارنة (المختبر): ارتداد شامل +0.057% • ارتداد مركّز +0.212% (t=+2.52) • IBS مركّز +0.202% (t=+10.58، اتساع 66% من العملات)")
    print("القاعدة الكاملة: reports/shadow_watch_plan.md")


    if args.symbols:
        per: dict[str, list[float]] = {}
        for r in [x for x in rows if x.get("outcome")]:
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
