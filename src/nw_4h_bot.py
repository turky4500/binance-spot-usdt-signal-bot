"""بناء بوت NW على فريم 4 ساعات — **ملف جديد** يعيد استخدام نواة 1H كما هي.

طلب المستخدم (2026-11-09): «بدلًا من فريم 15 اعتمد فريم 4 ساعات بنفس
الشروط وشغل التنبيهات»، ثم أوضح: «أنا أريده نفس نظام فريم 1 ساعة يرسل
توصيات فقط اختلاف الفريم»، و«فريم 1 ساعة لا يُمس، هذا النظام الجديد يعمل
بالتوازي»، و«نفس تقرير 1 ساعة في نهاية اليوم».

لا يغيّر هذا الملف أي سطر في ``nw_bot.py`` أو ``src/nw_signals.py`` أو
``src/nw_messages.py``: يبني ``NWBot`` عبر ``build_nw_bot()`` (نفس الشروط
ونفس درع إعادة الرسم ونفس مدة الصفقة)، ثم يُبدّل فقط:

    1. ``bot.interval = "4h"``       ⇒ شموع 4 ساعات في ``run_once()``.
    2. ``bot.state_path``/``store``   ⇒ ``data/state_nw4h.json`` (معزول).
    3. ``bot.trade_log``              ⇒ ``data/trades_log_nw4h.csv`` (معزول).
    4. ``_send_once`` مُغلَّف          ⇒ سطر «🕐 فريم 4 ساعات (4H)» في الرسالة.
    5. ``send_daily_report_if_due``  ⇒ تقرير يومي مستقل بنفس صيغة/توقيت 1H،
       بعنوان «NW 4H» ومفتاح حالة ``daily_report_nw4h``، و«إجمالي التوصيات
       المرسلة لليوم» محسوبًا بطول شمعة 4h = 14,400,000 ms.

كل شروط الإشارة تبقى حرفيًا شروط 1H: مؤشر Envelope [LuxAlgo]، h=8، mult=3،
src=close، نافذة 500، sae/499*3، ``bars_ago==1`` فقط، شمعة تالية خضراء ⇒
دخول، حمراء ⇒ إلغاء نهائي، ▼ ⇒ خروج بسعري الدخول/الخروج والنسبة الصافية،
تكرار ▲ ⇒ «إشارة دخول ثانية»، كل أزواج USDT بجميع أحكامها، بلا بوابة BTC
وبلا أهداف/وقف وبلا أي فلتر إضافي. النظام يرسل توصيات فقط لا ينفّذ صفقات.
"""

from __future__ import annotations

import os
from typing import Any

from nw_bot import NWBot, build_nw_bot
from src.nw_4h_messages import with_4h_frame
from src.nw_daily_report import send_daily_report_if_due as _daily_report
from src.nw_state import NWStateStore

INTERVAL_4H = "4h"
INTERVAL_MS_4H = 14_400_000     # طول شمعة 4h لحساب زمن رسالة الدخول في التقرير
STATE_4H = "data/state_nw4h.json"
TRADE_LOG_4H = "data/trades_log_nw4h.csv"
FRAME_LABEL_4H = "NW 4H"
REPORT_STATE_KEY_4H = "daily_report_nw4h"


def build_nw_4h_bot() -> NWBot:
    """نفس بوت 1H مع فريم 4h وحالة/دفتر/قالب رسالة مستقلين."""
    os.environ.setdefault("NW_STATE_FILE", STATE_4H)
    os.environ.setdefault("NW_TRADE_LOG", TRADE_LOG_4H)
    state_path = os.environ["NW_STATE_FILE"]
    trade_log = os.environ["NW_TRADE_LOG"]

    bot = build_nw_bot()

    # 1) الفريم: run_once() يمرّر self.interval إلى get_klines_for_symbols.
    bot.interval = INTERVAL_4H

    # 2+3) عزل الحالة والدفتر عن 1H و15M، حتى لو لم تُضبط متغيّرات البيئة.
    bot.state_path = state_path
    bot.store = NWStateStore(state_path)
    bot.trade_log = trade_log

    # تقرير يومي مستقل لفريم 4H: **نفس** صيغة تقرير 1H و**نفس** توقيته
    # (12:00 AM الرياض، اليوم السابق)، بعنوان «NW 4H» وحالة/دفتر معزولين،
    # ومفتاح حالة مستقل ``daily_report_nw4h``. عدد «التوصيات المرسلة لليوم»
    # يُحسب بطول شمعة 4h لا 1h ولا 15m. لا تعديل على nw_bot.py ولا على
    # شروط الإشارات.
    def _send_4h_daily_report(now_ms: int) -> None:
        _daily_report(bot, now_ms,
                      frame_label=FRAME_LABEL_4H,
                      state_key=REPORT_STATE_KEY_4H,
                      interval_ms=INTERVAL_MS_4H)

    bot.send_daily_report_if_due = _send_4h_daily_report  # type: ignore[method-assign]

    # 4) وسم الفريم داخل الرسالة: نُغلّف نقطة الإرسال الوحيدة بدل نسخ القالب.
    original_send_once = bot._send_once

    def _send_once_4h(state: dict[str, Any], key: str, text: str) -> bool:
        return original_send_once(state, key, with_4h_frame(text))

    bot._send_once = _send_once_4h  # type: ignore[method-assign]
    return bot
