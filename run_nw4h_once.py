"""تشغيل دورة واحدة من نظام NW على فريم 4 ساعات — بالتوازي مع نظام 1H.

لا يقرأ ولا يكتب أي ملف من ملفات 1H أو 15M: الحالة
``data/state_nw4h.json`` والدفتر ``data/trades_log_nw4h.csv``. الشروط
مستعارة حرفيًا من ``build_nw_bot()`` على main، والفريم يُذكر في كل رسالة
(دخول/تكرار/خروج). يرسل توصيات فقط، لا ينفّذ صفقات.
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
    build_nw_4h_bot().run_once()


if __name__ == "__main__":
    main()
