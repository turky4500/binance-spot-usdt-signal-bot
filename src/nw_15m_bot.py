"""بناء بوت NW على فريم 15 دقيقة — **ملف جديد** يعيد استخدام نواة 1H كما هي.

لا يغيّر هذا الملف أي سطر في ``nw_bot.py`` أو ``src/nw_signals.py`` أو
``src/nw_messages.py``: يبني ``NWBot`` عبر ``build_nw_bot()`` (نفس الشروط
ونفس درع إعادة الرسم الذي أضافه المستخدم)، ثم يُبدّل ثلاثة أشياء فقط:

    1. ``bot.interval = "15m"``       ⇒ شموع 15 دقيقة في ``run_once()``.
    2. ``bot.state_path``/``store``   ⇒ ``data/state_nw15.json`` (معزول).
    3. ``bot.trade_log``              ⇒ ``data/trades_log_nw15.csv`` (معزول).
    4. ``_send_once`` مُغلَّف          ⇒ سطر «🕐 فريم 15 دقيقة (15M)» في الرسالة.
    5. ``send_daily_report_if_due``  ⇒ تقرير يومي مستقل بنفس صيغة/توقيت 1H،
       بعنوان «NW 15M» ومفتاح حالة ``daily_report_nw15`` (معزول عن 1H).

كل شروط الإشارة تبقى حرفيًا شروط 1H: مؤشر Envelope [LuxAlgo]، h=8، mult=3،
src=close، نافذة 500، sae/499*3، ``bars_ago==1`` فقط، شمعة تالية خضراء ⇒
دخول، حمراء ⇒ إلغاء نهائي، ▼ ⇒ خروج بسعري الدخول/الخروج والنسبة الصافية،
تكرار ▲ ⇒ «إشارة دخول ثانية»، كل أزواج USDT بجميع أحكامها، بلا بوابة BTC
وبلا أهداف/وقف وبلا أي فلتر إضافي.
"""

from __future__ import annotations

import os
from typing import Any

from nw_bot import NWBot, build_nw_bot
from src.nw_15m_messages import with_15m_frame
from src.nw_daily_report import send_daily_report_if_due as _daily_report
from src.nw_state import NWStateStore

INTERVAL_15M = "15m"
STATE_15M = "data/state_nw15.json"
TRADE_LOG_15M = "data/trades_log_nw15.csv"
FRAME_LABEL_15M = "NW 15M"
REPORT_STATE_KEY_15M = "daily_report_nw15"


def build_nw_15m_bot() -> NWBot:
    """نفس بوت 1H مع فريم 15m وحالة/دفتر/قالب رسالة مستقلين."""
    os.environ.setdefault("NW_STATE_FILE", STATE_15M)
    os.environ.setdefault("NW_TRADE_LOG", TRADE_LOG_15M)
    state_path = os.environ["NW_STATE_FILE"]
    trade_log = os.environ["NW_TRADE_LOG"]

    bot = build_nw_bot()

    # 1) الفريم: run_once() يمرّر self.interval إلى get_klines_for_symbols.
    bot.interval = INTERVAL_15M

    # 2+3) عزل الحالة والدفتر عن 1H، حتى لو لم تُضبط متغيّرات البيئة.
    bot.state_path = state_path
    bot.store = NWStateStore(state_path)
    bot.trade_log = trade_log

    # تقرير يومي مستقل لفريم 15M: **نفس** صيغة تقرير 1H و**نفس** توقيته
    # (12:00 AM الرياض، اليوم السابق)، بعنوان «NW 15M» وحالة/دفتر معزولين،
    # ومفتاح حالة مستقل ``daily_report_nw15`` ⇒ لا يتشارك سجل منع التراكم
    # مع 1H ولا يمنع تقريره. لا تعديل على nw_bot.py ولا على شروط الإشارات.
    def _send_15m_daily_report(now_ms: int) -> None:
        _daily_report(bot, now_ms,
                      frame_label=FRAME_LABEL_15M,
                      state_key=REPORT_STATE_KEY_15M)

    bot.send_daily_report_if_due = _send_15m_daily_report  # type: ignore[method-assign]

    # 4) وسم الفريم داخل الرسالة: نُغلّف نقطة الإرسال الوحيدة بدل نسخ القالب.
    original_send_once = bot._send_once

    def _send_once_15m(state: dict[str, Any], key: str, text: str) -> bool:
        return original_send_once(state, key, with_15m_frame(text))

    bot._send_once = _send_once_15m  # type: ignore[method-assign]
    return bot
