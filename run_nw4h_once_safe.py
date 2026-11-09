"""غلاف آمن لتشغيل دورة NW 4H داخل GitHub Actions.

لا يعدّل أي ملف من ملفات المستخدم: يبني بوت 4H عبر ``build_nw_4h_bot()``
ثم يضيف حمايتين فقط حول الكائن الجاهز، تمامًا كما في غلاف 1H:

1. ``_send_once`` يُحفظ بعد كل رسالة ناجحة ⇒ إذا انقطع الاتصال في منتصف
   الدورة لا تُفقد مفاتيح الرسائل التي أُرسلت بالفعل، فلا تُعاد في الدورة التالية.
2. ``nw_bot.append_trade`` يصبح غير مكرّر ⇒ إعادة المحاولة لا تضيف صفًا
   مزدوجًا في ``data/trades_log_nw4h.csv``.

الوركفلو يعيد تشغيل هذا الغلاف حتى 3 مرات عند أي فشل شبكي عابر؛ بفضل
الحفظ التدريجي، المحاولة التالية تكمل من حيث توقفت بلا تكرار رسالة ولا صف.
الحالة ``data/state_nw4h.json`` والدفتر ``data/trades_log_nw4h.csv`` يظلان
معزولين تمامًا عن 1H و15M.
"""

from __future__ import annotations

import csv
import logging
import os
from pathlib import Path

import nw_bot
from src.nw_4h_bot import build_nw_4h_bot
from src.nw_journal import append_trade as _original_append_trade
from src.telegram_client import install_redaction


def _row_exists(path: str, row: dict) -> bool:
    p = Path(path)
    if not p.exists():
        return False
    with p.open(encoding="utf-8-sig", newline="") as f:
        for existing in csv.DictReader(f):
            if (
                str(existing.get("symbol")) == str(row["symbol"])
                and str(existing.get("entry_time")) == str(row["entry_time"])
                and str(existing.get("exit_time")) == str(row["exit_time"])
            ):
                return True
    return False


def _append_trade_once(path: str, row: dict) -> None:
    if _row_exists(path, row):
        return
    _original_append_trade(path, row)


# ``nw_bot.py`` يستورد الدالة على مستوى الوحدة ⇒ نستبدل المرجع فقط،
# دون أي تعديل على ملف المستخدم.
nw_bot.append_trade = _append_trade_once  # type: ignore[assignment]


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    install_redaction()
    bot = build_nw_4h_bot()

    original_send_once = bot._send_once

    def _send_once_and_save(state: dict, key: str, text: str) -> bool:
        sent = original_send_once(state, key, text)
        if sent:
            # حفظ فوري: أي انقطاع لاحق لا يُفقد مفتاح هذه الرسالة.
            bot.store.save(state)
        return sent

    bot._send_once = _send_once_and_save  # type: ignore[method-assign]
    bot.run_once()


if __name__ == "__main__":
    main()
