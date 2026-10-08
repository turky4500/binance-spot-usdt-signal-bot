"""معالج تنبيهات TradingView (مؤشر قمم وقيعان مؤكدة | Binance Spot).

التصميم: صندوق بريد مشترك على GitHub (data/tv_inbox/) — كل تنبيه = ملف JSON منفصل.
البوت يسحبه كل دورة عبر `process_tv_alerts()` ثم يحذفه بعد المعالجة الناجحة.

القواعد (مُطابقة لمتطلبات المستخدم):
  - buy / strong_buy: فتح صفقة + رسالة دخول (هدف واحد من المؤشر + وقف + حكم شرعي).
  - take_profit / stop_loss / exit / sell: إغلاق الصفقة + رسالة إغلاق بنتيجة (ناجحة/خاسرة).
  - potential_bottom / potential_top: لا رسائل (مراقبة فقط).
  - لا رسالة ما دامت الصفقة مفتوحة على نفس العملة.

التنبيه يحوي: action, ticker, price, stop, target, mode, reason (للخروج فقط).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ALLOWED_ACTIONS = {"buy", "strong_buy", "sell", "take_profit", "stop_loss", "exit"}
WATCH_ONLY_ACTIONS = {"potential_bottom", "potential_top"}  # تُحفظ للتقارير بلا رسائل
INBOX_DIRNAME = "tv_inbox"
PROCESSED_DIRNAME = "tv_processed"


@dataclass(frozen=True)
class TVAlert:
    """تنبيه مُحلَّل من ملف JSON خام في صندوق البريد."""
    action: str
    ticker: str
    price: float | None
    stop: float | None
    target: float | None
    mode: str
    reason: str
    raw: dict[str, Any]
    source_file: Path

    @property
    def symbol(self) -> str:
        """يُحوّل 'BINANCE:ETHUSDT' أو 'ETHUSDT' أو 'ETHUSDT.P' إلى 'ETHUSDT'."""
        s = (self.ticker or "").upper()
        s = re.sub(r"^BINANCE:", "", s)
        s = s.split(".")[0]  # شيل لاحقة العقود الآجلة
        return s


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_alert(path: Path) -> TVAlert | None:
    """يقرأ ملف JSON واحد ويعيد TVAlert أو None إن كان غير صالح/مكرر/مُعالج سابقًا."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logging.getLogger("tv-alerts").warning("فشل قراءة %s: %s", path.name, exc)
        path.rename(path.with_suffix(".corrupt"))
        return None
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action") or "").strip().lower()
    if not action:
        return None
    if action in WATCH_ONLY_ACTIONS:
        return None  # مراقبة فقط، لا تعامل
    if action not in ALLOWED_ACTIONS:
        return None
    return TVAlert(
        action=action,
        ticker=str(raw.get("ticker") or ""),
        price=_to_float(raw.get("price")),
        stop=_to_float(raw.get("stop")),
        target=_to_float(raw.get("target")),
        mode=str(raw.get("mode") or "متوازن"),
        reason=str(raw.get("reason") or ""),
        raw=raw,
        source_file=path,
    )


def iter_inbox(data_dir: str | Path) -> Iterable[Path]:
    inbox = Path(data_dir) / INBOX_DIRNAME
    if not inbox.exists():
        return []
    # الترتيب الزمني يضمن الترتيب
    return sorted(p for p in inbox.glob("*.json") if p.is_file())


def archive_file(path: Path) -> None:
    """نقل ملف إلى tv_processed/ مع طابع زمني."""
    if not path.exists():
        return
    dest_dir = path.parent.parent / PROCESSED_DIRNAME
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    target = dest_dir / f"{ts}_{path.name}"
    try:
        path.rename(target)
    except Exception:
        # أفضل حذف من تعطّل المعالجة بالكامل
        try:
            path.unlink()
        except Exception:
            pass


def list_inbox(data_dir: str | Path) -> list[dict]:
    """عرض محتويات الصندوق (للتشخيص)."""
    items = []
    for p in iter_inbox(data_dir):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            raw = {"_error": "invalid json"}
        items.append({"file": p.name, "action": raw.get("action"), "ticker": raw.get("ticker")})
    return items
