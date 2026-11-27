"""NW (Nadaraya-Watson Envelope) على HBARUSDT فريم 1 دقيقة، حلقة كل دقيقة بلا موعد نهاية.

ملف جديد بالتوازي مع البوتات الحالية: ما يعدّل أي ملف موجود. يعيد استخدام
build_nw_bot() كما هو (نفس الشروط والرسائل والحكم الشرعي)، ويبدّل فقط:
الفريم = 1m، الكون = HBARUSDT فقط، سطر الفريم في الرسالة، وملف حالة منفصل.
الحلقة تشتغل NW1M_RUN_MINUTES دقيقة ثم تنتهي، والـ workflow يعيد تشغيل نفسه
(حد GitHub للمهمة الواحدة 6 ساعات). الحالة تنتقل بين التشغيلات عبر actions/cache،
بدون أي commit على المستودع.
NW1M_SEED_ENTRY = "سعر_الدخول,زمن_شمعة_التأكيد_ms,زمن_شمعة_الإشارة_ms": يزرع مركزًا
مفتوحًا فقط إذا الحالة ما فيها مركز (لتصل إشارة الخروج لصفقة بدأت قبل التشغيل).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("NW_STATE_FILE", "data/state_nw1m.json")
os.environ.setdefault("NW_TRADE_LOG", "data/trades_log_nw1m.csv")

from nw_bot import build_nw_bot
from src.nw_state import NWStateStore, symbol_state
from src.telegram_client import install_redaction

RIYADH = ZoneInfo("Asia/Riyadh")
SYMBOL = os.getenv("NW1M_SYMBOL", "HBARUSDT")
RUN_MINUTES = float(os.getenv("NW1M_RUN_MINUTES", "300"))
FRAME_1H = "🕐 فريم الساعة (1H) · Nadaraya"
FRAME_1M = "🕐 فريم دقيقة (1M) · Nadaraya"
LOG = logging.getLogger("nw1m")


def build():
    bot = build_nw_bot()
    bot.interval = "1m"
    bot.state_path = os.environ["NW_STATE_FILE"]
    bot.store = NWStateStore(bot.state_path)
    bot.trade_log = os.environ["NW_TRADE_LOG"]
    bot.send_daily_report_if_due = lambda now_ms: None   # لا تقرير يومي
    original = bot._send_once

    def send_once(state, key, text):
        return original(state, key, text.replace(FRAME_1H, FRAME_1M, 1))

    bot._send_once = send_once
    return bot


def seed_position(bot) -> None:
    raw = os.getenv("NW1M_SEED_ENTRY", "").strip()
    if not raw:
        return
    try:
        price, confirm_ms, signal_ms = raw.split(",")
        state = bot.store.load()
        item = symbol_state(state, SYMBOL)
        if item.get("position") is not None:
            return
        item["position"] = {"entry_price": float(price), "entry_time": int(confirm_ms),
                            "signal_bar_time": int(signal_ms), "signal_price": float(price),
                            "entry_count": 1}
        item["entry_count"] = 1
        item["last_buy_bar"] = int(signal_ms)
        item["last_entry_price"] = float(price)
        state["buy_history"].setdefault(SYMBOL, []).append(int(signal_ms))
        bot.store.save(state)
        LOG.info("تم زرع مركز مفتوح من الحالة الخارجية: دخول %s", price)
    except Exception as e:
        LOG.error("فشل زرع المركز: %s", e)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    install_redaction()
    bot = build()
    seed_position(bot)
    deadline = time.time() + RUN_MINUTES * 60
    n = 0
    while time.time() < deadline:
        n += 1
        try:
            bot.run_once(universe=[SYMBOL])
        except Exception as e:  # نكمل الحلقة حتى لو فشلت دورة
            LOG.error("فشلت الدورة %d: %s", n, e)
        # الدورة التالية بعد إغلاق الشمعة بـ 3 ثواني تقريبًا
        time.sleep(max(5, 63 - (time.time() % 60)))
    LOG.info("انتهت هذه الجولة (%s) بعد %d دورة؛ التشغيل التالي يبدأ تلقائيًا.",
             datetime.now(RIYADH).strftime("%H:%M"), n)


if __name__ == "__main__":
    main()
