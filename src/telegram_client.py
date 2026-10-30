from __future__ import annotations

import logging
import os
import time

import requests

_MUTE_VALUES = ("1", "true", "yes", "on")
# ── حماية من إغراق تيليجرام (2026-10-30) ──
# بعد حادثة 246 رسالة في ثانية واحدة: تيليجرام يسمح ~1 رسالة/ثانية و~20 رسالة/دقيقة
# للمحادثة الواحدة، ويُرجع 429 مع retry_after عند التجاوز. هنا: فاصل زمني أدنى
# بين الرسائل + احترام retry_after وإعادة محاولة (بدل فقدان الرسالة بصمت).
_MIN_INTERVAL_ENV = "TELEGRAM_MIN_INTERVAL_SECONDS"
_MAX_ATTEMPTS = 3


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
        try:
            self.min_interval = float(os.getenv(_MIN_INTERVAL_ENV, "1.05"))
        except (TypeError, ValueError):
            self.min_interval = 1.05
        self._last_sent_at = 0.0

    def _wait_turn(self) -> None:
        """فاصل زمني أدنى بين رسالتين (0 = معطّل)."""
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_sent_at
        if 0 < elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_sent_at = time.monotonic()

    @staticmethod
    def _retry_after(response, attempt: int) -> float:
        """مدة الانتظار التي يطلبها تيليجرام عند 429 (retry_after)."""
        try:
            payload = response.json()
            value = float((payload.get("parameters") or {}).get("retry_after", 0) or 0)
            if value > 0:
                return min(value, 30.0)
        except Exception:
            pass
        return min(2.0 * attempt, 15.0)

    def send_message(self, text: str) -> None:
        if self.muted:
            # إسكات مؤقت: لا اتصال بتليجرام ولا استثناء — بقية المنطق يكمل طبيعيًا.
            self.logger.info("TELEGRAM_MUTE=1 — رسالة لم تُرسل (طول النص %s حرفًا)", len(text))
            return
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            self._wait_turn()
            response = self.session.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
                timeout=self.timeout,
            )
            status = int(getattr(response, "status_code", 200) or 200)
            if status == 429 and attempt < _MAX_ATTEMPTS:
                delay = self._retry_after(response, attempt)
                self.logger.warning(
                    "تيليجرام 429 (تجاوز الحد) — انتظار %.1f ثانية ثم إعادة المحاولة %s/%s",
                    delay, attempt, _MAX_ATTEMPTS,
                )
                time.sleep(delay)
                continue
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(f"Telegram sendMessage failed: {payload}")
            return
