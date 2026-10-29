from __future__ import annotations

import logging

from src.config import AppConfig
from zigzag_bot import build_zigzag_bot


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, AppConfig.from_env().log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    bot = build_zigzag_bot()
    bot.run_once()


if __name__ == "__main__":
    main()
