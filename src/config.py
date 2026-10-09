from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(slots=True)
class AppConfig:
    telegram_bot_token: str
    telegram_chat_id: str
    timezone_name: str = os.getenv("TIMEZONE", "Asia/Riyadh")
    binance_base_url: str = os.getenv("BINANCE_BASE_URL", "https://data-api.binance.vision")
    quote_asset: str = os.getenv("BINANCE_QUOTE_ASSET", "USDT")
    interval: str = os.getenv("BINANCE_INTERVAL", "1h")
    poll_seconds: int = int(os.getenv("POLL_SECONDS", "60"))
    # 499: أقصى عدد شموع يبقى فيه وزن طلب Binance لـ klines = 2 (500+ يصبح 5).
    # وهو مطلوب لأن sma(atr(200),200) في Target Trend لا تصحح قبل ~400 شمعة.
    kline_limit: int = int(os.getenv("KLINE_LIMIT", "499"))
    request_timeout: int = int(os.getenv("REQUEST_TIMEOUT", "20"))
    max_workers: int = int(os.getenv("MAX_WORKERS", "10"))
    halal_refresh_hours: int = int(os.getenv("HALAL_REFRESH_HOURS", "6"))
    state_file: str = os.getenv("STATE_FILE", "data/state.json")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    @classmethod
    def from_env(cls) -> "AppConfig":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token:
            raise ValueError("Missing TELEGRAM_BOT_TOKEN environment variable.")
        if not chat_id:
            raise ValueError("Missing TELEGRAM_CHAT_ID environment variable.")
        return cls(
            telegram_bot_token=token,
            telegram_chat_id=chat_id,
        )
