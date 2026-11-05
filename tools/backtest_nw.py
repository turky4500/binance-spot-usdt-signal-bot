"""أداة قياس صادقة لنظام NW 1H — **ليست باك-تست ربح**.

تقيس على بيانات بينانس الحقيقية:
  1) عدد التسميات ▲/▼ التي تمرّ شروط المستخدم على النافذة الحالية.
  2) **انحراف إعادة الرسم** بمحاذاة صحيحة بالفهرس المطلق للشمعة:
     كم تسمية تظهر/تختفي بأثر رجعي عند إضافة شمعة واحدة، عبر مئات النوافذ.
  3) بقاء «التسمية الطازجة» (bars_ago=1) على نفس الشمعة بعد شمعة واحدة.
  4) مقدار تغيّر قيمة المنحنى لنفس الشمعة التاريخية بعد شمعة واحدة.

⚠️ تصحيح مسجّل (2026-11-06): رقم «34 تسمية تغيّرت بعد شمعة واحدة» الذي
قيل سابقًا كان ناتج **محاذاة خاطئة** (جُمع 1 إلى bars_ago بدل طرحه)؛ القياس
الصحيح أدناه: متوسط ≈0.94 تسمية/شمعة، والوسيط 0، والأقصى 6. خطأ المحاذاة
لا يُلغي التحذير: قيم المنحنى تتغيّر فعلًا، والتسميات الطازجة أقلها ثباتًا.

لا تحسب عائدًا ولا PF ولا تدّعي ربحًا. نظام NW يرسل توصيات فقط.

    python tools/backtest_nw.py --symbol BTCUSDT --limit 1000
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.nw_signals import NWSignalConfig, evaluate_symbol  # noqa: E402
from src.nw_strategy import annotate_repaint_signals, nw_curve  # noqa: E402

BASE = "https://data-api.binance.vision/api/v3/klines"
WINDOW = 500


def fetch(symbol: str, interval: str = "1h", limit: int = 1000):
    url = BASE + "?" + urllib.parse.urlencode({
        "symbol": symbol, "interval": interval, "limit": limit,
    })
    with urllib.request.urlopen(url, timeout=25) as r:
        raw = json.load(r)
    opens = np.array([float(k[1]) for k in raw])
    closes = np.array([float(k[4]) for k in raw])
    open_times = np.array([int(k[0]) for k in raw], dtype=np.int64)
    close_times = np.array([int(k[6]) for k in raw], dtype=np.int64)
    now_ms = int(raw[-1][0]) + 3_600_000
    mask = close_times < now_ms          # الشموع المغلقة فقط
    return opens[mask], closes[mask], open_times[mask]


def window_at(closes: np.ndarray, end: int, h: float = 8.0):
    """المنحنى والتسميات للنافذة المنتهية عند end (آخر شمعة = end-1)."""
    w = closes[end - WINDOW:end]
    y = nw_curve(w, h=h, window=WINDOW)
    labels = annotate_repaint_signals(y, w)
    # مفتاح محاذى: الفهرس المطلق للشمعة داخل السلسلة الكاملة
    absolute = {(end - 1 - s["bars_ago"], s["kind"]) for s in labels}
    return y, labels, absolute


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--checks", type=int, default=400, help="عدد النوافذ المتتالية")
    args = ap.parse_args()

    o, c, t = fetch(args.symbol, limit=args.limit)
    print(f"الرمز: {args.symbol} | شموع ساعية مغلقة: {len(c)} | آخر إغلاق: {c[-1]:.2f}")
    if len(c) < WINDOW + args.checks + 2:
        print(f"⚠️ بيانات غير كافية (يحتاج ≥ {WINDOW + args.checks + 2})")
        return

    # 1) شروط المستخدم على النافذة الحالية
    res = evaluate_symbol(o, c, t, NWSignalConfig())
    print("\n[1] شروط المستخدم على آخر نافذة")
    print(f"  ▲ مقبولة: {len(res['buy'])} | ▼ مقبولة: {len(res['sell'])}")
    print(f"  تسميات تاريخية مُهمَلة (حماية عدم الأثر الرجعي): {res['ignored_retro']}")
    print(f"  لون أحدث قطعة: {res['newest_color']}")

    # 2) انحراف إعادة الرسم بمحاذاة صحيحة
    ends = range(len(c) - args.checks, len(c) - 1)
    diffs = []
    drifts = []
    fresh = fresh_survived = 0
    for end in ends:
        ya, la, sa = window_at(c, end)
        yb, lb, sb = window_at(c, end + 1)
        diffs.append(len(sa ^ sb))
        drifts.append(abs(float(ya[0] - yb[1])))     # نفس الشمعة المطلقة
        f_labels = [s for s in la if s["bars_ago"] == 1]
        if f_labels:
            fresh += 1
            # نفس الشمعة تصبح bars_ago=2 في النافذة التالية
            if any((end - 2, s["kind"]) in sb for s in f_labels):
                fresh_survived += 1
    print(f"\n[2] إعادة الرسم عبر {len(diffs)} نافذة متتالية (محاذاة بالفهرس المطلق)")
    print(f"  تسميات ظهرت/اختفت بعد شمعة واحدة: متوسط {statistics.mean(diffs):.2f} | "
          f"وسيط {statistics.median(diffs):.0f} | أقصى {max(diffs)} | "
          f"صفر تغيير في {sum(d == 0 for d in diffs)} نافذة")
    print(f"  تغيّر قيمة المنحنى لنفس الشمعة: متوسط {statistics.mean(drifts):.2f} | "
          f"أقصى {max(drifts):.2f} ({max(drifts) / c[-1] * 100:.3f}% من السعر)")

    # 3) ثبات التسمية الطازجة (التي يعتمد عليها النظام)
    print("\n[3] التسمية الطازجة bars_ago=1 (التي يستخدمها النظام)")
    print(f"  ظهرت في {fresh} من {len(diffs)} ساعة | "
          f"بقيت على نفس الشمعة بعد شمعة واحدة: {fresh_survived}/{fresh}")
    print("  ⇒ التسميات الأحدث هي الأقل ثباتًا؛ لهذا يُرسل النظام تحذير إعادة الرسم"
          " ولا يُعتمد عليها كحقيقة نهائية.")

    # 4) تواتر الإشارات المقبولة فعليًا بشروط المستخدم
    buys = sells = 0
    cfg = NWSignalConfig()
    for end in ends:
        r = evaluate_symbol(o[:end], c[:end], t[:end], cfg)
        buys += len(r["buy"])
        sells += len(r["sell"])
    print(f"\n[4] تواتر الإشارات المقبولة على {args.symbol} عبر {len(diffs)} ساعة")
    print(f"  توصيات دخول: {buys} | إشارات خروج: {sells} "
          f"(≈ {buys / len(diffs) * 24:.2f} دخول/يوم لكل زوج)")
    print("  ⇒ رقم تواتر فقط، ليس عائدًا ولا تقييم أداء.")

    print("\n⚠️ الخلاصة: لا يجوز بناء باك-تست ربح على تسميات repaint=true.")


if __name__ == "__main__":
    main()
