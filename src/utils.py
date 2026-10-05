from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def format_price(value: float) -> str:
    if value is None:
        return "—"
    formatted = f"{value:.8f}".rstrip("0").rstrip(".")
    return formatted or "0"


def ms_to_local_text(ms: int, tz_name: str) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz_name))
    period = "مساءً" if dt.hour >= 12 else "صباحًا"
    hour12 = dt.hour % 12
    if hour12 == 0:
        hour12 = 12
    return f"{dt:%Y-%m-%d} {hour12}:{dt:%M:%S} {period}"


def humanize_duration_ar(start_ms: int, end_ms: int) -> str:
    total_seconds = max(int((end_ms - start_ms) / 1000), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts: list[str] = []
    if hours:
        parts.append(f"{hours} ساعة" if hours == 1 else f"{hours} ساعات")
    if minutes:
        parts.append(f"{minutes} دقيقة")
    if seconds and not parts:
        parts.append(f"{seconds} ثانية")
    return " و ".join(parts) if parts else "أقل من دقيقة"
