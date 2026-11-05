"""تشغيل دورة واحدة من نظام NW 1H (مثل GitHub Actions).

يقرأ TELEGRAM_MUTE من البيئة: عند 1 يعمل البوت ويحدّث حالته بلا إرسال
(مفيد للتجربة المحلية)، وعند 0 تُرسل رسائل تيليجرام الحقيقية.
"""

from __future__ import annotations

import logging
import os

from nw_bot import build_nw_bot
from src.telegram_client import install_redaction


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    install_redaction()
    build_nw_bot().run_once()


if __name__ == "__main__":
    main()
