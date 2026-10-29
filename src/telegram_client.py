from __future__ import annotations

import logging
import os

import requests

_MUTE_VALUES = ("1", "true", "yes", "on")


def is_muted() -> bool:
    """إسكات كل الرسائل مؤقتًا (TELEGRAM_MUTE=1).

    البوتات تستمر في العمل: تُحدَّث الحالة والدفاتر والتقارير الداخلية،
    ويُتجاوَز إرسال تيليجرام فقط. الإرجاع: احذف المتغيّر أو اضبطه على 0.
    """
    return os.getenv("TELEGRAM_MUTE", "0").strip().lower() in _MUTE_VALUES


class TelegramClient:
    def __init__(self, token: str, chat_id: str, timeout: int = 20) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()
        self.muted = is_muted()
        self.logger = logging.getLogger("telegram")

    def send_message(self, text: str) -> None:
        if self.muted:
            # إسكات مؤقت: لا اتصال بتليجرام ولا استثناء — بقية المنطق يكمل طبيعيًا.
            self.logger.info("TELEGRAM_MUTE=1 — رسالة لم تُرسل (طول النص %s حرفًا)", len(text))
            return
        response = self.session.post(
            f"{self.base_url}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram sendMessage failed: {payload}")
