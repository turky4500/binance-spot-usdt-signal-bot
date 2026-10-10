from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Iterable

from .trade_journal import TRADE_LOG_FILE
from .utils import local_date_key_from_ms


def _to_float(value: str | int | float | None) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def load_trade_rows(data_dir: str) -> list[dict[str, str]]:
    path = Path(data_dir) / TRADE_LOG_FILE
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def rows_for_entry_day(rows: Iterable[dict[str, str]], report_day: str) -> list[dict[str, str]]:
    return [row for row in rows if (row.get("entry_date_local") or "") == report_day]


def rows_for_exit_day(rows: Iterable[dict[str, str]], report_day: str, tz_name: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in rows:
        exit_time_ms = _to_float(row.get("exit_time_ms"))
        if exit_time_ms is None:
            continue
        if local_date_key_from_ms(int(exit_time_ms), tz_name) == report_day:
            out.append(row)
    return out


def rows_for_entry_day_range(rows: Iterable[dict[str, str]], start_day: str, end_day: str) -> list[dict[str, str]]:
    """صفقات دخلت خلال فترة (شامل الطرفين) بحسب يوم الدخول المحلي."""
    return [row for row in rows if start_day <= str(row.get("entry_date_local") or "") <= end_day]


def rows_for_exit_day_range(rows: Iterable[dict[str, str]], start_day: str, end_day: str, tz_name: str) -> list[dict[str, str]]:
    """صفقات أُغلقت خلال فترة (شامل الطرفين) بحسب يوم الإغلاق المحلي."""
    selected = []
    for row in rows:
        try:
            exit_ms = int(float(row.get("exit_time_ms") or 0))
        except (TypeError, ValueError):
            continue
        if exit_ms <= 0:
            continue
        if start_day <= local_date_key_from_ms(exit_ms, tz_name) <= end_day:
            selected.append(row)
    return selected


def avg_metric(rows: Iterable[dict[str, str]], key: str) -> float | None:
    vals = [_to_float(row.get(key)) for row in rows]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def success_rate_percent(wins: int, losses: int) -> float:
    total = wins + losses
    if total <= 0:
        return 0.0
    return wins / total * 100.0


WIN_OUTCOMES = {"target", "win"}
LOSS_OUTCOMES = {"stop", "loss"}
# صفقة تحقّق هدفًا فأكثر ثم أُغلقت بنتيجة سالبة — لا تُحتسب ضمن الخاسرة (قرار المستخدم)
NEUTRAL_OUTCOMES = {"partial", "neutral"}


def is_win(outcome: str | None) -> bool:
    return (outcome or "") in WIN_OUTCOMES


def is_loss(outcome: str | None) -> bool:
    return (outcome or "") in LOSS_OUTCOMES


def is_neutral(outcome: str | None) -> bool:
    return (outcome or "") in NEUTRAL_OUTCOMES


def top_symbols(rows: Iterable[dict[str, str]], empty_text: str, limit: int = 3) -> str:
    counts = Counter(row.get("symbol", "") for row in rows if row.get("symbol"))
    if not counts:
        return empty_text
    return " • ".join(f"{symbol} ({count})" for symbol, count in counts.most_common(limit))


def target_hit_stats(rows: Iterable[dict[str, str]]) -> dict[str, int]:
    """عدد الصفقات التي بلغت كل هدف من الأهداف الثلاثة (من حقول hit_target*)."""
    stats = {"t1": 0, "t2": 0, "t3": 0, "any": 0, "total": 0}
    for row in rows:
        stats["total"] += 1
        hit_any = False
        for key, field in (("t1", "hit_target1"), ("t2", "hit_target2"), ("t3", "hit_target3")):
            if str(row.get(field) or "0") == "1":
                stats[key] += 1
                hit_any = True
        if hit_any:
            stats["any"] += 1
    return stats


def build_daily_observations(
    win_rows: list[dict[str, str]],
    loss_rows: list[dict[str, str]],
    closed_rows: list[dict[str, str]],
) -> list[str]:
    """ملاحظات تحليلية مختصرة لصفقات Target Trend المغلقة في اليوم."""
    notes: list[str] = []

    win_hits = target_hit_stats(win_rows)
    loss_hits = target_hit_stats(loss_rows)
    if win_hits["total"] >= 2 and loss_hits["total"] >= 2:
        win_share = win_hits["any"] / win_hits["total"] * 100.0
        loss_share = loss_hits["any"] / loss_hits["total"] * 100.0
        if win_share >= loss_share + 15:
            notes.append(
                f"الصفقات الرابحة بلغت هدفًا على الأقل في {win_share:.0f}% من الحالات مقابل {loss_share:.0f}% للخاسرة."
            )
        elif loss_share >= win_share + 15:
            notes.append(
                f"الخاسرة بلغت أهدافًا أكثر ({loss_share:.0f}% مقابل {win_share:.0f}%) — راجع مسافة الأهداف أو الوقف."
            )

    avg_win = avg_metric(win_rows, "net_return_pct")
    avg_loss = avg_metric(loss_rows, "net_return_pct")
    if avg_win is not None and avg_loss is not None:
        notes.append(f"متوسط الرابحة {avg_win:+.2f}% مقابل الخاسرة {avg_loss:+.2f}%.")

    dur_win = avg_metric(win_rows, "duration_minutes")
    dur_loss = avg_metric(loss_rows, "duration_minutes")
    if dur_win is not None and dur_loss is not None:
        notes.append(
            f"متوسط مدة الرابحة {dur_win / 60:.1f} ساعة مقابل الخاسرة {dur_loss / 60:.1f} ساعة."
        )

    if not notes:
        notes.append("لا توجد عيّنة كافية اليوم لاستنتاج نوعي — نحتاج مزيدًا من الصفقات التراكمية.")
    return notes[:4]
