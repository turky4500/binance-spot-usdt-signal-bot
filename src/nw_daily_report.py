"""التقرير اليومي لنظام NW 1H — عند 12:00 AM بتوقيت الرياض عن اليوم السابق.

قواعد المستخدم (2026-11-08):

* **التوقيت:** يُرسل مرة واحدة في أول دورة بعد منتصف الليل (00:00–00:59
  بتوقيت الرياض = 21:00–21:59 UTC)، ويحمل تاريخ **اليوم السابق** الذي انتهى
  للتو. لا يُرسل في أي ساعة أخرى، ولا مرتين لنفس اليوم.
* **المحتوى:** المراكز المفتوحة حاليًا + الصفقات المغلقة التي لم يُبلَّغ عنها
  في تقرير سابق + إجمالي صافي الربح/الخسارة لهذه الصفقات. **بلا** «إجمالي
  دورات البوت» و**بلا** «إجمالي التنبيهات المرسلة».
* **منع التراكم:** مفتاح كل صفقة ``symbol:entry_time:exit_time`` يُحفظ في
  ``daily_report_nw.reported_trade_keys`` بعد نجاح الإرسال فقط، فلا تُعاد
  الصفقة نفسها في تقرير لاحق. الصفقات التي أُغلقت ولم يُبلَّغ عنها سابقًا
  (مثل تعذّر دورة سابقة) تُدرج في أول تقرير تالٍ ⇒ لا تضيع ولا تتكرر.

هذا الملف جديد: لا يلمس شروط الإشارات ولا «درع إعادة الرسم» ولا «مدة الصفقة»
التي أضافها المستخدم، ولا نظام 15m.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .nw_messages import RIYADH, fmt_pct, fmt_price, result_label, riyadh_time

_LOG = logging.getLogger(__name__)

REPORT_HOUR_RIYADH = 0          # 12:00 AM ⇒ 12:59 AM بتوقيت الرياض
MAX_REPORTED_KEYS = 400        # سقف مفاتيح الصفقات المُبلَّغ عنها
MAX_ROWS = 40                  # سقف أسطر كل قسم
MAX_CHARS = 3800               # أقل من حد رسالة تيليجرام (4096)


def _trade_key(row: dict) -> str:
    """مفتاح ثابت للصفقة المغلقة ⇒ لا تتكرر في تقرير لاحق."""
    return f"{row.get('symbol', '')}:{row.get('entry_time', '')}:{row.get('exit_time', '')}"


def _read_closed(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        with p.open(encoding="utf-8-sig") as f:
            return [r for r in csv.DictReader(f)
                    if str(r.get("exit_time") or "").isdigit()]
    except (OSError, ValueError, csv.Error):
        return []


def _open_positions(state: dict) -> list[tuple[str, dict]]:
    out = []
    for sym, val in state.get("symbols", {}).items():
        if isinstance(val, dict) and isinstance(val.get("position"), dict):
            out.append((str(sym), val["position"]))
    return sorted(out)


def _int_ms(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _line_open(sym: str, pos: dict) -> str:
    price = fmt_price(float(pos.get("entry_price") or 0.0))
    when = riyadh_time(_int_ms(pos.get("entry_time"))) or "—"
    return f"• {sym} — دخول {price} | 🕒 {when}"


def _line_closed(row: dict) -> str:
    sym = str(row.get("symbol") or "")
    entry = fmt_price(float(row.get("entry_price") or 0.0))
    exit_ = fmt_price(float(row.get("exit_price") or 0.0))
    net = float(row.get("net_pct") or 0.0)
    when = riyadh_time(_int_ms(row.get("exit_time")))
    return f"• {sym} — {entry} ← {exit_} | {result_label(net)} {fmt_pct(net)} | 🕒 {when}"


def _section(title: str, lines: list[str], limit: int) -> list[str]:
    out = [f"{title} {len(lines)}", ""]
    if not lines:
        out.append("• لا يوجد")
    else:
        out.extend(lines[:limit])
        if len(lines) > limit:
            out.append(f"• …و {len(lines) - limit} أخرى")
    return out


def build_report_text(report_day: str, state: dict, new_closed: list[dict]) -> str:
    """مفتوحة + مغلقة جديدة + الإجمالي، بلا دورات وتنبيهات."""
    opens = _open_positions(state)
    open_lines = [_line_open(s, p) for s, p in opens]
    closed_lines = [_line_closed(r) for r in new_closed]
    total = sum(float(r.get("net_pct") or 0.0) for r in new_closed)

    # نُقلّص الأسطر لا الرأس/الإجمالي، حتى لو كثرت المراكز.
    limit = MAX_ROWS
    while True:
        parts = [
            "📊 **التقرير اليومي لنظام Nadaraya-Watson (NW 1H)**",
            "",
            f"📅 اليوم: `{report_day}` (حتى 12:00 AM بتوقيت الرياض)",
            "",
            *_section("📌 **المراكز المفتوحة:**", open_lines, limit),
            "",
            *_section("🔒 **الصفقات المغلقة:**", closed_lines, limit),
            "",
            f"📈 **إجمالي صافي الربح/الخسارة:** `{fmt_pct(total)}`",
        ]
        text = "\n".join(parts)
        if len(text) <= MAX_CHARS or limit <= 0:
            return text
        limit = max(0, limit - 5)


def send_daily_report_if_due(bot: Any, now_ms: int) -> str | None:
    """يُرسل تقرير اليوم السابق مرة واحدة عند 12 AM الرياض. يُعيد النص أو None.

    ``bot`` هو كائن ``NWBot`` (نعتمد على ``store`` و``trade_log`` و``tg`` فقط)،
    ما يجعل المنطق قابلًا للاختبار ببدائل بسيطة بلا شبكة ولا تيليجرام.
    """
    now = datetime.fromtimestamp(int(now_ms) / 1000, RIYADH)
    if now.hour != REPORT_HOUR_RIYADH:
        return None

    # اليوم السابق بالرياض ينتهي عند 12:00 AM الذي بدأ للتو.
    day_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = day_end - timedelta(days=1)
    end_ms = int(day_end.timestamp() * 1000)
    report_day = day_start.date().isoformat()

    state = bot.store.load()
    # لا نُنشئ المفتاح إلا عند الحفظ الناجح ⇒ فشل الإرسال لا يترك أثرًا.
    report_state = dict(state.get("daily_report_nw") or {})
    if report_state.get("last_report_day") == report_day:
        return None

    reported = set(report_state.get("reported_trade_keys") or [])
    seen: set[str] = set()
    new_closed: list[dict] = []
    for r in _read_closed(bot.trade_log):
        k = _trade_key(r)
        # أُغلقت قبل نهاية اليوم المُبلَّغ عنه + لم يُبلَّغ عنها من قبل + فريدة
        if _int_ms(r.get("exit_time")) < end_ms and k not in reported and k not in seen:
            seen.add(k)
            new_closed.append(r)

    text = build_report_text(report_day, state, new_closed)
    try:
        bot.tg.send_message(text)
    except Exception as e:                       # لا تُسجَّل الصفقة إن فشل الإرسال
        _LOG.error("❌ فشل إرسال التقرير اليومي لـ NW: %s", e)
        return None

    report_state["last_report_day"] = report_day
    report_state["reported_trade_keys"] = list(
        dict.fromkeys([*sorted(reported), *(_trade_key(r) for r in new_closed)])
    )[-MAX_REPORTED_KEYS:]
    state["daily_report_nw"] = report_state
    bot.store.save(state)
    _LOG.info("✅ أُرسل تقرير NW عن يوم %s (مفتوحة=%d مغلقة_جديدة=%d)",
              report_day, len(_open_positions(state)), len(new_closed))
    return text
