"""إرسال التقرير اليومي لنظام NW 4H فقط — **بلا فحص إشارات ولا دورة**.

دورات فريم 4h تقع عند 03:03/07:03/11:03/15:03/19:03/23:03 بتوقيت الرياض،
فلا تصادف ساعة 12:00 AM المطلوبة للتقرير. لذلك يُشغَّل هذا الملف من وركفلو
منفصل عند 00:10 AM الرياض (21:10 UTC): يستدعي ``send_daily_report_if_due``
مباشرة، ولا يستدعي ``run_once()`` ⇒ لا فحص عملات، لا زيادة ``cycles``، ولا
كتابة في دفتر الصفقات. الحالة وحدها تُحدَّث عند إرسال التقرير.
"""

from __future__ import annotations

import logging
import os

from src.nw_4h_bot import build_nw_4h_bot
from src.telegram_client import install_redaction


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    install_redaction()
    bot = build_nw_4h_bot()
    bot.send_daily_report_if_due(bot.client.get_server_time())


if __name__ == "__main__":
    main()
