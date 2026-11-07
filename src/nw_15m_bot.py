"""بناء بوت NW على فريم 15 دقيقة — **ملف جديد** يعيد استخدام نواة 1H كما هي.

لا يغيّر هذا الملف أي سطر في ``nw_bot.py`` أو ``src/nw_signals.py`` أو
``src/nw_messages.py``: يبني ``NWBot`` عبر ``build_nw_bot()`` (نفس الشروط
ونفس درع إعادة الرسم الذي أضافه المستخدم)، ثم يُبدّل ثلاثة أشياء فقط:

    1. ``bot.interval = "15m"``       ⇒ شموع 15 دقيقة في ``run_once()``.
    2. ``bot.state_path``/``store``   ⇒ ``data/state_nw15.json`` (معزول).
    3. ``bot.trade_log``              ⇒ ``data/trades_log_nw15.csv`` (معزول).
    4. ``_send_once`` مُغلَّف          ⇒ سطر «🕐 فريم 15 دقيقة (15M)» في الرسالة.

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
from src.nw_state import NWStateStore

INTERVAL_15M = "15m"
STATE_15M = "data/state_nw15.json"
TRADE_LOG_15M = "data/trades_log_nw15.csv"


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

    # التقرير اليومي الذي أضافه المستخدم عنوانه «NW 1H» ويقرأ حالة NW؛
    # إبقاؤه على 15m كان سيرسل تقريرًا ثانيًا بعنوان 1H وبيانات 15m.
    # نُبقيه يعمل كما كتبه المستخدم على نظام 1H فقط، بلا تعديل ملفه.
    bot.send_daily_report_if_due = lambda _now_ms: None  # type: ignore[method-assign]

    # 4) وسم الفريم داخل الرسالة: نُغلّف نقطة الإرسال الوحيدة بدل نسخ القالب.
    original_send_once = bot._send_once

    def _send_once_15m(state: dict[str, Any], key: str, text: str) -> bool:
        return original_send_once(state, key, with_15m_frame(text))

    bot._send_once = _send_once_15m  # type: ignore[method-assign]
    return bot
