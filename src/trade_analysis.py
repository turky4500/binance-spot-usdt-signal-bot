from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Iterable

from .shadow_journal import SHADOW_LOG_FILE
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


def load_shadow_rows(data_dir: str) -> list[dict[str, str]]:
    path = Path(data_dir) / SHADOW_LOG_FILE
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def shadow_stats(rows: Iterable[dict[str, str]]) -> dict[str, float | int]:
    rows = list(rows)
    wins = sum(1 for row in rows if row.get("outcome") == "target")
    losses = sum(1 for row in rows if row.get("outcome") == "stop")
    return {
        "total": len(rows),
        "wins": wins,
        "losses": losses,
        "pending": sum(1 for row in rows if row.get("outcome") in ("", None)),
        "rate": success_rate_percent(wins, losses),
    }


def rows_for_entry_day(rows: Iterable[dict[str, str]], report_day: str) -> list[dict[str, str]]:
    return [row for row in rows if (row.get("entry_date_local") or "") == report_day]


def shadow_rows_for_day(rows: Iterable[dict[str, str]], report_day: str) -> list[dict[str, str]]:
    return [row for row in rows if (row.get("rejected_date_local") or "") == report_day]


def rows_for_exit_day(rows: Iterable[dict[str, str]], report_day: str, tz_name: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in rows:
        exit_time_ms = _to_float(row.get("exit_time_ms"))
        if exit_time_ms is None:
            continue
        if local_date_key_from_ms(int(exit_time_ms), tz_name) == report_day:
            out.append(row)
    return out


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


def top_symbols(rows: Iterable[dict[str, str]], empty_text: str, limit: int = 3) -> str:
    counts = Counter(row.get("symbol", "") for row in rows if row.get("symbol"))
    if not counts:
        return empty_text
    return " • ".join(f"{symbol} ({count})" for symbol, count in counts.most_common(limit))


def strong_vs_normal_stats(rows: Iterable[dict[str, str]]) -> dict[str, float | int]:
    strong = [r for r in rows if str(r.get("strong_signal", "0")) == "1"]
    normal = [r for r in rows if str(r.get("strong_signal", "0")) != "1"]
    strong_wins = sum(1 for r in strong if r.get("outcome") == "target")
    strong_losses = sum(1 for r in strong if r.get("outcome") == "stop")
    normal_wins = sum(1 for r in normal if r.get("outcome") == "target")
    normal_losses = sum(1 for r in normal if r.get("outcome") == "stop")
    return {
        "strong_count": len(strong),
        "normal_count": len(normal),
        "strong_rate": success_rate_percent(strong_wins, strong_losses),
        "normal_rate": success_rate_percent(normal_wins, normal_losses),
    }


def build_daily_observations(win_rows: list[dict[str, str]], loss_rows: list[dict[str, str]], closed_rows: list[dict[str, str]]) -> list[str]:
    notes: list[str] = []

    win_buy = avg_metric(win_rows, "buy_score")
    loss_buy = avg_metric(loss_rows, "buy_score")
    if win_buy is not None and loss_buy is not None and win_buy >= loss_buy + 0.25:
        notes.append(f"الصفقات الرابحة اليوم امتلكت Buy Score أعلى بمتوسط {win_buy:.2f} مقابل {loss_buy:.2f} للخاسرة.")

    win_rvol = avg_metric(win_rows, "relative_volume")
    loss_rvol = avg_metric(loss_rows, "relative_volume")
    if win_rvol is not None and loss_rvol is not None and win_rvol >= loss_rvol * 1.10:
        notes.append(f"الحجم النسبي الأعلى ارتبط بنتائج أفضل: متوسط الرابحة {win_rvol:.2f}x مقابل {loss_rvol:.2f}x للخاسرة.")

    win_adx = avg_metric(win_rows, "adx")
    loss_adx = avg_metric(loss_rows, "adx")
    if win_adx is not None and loss_adx is not None and loss_adx >= win_adx + 2:
        notes.append(f"الصفقات الخاسرة جاءت مع ADX أعلى غالبًا: {loss_adx:.2f} مقابل {win_adx:.2f} للرابحة.")

    win_rr = avg_metric(win_rows, "reward_risk_ratio")
    loss_rr = avg_metric(loss_rows, "reward_risk_ratio")
    if win_rr is not None and loss_rr is not None and win_rr >= loss_rr + 0.10:
        notes.append(f"العائد إلى المخاطرة كان أفضل في الرابحة: {win_rr:.2f} مقابل {loss_rr:.2f} للخاسرة.")

    signal_stats = strong_vs_normal_stats(closed_rows)
    if signal_stats["strong_count"] >= 2 and signal_stats["normal_count"] >= 2:
        strong_rate = float(signal_stats["strong_rate"])
        normal_rate = float(signal_stats["normal_rate"])
        if strong_rate >= normal_rate + 10:
            notes.append(f"الإشارات القوية تفوقت اليوم: نسبة نجاح {strong_rate:.1f}% مقابل {normal_rate:.1f}% للعادية.")
        elif normal_rate >= strong_rate + 10:
            notes.append(f"الإشارات العادية كانت أفضل اليوم: {normal_rate:.1f}% مقابل {strong_rate:.1f}% للقوية، ويستحق ذلك المراجعة.")

    if not notes:
        notes.append("لا توجد فروق رقمية كافية اليوم لاستخراج سبب واضح؛ نحتاج مزيدًا من البيانات التراكمية.")
    return notes[:4]
