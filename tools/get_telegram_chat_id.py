from __future__ import annotations

import json
import os

import requests


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN first.")

    response = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise SystemExit(f"Telegram API error: {payload}")

    results = payload.get("result", [])
    if not results:
        print("No updates yet. Send /start to the bot in private, or send a message in the target group/channel first.")
        return

    chats: dict[str, dict] = {}
    for item in results:
        message = item.get("message") or item.get("channel_post") or item.get("edited_message") or item.get("edited_channel_post")
        if not message:
            continue
        chat = message.get("chat")
        if not chat:
            continue
        chats[str(chat["id"])] = {
            "id": chat["id"],
            "type": chat.get("type"),
            "title": chat.get("title") or chat.get("username") or chat.get("first_name"),
        }

    print(json.dumps(list(chats.values()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
