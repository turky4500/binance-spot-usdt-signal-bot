"""محرك اختبار موحّد: نفس قواعد الدخول والخروج لكل الأنظمة لضمان مقارنة عادلة.

قواعد ثابتة:
- الدخول عند إغلاق شمعة الإشارة (نفس سلوك البوت).
- الخروج يبدأ من الشمعة التالية فقط (لا خروج في شمعة الدخول).
- الهدف 2% ثابت (نفس البوت).
- الوقف = 1.5×ATR(14) مع حد أدنى 1.2% وحد أقصى 2.5% (نفس max_stop_pct في البوت).
- إذا لامست الشمعة الهدف والوقف معًا → نفترض الوقف أولًا (تحفّظ).
- عمولة 0.1% لكل جهة.
- صفقة واحدة لكل عملة في نفس الوقت.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TARGET_PCT = 2.0
ATR_STOP_MULT = 1.5
MIN_STOP_PCT = 1.2
MAX_STOP_PCT = 2.5
FEE_PER_SIDE_PCT = 0.1


def simulate(
    df: pd.DataFrame,
    entries: pd.Series,
    atr_series: pd.Series,
    cooldown_bars: int = 1,
    stop_pct_series: pd.Series | None = None,
    same_bar_rule: str = "stop_first",
    exit_mode: str = "intrabar",
) -> pd.DataFrame:
    """يحاكي نظامًا واحدًا على عملة واحدة. entries = إشارات دخول (نعم/لا) لكل شمعة."""
    close = df["close"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    atr_v = atr_series.to_numpy(dtype=float)
    sig = entries.fillna(False).to_numpy(dtype=bool)
    override = stop_pct_series.to_numpy(dtype=float) if stop_pct_series is not None else None
    open_ms = df["open_time"].to_numpy(dtype=np.int64)
    n = len(df)

    trades: list[dict] = []
    i = 0
    last_exit = -10**9
    while i < n - 1:
        if not sig[i] or np.isnan(atr_v[i]) or (i - last_exit) < cooldown_bars:
            i += 1
            continue

        entry = close[i]
        if override is not None:
            raw = override[i]
            if np.isnan(raw) or raw <= 0:
                i += 1
                continue
            stop_pct = float(np.clip(raw, MIN_STOP_PCT, MAX_STOP_PCT))
        else:
            stop_pct = float(np.clip(ATR_STOP_MULT * atr_v[i] / entry * 100.0, MIN_STOP_PCT, MAX_STOP_PCT))
        stop = entry * (1 - stop_pct / 100.0)
        target = entry * (1 + TARGET_PCT / 100.0)

        outcome = None
        exit_price = None
        exit_index = None
        peak = entry
        if exit_mode == "trailing_atr":
            for j in range(i + 1, n):
                peak = max(peak, float(high[j]))
                if np.isnan(atr_v[j]):
                    continue
                trail = peak - ATR_STOP_MULT * float(atr_v[j])
                if float(close[j]) <= trail:
                    outcome, exit_price, exit_index = ("win" if close[j] > entry else "loss"), float(close[j]), j
                    break
            if outcome is None:
                outcome, exit_price, exit_index = "open", float(close[n - 1]), n - 1
            gross = (exit_price / entry - 1.0) * 100.0
            trades.append({
                "entry_index": i, "entry_time": int(open_ms[i]), "exit_time": int(open_ms[exit_index]),
                "entry_price": entry, "exit_price": float(exit_price),
                "stop_pct": ATR_STOP_MULT * float(atr_v[i]) / entry * 100.0,
                "outcome": outcome, "gross_pct": gross, "net_pct": gross - 2.0 * FEE_PER_SIDE_PCT,
                "bars_held": exit_index - i,
            })
            last_exit = exit_index
            i = exit_index + 1
            continue

        for j in range(i + 1, n):
            if exit_mode == "close":
                # قرار على إغلاق الشمعة فقط — بلا أي غموض داخل الشمعة
                if close[j] <= stop:
                    outcome, exit_price, exit_index = "stop", stop, j
                    break
                if close[j] >= target:
                    outcome, exit_price, exit_index = "target", target, j
                    break
                continue
            hit_stop = low[j] <= stop
            hit_target = high[j] >= target
            if hit_stop and hit_target:
                if same_bar_rule == "target_first":
                    outcome, exit_price, exit_index = "target", target, j
                elif same_bar_rule == "skip":
                    continue
                else:
                    outcome, exit_price, exit_index = "stop", stop, j
                break
            if hit_stop:
                outcome, exit_price, exit_index = "stop", stop, j
                break
            if hit_target:
                outcome, exit_price, exit_index = "target", target, j
                break

        if outcome is None:
            # صفقة ما زالت مفتوحة حتى نهاية البيانات — تُسجَّل بسعر الإغلاق الأخير
            outcome, exit_price, exit_index = "open", close[n - 1], n - 1

        gross = (exit_price / entry - 1.0) * 100.0
        net = gross - 2.0 * FEE_PER_SIDE_PCT
        trades.append({
            "entry_index": i,
            "entry_time": int(open_ms[i]),
            "exit_time": int(open_ms[exit_index]),
            "entry_price": entry,
            "exit_price": float(exit_price),
            "stop_pct": stop_pct,
            "outcome": outcome,
            "gross_pct": gross,
            "net_pct": net,
            "bars_held": exit_index - i,
        })
        last_exit = exit_index
        i = exit_index + 1

    return pd.DataFrame(trades)


def random_entries_like(df: pd.DataFrame, count: int, seed: int, warmup: int = 210) -> pd.Series:
    """إشارات دخول عشوائية بعدد مطابق — للمقارنة المرجعية (لا حافة متوقعة)."""
    rng = np.random.default_rng(seed)
    n = len(df)
    sig = np.zeros(n, dtype=bool)
    if count <= 0 or n <= warmup + 2:
        return pd.Series(sig, index=df.index)
    picks = rng.choice(np.arange(warmup, n - 1), size=min(count, n - warmup - 1), replace=False)
    sig[picks] = True
    return pd.Series(sig, index=df.index)


def summarize(trades: pd.DataFrame, label: str) -> dict:
    if trades is None or trades.empty:
        return {
            "system": label, "trades": 0, "win_rate": 0.0, "avg_net_pct": 0.0, "total_net_pct": 0.0,
            "avg_win_pct": 0.0, "avg_loss_pct": 0.0, "profit_factor": 0.0, "max_dd_pct": 0.0,
            "avg_bars_held": 0.0, "t_stat": 0.0, "stop_avg_pct": 0.0,
        }

    closed = trades[trades["outcome"].isin(["target", "stop"])]
    wins = closed[closed["outcome"] == "target"]
    losses = closed[closed["outcome"] == "stop"]
    net = closed["net_pct"].to_numpy(dtype=float)

    equity = np.cumsum(net)
    running_max = np.maximum.accumulate(equity)
    max_dd = float(np.max(running_max - equity)) if len(equity) else 0.0

    gross_win = float(wins["net_pct"].sum())
    gross_loss = float(-losses["net_pct"].sum())
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0
    t_stat = float(net.mean() / (net.std(ddof=1) / np.sqrt(len(net)))) if len(net) > 2 and net.std(ddof=1) > 0 else 0.0

    return {
        "system": label,
        "trades": int(len(closed)),
        "win_rate": round(len(wins) / len(closed) * 100, 2) if len(closed) else 0.0,
        "avg_net_pct": round(float(net.mean()), 4) if len(net) else 0.0,
        "total_net_pct": round(float(net.sum()), 2) if len(net) else 0.0,
        "avg_win_pct": round(float(wins["net_pct"].mean()), 4) if len(wins) else 0.0,
        "avg_loss_pct": round(float(losses["net_pct"].mean()), 4) if len(losses) else 0.0,
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else 999.0,
        "max_dd_pct": round(max_dd, 2),
        "avg_bars_held": round(float(closed["bars_held"].mean()), 2) if len(closed) else 0.0,
        "t_stat": round(t_stat, 2),
        "stop_avg_pct": round(float(closed["stop_pct"].mean()), 3) if len(closed) else 0.0,
        "open_trades": int((trades["outcome"] == "open").sum()),
    }
