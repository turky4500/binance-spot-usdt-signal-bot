"""منطق «تجربة تتبّع الاتجاه» على فريم 1H — وحدة نقية بلا I/O ولا تيليجرام.

القاعدة (مطابقة حرفيًا لل باك تست في ``backtest_hbar_1m/strategy_analysis.py``):

    دخول عند إغلاق شمعة 1H مغلقة:
        close > أعلى high لـ20 شمعة سابقة   (Donchian 20)
        و close > EMA(200)
        و EMA(200)[j] > EMA(200)[j-10]      (ميل صاعد)
    وقف أولي   = entry − 2.5 × ATR(14)
    خط التتبّع = max(الوقف الأولي، أدنى low لـ10 شموع سابقة)، ويُحدَّث صعودًا فقط
    خروج       = low <= trail  ⇒  يُملأ عند trail
    رسوم       = 0.2% ذهابًا وعودة تُطرح من النتيجة فقط

لا هدف ربح ثابت، لا إعادة رسم، لا تعويض هابط، لا رافعة، لا شورت، لا بوابة BTC،
لا مرآة Pine، ولا أي مقدّر بديل. هذه الوحدة لا تلمس أي ملف لأنظمة أخرى.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

RIYADH = timezone(timedelta(hours=3))
INTERVAL_MS = 3_600_000
FEE_PCT = 0.2

EMA_LEN = 200
EMA_SLOPE_LAG = 10
DONCHIAN_ENTRY_LEN = 20
DONCHIAN_EXIT_LEN = 10
ATR_LEN = 14
ATR_STOP_MULT = 2.5
WARMUP = 200
MAX_CATCHUP_BARS = 6

# نفس قائمة الاستبعاد المعتمدة في النظام السادس: عملات مستقرة مربوطة فقط.
# «كل أزواج USDT» ما عدا هذه (قرار المستخدم 2026-11-16).
PEGGED_STABLE_BASES = frozenset({
    "USDC", "FDUSD", "TUSD", "RLUSD", "XUSD", "USD1", "USDE", "USDS", "BFUSD", "U",
    "EUR", "EURI", "BUSD", "USDP", "DAI", "PYUSD", "GUSD", "LUSD", "AEUR", "EURC",
    "USDY", "USDD", "USDF",
})


def riyadh(ms: int | float) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).astimezone(RIYADH)


def fmt_time(ms: int | float) -> str:
    return riyadh(ms).strftime("%Y-%m-%d %H:%M")


def fmt_date(ms: int | float) -> str:
    return riyadh(ms).strftime("%Y-%m-%d")


def fmt_price(x: float) -> str:
    x = float(x)
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.5f}".rstrip("0").rstrip(".")
    if x >= 0.01:
        return f"{x:.6f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".")


def is_excluded(symbol: str) -> bool:
    """استثناء العملات المستقرة المربوطة فقط؛ كل أزواج USDT الأخرى مشمولة."""
    if not symbol.endswith("USDT"):
        return True
    return symbol[:-4] in PEGGED_STABLE_BASES


def indicators(high, low, close) -> dict[str, np.ndarray]:
    """مؤشرات سببية: كل قيمة عند j تعتمد على j وما قبله فقط (لا إعادة رسم)."""
    df = pd.DataFrame({"high": np.asarray(high, dtype=float),
                       "low": np.asarray(low, dtype=float),
                       "close": np.asarray(close, dtype=float)})
    ema = df["close"].ewm(span=EMA_LEN, adjust=False).mean().to_numpy()
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(ATR_LEN).mean().to_numpy()
    dh20 = df["high"].rolling(DONCHIAN_ENTRY_LEN).max().shift(1).to_numpy()
    dl10 = df["low"].rolling(DONCHIAN_EXIT_LEN).min().shift(1).to_numpy()
    return {"ema": ema, "atr": atr, "dh20": dh20, "dl10": dl10}


def step(j: int, ind: dict, t: np.ndarray, close: np.ndarray, low: np.ndarray,
         position: dict | None) -> tuple[str | None, dict, dict | None]:
    """خطوة واحدة على الشمعة المغلقة j.

    يُرجع ``(action, payload, new_position)`` حيث action ∈ {None, 'entry', 'exit'}.
    """
    if position is not None:
        trail = float(position["trail"])
        dl = ind["dl10"][j]
        if np.isfinite(dl):
            trail = max(trail, float(dl))
        newpos = {**position, "trail": trail}
        if float(low[j]) <= trail:
            ex = trail
            gross = (ex / float(position["entry"]) - 1.0) * 100.0
            payload = {
                "entry": float(position["entry"]),
                "entry_bar_ms": int(position["entry_bar_ms"]),
                "exit": ex,
                "exit_bar_ms": int(t[j]),
                "trail": trail,
                "gross_pct": round(gross, 4),
                "net_pct": round(gross - FEE_PCT, 4),
                "holding_bars": int((int(t[j]) - int(position["entry_bar_ms"])) // INTERVAL_MS),
                "exit_reason": "trail_stop",
            }
            return "exit", payload, None
        return None, {}, newpos

    ema, atr, dh20, dl10 = ind["ema"], ind["atr"], ind["dh20"], ind["dl10"]
    if j < EMA_SLOPE_LAG:
        return None, {}, None
    if not (np.isfinite(dh20[j]) and np.isfinite(ema[j]) and np.isfinite(atr[j])):
        return None, {}, None
    if not (float(close[j]) > float(dh20[j])
            and float(close[j]) > float(ema[j])
            and float(ema[j]) > float(ema[j - EMA_SLOPE_LAG])):
        return None, {}, None

    entry = float(close[j])
    init_stop = entry - ATR_STOP_MULT * float(atr[j])
    dl = dl10[j]
    trail = max(init_stop, float(dl)) if np.isfinite(dl) else init_stop
    pos = {
        "entry": entry,
        "entry_bar_ms": int(t[j]),
        "trail": trail,
        "init_stop": init_stop,
        "ema200": float(ema[j]),
        "donchian20": float(dh20[j]),
        "atr14": float(atr[j]),
        "donchian10_at_entry": float(dl) if np.isfinite(dl) else None,
    }
    return "entry", pos, pos


def entry_message(symbol: str, bar_ms: int, pos: dict) -> str:
    above_ema = (pos["entry"] / pos["ema200"] - 1) * 100 if pos["ema200"] else 0.0
    above_dh = (pos["entry"] / pos["donchian20"] - 1) * 100 if pos["donchian20"] else 0.0
    return (
        "🧪 تجربة TREND · 1H | توصية دخول\n"
        f"الزوج: {symbol}\n"
        f"الشمعة المغلقة: {fmt_time(bar_ms)} (الرياض)\n"
        f"الإغلاق: {fmt_price(pos['entry'])}\n"
        f"EMA200: {fmt_price(pos['ema200'])} ← الإغلاق فوقه بـ {above_ema:.2f}%، وميله صاعد\n"
        f"قمة 20 شمعة سابقة: {fmt_price(pos['donchian20'])} ← اختراق بـ {above_dh:.2f}%\n"
        f"ATR(14): {fmt_price(pos['atr14'])}\n"
        f"الوقف الأولي: {fmt_price(pos['init_stop'])} = الدخول − 2.5×ATR\n"
        f"خط التتبّع الآن: {fmt_price(pos['trail'])} = max(الوقف، قاع 10 شموع)\n"
        "الخروج: إذا لمس low خط التتبّع — لا هدف ربح ثابت.\n"
        "⚠️ توصية تجريبية، ليست تنفيذًا فعليًا ولا نصيحة استثمارية."
    )


def exit_message(symbol: str, bar_ms: int, p: dict) -> str:
    return (
        "🧪 تجربة TREND · 1H | إشارة خروج\n"
        f"الزوج: {symbol}\n"
        f"الشمعة المغلقة: {fmt_time(bar_ms)} (الرياض)\n"
        f"الدخول: {fmt_price(p['entry'])} @ {fmt_time(p['entry_bar_ms'])}\n"
        f"الخروج: {fmt_price(p['exit'])} (لمس low خط التتبّع)\n"
        f"النتيجة: {p['net_pct']:+.3f}% صافي بعد 0.2% عمولة | "
        f"المدة: {p['holding_bars']} ساعة\n"
        "⚠️ محاكاة تجريبية، ليست تنفيذًا فعليًا ولا نصيحة استثمارية."
    )


def metrics(rows: list[dict]) -> dict:
    """مقاييس مجمّعة على قائمة صفقات مغلقة (حقل net_pct)."""
    nets = [float(r["net_pct"]) for r in rows]
    if not nets:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0,
                "avg_win_net": 0.0, "avg_loss_net": 0.0, "payoff": 0.0,
                "avg_net_pct": 0.0, "total_additive_net_pct": 0.0,
                "compounded_return_pct": 0.0, "max_drawdown_pct": 0.0,
                "profit_factor": 0.0, "best_net_pct": 0.0, "worst_net_pct": 0.0,
                "avg_holding_bars": 0.0}
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    eq = [1.0]
    for n in nets:
        eq.append(eq[-1] * (1 + n / 100.0))
    peak, dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak * 100.0)
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    holds = [float(r.get("holding_bars", 0)) for r in rows]
    return {
        "trades": len(nets),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / len(nets), 2),
        "avg_win_net": round(sum(wins) / len(wins), 4) if wins else 0.0,
        "avg_loss_net": round(sum(losses) / len(losses), 4) if losses else 0.0,
        "payoff": round(abs((sum(wins) / len(wins)) / (sum(losses) / len(losses))), 3)
                  if wins and losses and sum(losses) else 0.0,
        "avg_net_pct": round(sum(nets) / len(nets), 4),
        "total_additive_net_pct": round(sum(nets), 4),
        "compounded_return_pct": round((eq[-1] - 1) * 100, 4),
        "max_drawdown_pct": round(dd, 4),
        "profit_factor": round(pf, 4) if np.isfinite(pf) else "∞",
        "best_net_pct": round(max(nets), 4),
        "worst_net_pct": round(min(nets), 4),
        "avg_holding_bars": round(sum(holds) / len(holds), 2),
    }
