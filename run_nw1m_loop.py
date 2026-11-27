"""NW (Nadaraya-Watson Envelope) على HBARUSDT فريم 1 دقيقة، حلقة كل دقيقة لين 11 مساء الرياض.

ملف جديد بالتوازي مع البوتات الحالية: ما يعدّل أي ملف موجود. يعيد استخدام
build_nw_bot() كما هو (نفس الشروط والرسائل والحكم الشرعي)، ويبدّل فقط:
الفريم = 1m، الكون = HBARUSDT فقط، سطر الفريم في الرسالة، وملف حالة منفصل.
ما يعمل أي commit: الحالة تبقى داخل الـ job فقط.
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
from src.nw_state import NWStateStore
from src.telegram_client import install_redaction

RIYADH = ZoneInfo("Asia/Riyadh")
SYMBOL = os.getenv("NW1M_SYMBOL", "HBARUSDT")
END_HOUR = int(os.getenv("NW1M_END_HOUR", "23"))   # 23:00 بتوقيت الرياض
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


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    install_redaction()
    now = datetime.now(RIYADH)
    end = now.replace(hour=END_HOUR, minute=0, second=0, microsecond=0)
    if now >= end:
        LOG.info("الوقت بعد %02d:00 الرياض، لا شي نسويه.", END_HOUR)
        return
    bot = build()
    n = 0
    while datetime.now(RIYADH) < end:
        n += 1
        try:
            bot.run_once(universe=[SYMBOL])
        except Exception as e:  # نكمل الحلقة حتى لو فشلت دورة
            LOG.error("فشلت الدورة %d: %s", n, e)
        # الدورة التالية بعد إغلاق الشمعة بـ 3 ثواني تقريبًا
        time.sleep(max(5, 63 - (time.time() % 60)))
    LOG.info("انتهت المراقبة عند %02d:00 الرياض بعد %d دورة.", END_HOUR, n)


if __name__ == "__main__":
    main()
