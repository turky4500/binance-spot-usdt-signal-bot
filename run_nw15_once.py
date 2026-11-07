"""تشغيل دورة واحدة من نظام NW على فريم 15 دقيقة — بالتوازي مع نظام 1H.

لا يقرأ ولا يكتب أي ملف من ملفات 1H: الحالة ``data/state_nw15.json`` والدفتر
``data/trades_log_nw15.csv``. الشروط مستعارة حرفيًا من ``build_nw_bot()`` على
main، والفريم يُذكر في كل رسالة (دخول/تكرار/خروج).
"""

from __future__ import annotations

import logging
import os

from src.nw_15m_bot import build_nw_15m_bot
from src.telegram_client import install_redaction


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    install_redaction()
    build_nw_15m_bot().run_once()


if __name__ == "__main__":
    main()
