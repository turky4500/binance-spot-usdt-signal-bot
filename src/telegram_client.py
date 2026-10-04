from __future__ import annotations

import requests


class TelegramClient:
    def __init__(self, token: str, chat_id: str, timeout: int = 20) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()

    def send_message(self, text: str) -> None:
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
