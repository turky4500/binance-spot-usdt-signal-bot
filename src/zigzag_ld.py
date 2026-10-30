"""ZigZag++ [LD] (DevLucem) — تطبيق خوارزمية MT4 ZigZag: Depth / Deviation / Backstep.

المرجع: كود Pine الذي أرسله المستخدم (2026-10-30) — مؤشر `ZigZag++` للمطور DevLucem،
ويعتمد مكتبة `DevLucem/ZigLib/1` (غير منشورة) المبنية على خوارزمية ZigZag.mq4.

لماذا هذا الملف؟ النظام السابق كان يستخدم Depth فقط ويحوّله إلى قاعدة
«أدنى قاع في 12 شمعة»، فينتج قيعانًا مختلفة عن المؤشر الذي يراه المستخدم
(مثال موثّق: JPMBUSDT — المؤشر يرسم HL عند 342.76 بينما البوت أخذ 343.37).

التحقق من التطابق (2026-10-30، JPMBUSDT/1H، Depth=12/Dev=5/Back=2):
    قمة HH = 346.16 (2026-10-29 17:00 الرياض) ✓ مطابقة للشارت
    قاع HL = 342.76 (2026-10-29 22:00 الرياض) ✓ مطابقة للشارت

⚠️ ملاحظتان مهمتان:
  1) المؤشر افتراضيًا `repaint = true`: الوسم الأخير يتحرك مع السعر ولا يتأكد إلا
     بعد انعكاس الاتجاه (backstep + deviation). البوت يستخدم **المحاور المؤكدة فقط**
     (لا نظر للمستقبل) — لذلك قد تتأخر الإشارة شمعة/شمعتين عن الوسم المؤقت على الشارت.
  2) Deviation في MT4 بوحدات النقطة (point = tickSize). الافتراضي هنا 5 نقاط،
     وهو ما يطابق المؤشر على JPMBUSDT (tickSize = 0.01).
"""
from __future__ import annotations
import numpy as np


def mt4_zigzag(high, low, depth=12, deviation=5, backstep=2, point=0.01):
    high = np.asarray(high, dtype=float); low = np.asarray(low, dtype=float)
    n = len(high); dev = deviation * point
    if n < depth + backstep + 2:
        return []
    low_buf = np.zeros(n); high_buf = np.zeros(n)
    lastlow = lasthigh = 0.0

    for i in range(depth - 1, n):
        # ── القيعان
        val = low[i - depth + 1: i + 1].min()
        if lastlow != 0.0 and val == lastlow:
            val = 0.0
        else:
            lastlow = val
            if (low[i] - val) > dev:
                val = 0.0
            else:
                for back in range(1, backstep + 1):
                    j = i + back
                    if j < n and low[j] < val:
                        val = 0.0; break
        low_buf[i] = val
        # ── القمم
        val = high[i - depth + 1: i + 1].max()
        if lasthigh != 0.0 and val == lasthigh:
            val = 0.0
        else:
            lasthigh = val
            if (val - high[i]) > dev:
                val = 0.0
            else:
                for back in range(1, backstep + 1):
                    j = i + back
                    if j < n and high[j] > val:
                        val = 0.0; break
        high_buf[i] = val

    # ── تحويل العلامات إلى قمم/قيعان متبادلة (خط الزجزاج)
    pivots = []
    for i in range(n):
        for kind, buf in (("L", low_buf), ("H", high_buf)):
            if buf[i] == 0.0:
                continue
            price = float(buf[i])
            if pivots and pivots[-1]["kind"] == kind:
                prev = pivots[-1]
                better = price < prev["price"] if kind == "L" else price > prev["price"]
                if better:
                    pivots[-1] = {"kind": kind, "price": price, "bar": i}
            else:
                pivots.append({"kind": kind, "price": price, "bar": i})
    return pivots


def latest_confirmed_pivots(high, low, depth=12, deviation=5, backstep=2, point=0.01):
    """المحاور المؤكدة حتى آخر شمعة مغلقة (بلا نظر للمستقبل)."""
    return mt4_zigzag(high, low, depth=depth, deviation=deviation, backstep=backstep, point=point)


def provisional(high, low, pivots, n):
    """حالة الخط في الوضع الافتراضي للمؤشر (repaint=true): z1 آخر محور مؤكد، z2 الطرف الجاري.

    يعيد dict: direction, z1, z2, label (HH/HL/LH/LL) بنفس معادلة المؤشر:
        label = direction<0 ? (z2<prevLow ? 'LL':'HL') : (z2>prevHigh ? 'HH':'LH')
    """
    if not pivots:
        return None
    z1 = pivots[-1]
    direction = -1 if z1["kind"] == "H" else 1
    if direction < 0:
        seg = slice(z1["bar"] + 1, n)
        if seg.start >= n:          # لا شموع بعد آخر محور: الطرف الجاري = المحور نفسه
            z2 = dict(z1)
            prev_same = [p for p in pivots[:-1] if p["kind"] == "L"]
            label = ("LL" if (prev_same and z2["price"] < prev_same[-1]["price"]) else "HL")
            return {"direction": direction, "z1": z1, "z2": z2, "label": label}
        idx = int(np.argmin(low[seg]) + seg.start)
        z2 = {"kind": "L", "price": float(low[idx]), "bar": idx}
        prev_same = [p for p in pivots[:-1] if p["kind"] == "L"]
        label = ("LL" if (prev_same and z2["price"] < prev_same[-1]["price"]) else "HL")
    else:
        seg = slice(z1["bar"] + 1, n)
        if seg.start >= n:
            z2 = dict(z1)
            prev_same = [p for p in pivots[:-1] if p["kind"] == "H"]
            label = ("HH" if (prev_same and z2["price"] > prev_same[-1]["price"]) else "LH")
            return {"direction": direction, "z1": z1, "z2": z2, "label": label}
        idx = int(np.argmax(high[seg]) + seg.start)
        z2 = {"kind": "H", "price": float(high[idx]), "bar": idx}
        prev_same = [p for p in pivots[:-1] if p["kind"] == "H"]
        label = ("HH" if (prev_same and z2["price"] > prev_same[-1]["price"]) else "LH")
    return {"direction": direction, "z1": z1, "z2": z2, "label": label}
