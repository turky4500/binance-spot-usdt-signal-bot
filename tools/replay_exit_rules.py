"""يعيد تشغيل قواعد الخروج على الصفقات الحقيقية المسجّلة في trades_log.csv.

الغرض: معرفة ماذا كان سيحدث لو كانت قاعدة الوقف المتحرك مفعّلة على نفس الصفقات
التي دخلها البوت فعلًا — بنفس أسعار الدخول الحقيقية وبيانات Binance الفعلية.

الاستخدام:
    python tools/replay_exit_rules.py data/trades_log.csv
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategy import atr as compute_atr  # noqa: E402

BASE_URL = "https://data-api.binance.vision"
HOUR_MS = 3_600_000
FEE_PER_SIDE_PCT = 0.1
TRAIL_ATR_MULT = 2.0
ATR_LEN = 14


def fetch_forward(symbol: str, entry_open_time: int, hours_after: int = 336, session: requests.Session | None = None) -> pd.DataFrame:
    """يجلب شموع الساعة من 60 ساعة قبل الدخول إلى hours_after ساعة بعده."""
    start = int(entry_open_time) - 60 * HOUR_MS
    end = int(entry_open_time) + hours_after * HOUR_MS
    sess = session or requests
    rows: list[list] = []
    cursor = start
    while cursor < end:
        resp = sess.get(f"{BASE_URL}/api/v3/klines", params={
            "symbol": symbol, "interval": "1h", "startTime": cursor, "endTime": end, "limit": 1000,
        }, timeout=20)
        resp.raise_for_status()
        chunk = resp.json()
        if not chunk:
            break
        rows.extend(chunk)
        if chunk[-1][0] <= cursor:
            break
        cursor = chunk[-1][0] + HOUR_MS
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "close_time",
                                     "qav", "trades", "tbb", "tbq", "ignore"])
    df = df[["open_time", "open", "high", "low", "close", "close_time"]].copy()
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_numeric(df["open_time"]).astype("int64")
    df = df.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)
    return df


def simulate_trailing(df: pd.DataFrame, entry_open_time: int, entry_price: float) -> dict | None:
    """قاعدة الوقف المتحرك: 2×ATR من أعلى سعر، الخروج عند إغلاق شمعة تحت الوقف."""
    # entry_time_ms في السجل هو وقت إغلاق شمعة الدخول → نجد الشمعة التي تحتويه
    candidates = df.index[df["open_time"] <= int(entry_open_time)]
    if len(candidates) == 0:
        return None
    start = int(candidates[-1])
    atr_series = compute_atr(df, ATR_LEN)

    peak = float(entry_price)
    high = df["high"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    open_ms = df["open_time"].to_numpy(dtype=np.int64)
    atr_v = atr_series.to_numpy(dtype=float)

    for j in range(start + 1, len(df)):
        peak = max(peak, float(high[j]))
        if np.isnan(atr_v[j]):
            continue
        trail = peak - TRAIL_ATR_MULT * float(atr_v[j])
        if float(close[j]) <= trail:
            gross = (float(close[j]) / float(entry_price) - 1.0) * 100.0
            return {
                "rule_outcome": "win" if gross - 2 * FEE_PER_SIDE_PCT > 0 else "loss",
                "rule_exit_time": int(open_ms[j]),
                "rule_exit_price": float(close[j]),
                "rule_net_pct": round(gross - 2 * FEE_PER_SIDE_PCT, 4),
                "rule_peak_pct": round((peak / float(entry_price) - 1.0) * 100.0, 4),
                "rule_hours_held": j - start,
                "rule_reached_by_end": False,
            }

    # لم تُغلق خلال الأفق الزمني — نُسجّلها بالنتيجة حتى آخر سعر متاح
    gross = (float(close[-1]) / float(entry_price) - 1.0) * 100.0
    return {
        "rule_outcome": "open_at_horizon",
        "rule_exit_time": int(open_ms[-1]),
        "rule_exit_price": float(close[-1]),
        "rule_net_pct": round(gross - 2 * FEE_PER_SIDE_PCT, 4),
        "rule_peak_pct": round((peak / float(entry_price) - 1.0) * 100.0, 4),
        "rule_hours_held": len(df) - 1 - start,
        "rule_reached_by_end": True,
    }


def simulate_variants(df: pd.DataFrame, entry_time_ms: int, entry_price: float,
                      stop_price: float, target_price: float, atr_mult: float = 2.0,
                      lock_pct: float = 0.0) -> dict | None:
    """قاعدة هجينة: وقف ثابت (وقف الإشارة الأصلي) حتى بلوغ الهدف المرجعي، ثم وقف متحرك يقفل ربحًا.

    تُرجّع أيضًا نتيجة "الوقف المتحرك النقي" على نفس الشمعة الأساس.
    """
    candidates = df.index[df["open_time"] <= int(entry_time_ms)]
    if len(candidates) == 0:
        return None
    start = int(candidates[-1])
    atr_series = compute_atr(df, ATR_LEN)
    high = df["high"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    open_ms = df["open_time"].to_numpy(dtype=np.int64)
    atr_v = atr_series.to_numpy(dtype=float)

    reached = False
    peak = float(entry_price)
    lock_level = float(entry_price) * (1 + lock_pct / 100.0)

    for j in range(start + 1, len(df)):
        peak = max(peak, float(high[j]))
        if not reached:
            if float(close[j]) <= float(stop_price):
                gross = (float(stop_price) / float(entry_price) - 1.0) * 100.0
                return {"hyb_outcome": "loss", "hyb_net_pct": round(gross - 2 * FEE_PER_SIDE_PCT, 4),
                        "hyb_exit_time": int(open_ms[j]), "hyb_peak_pct": round((peak / float(entry_price) - 1) * 100, 4),
                        "hyb_reached_target": False, "hyb_hours_held": j - start}
            if float(high[j]) >= float(target_price):
                reached = True
            continue
        if np.isnan(atr_v[j]):
            continue
        trail = max(lock_level, peak - atr_mult * float(atr_v[j]))
        if float(close[j]) <= trail:
            gross = (float(close[j]) / float(entry_price) - 1.0) * 100.0
            return {"hyb_outcome": "win" if gross - 2 * FEE_PER_SIDE_PCT > 0 else "loss",
                    "hyb_net_pct": round(gross - 2 * FEE_PER_SIDE_PCT, 4), "hyb_exit_time": int(open_ms[j]),
                    "hyb_peak_pct": round((peak / float(entry_price) - 1) * 100, 4),
                    "hyb_reached_target": True, "hyb_hours_held": j - start}

    gross = (float(close[-1]) / float(entry_price) - 1.0) * 100.0
    return {"hyb_outcome": "open_at_horizon", "hyb_net_pct": round(gross - 2 * FEE_PER_SIDE_PCT, 4),
            "hyb_exit_time": int(open_ms[-1]), "hyb_peak_pct": round((peak / float(entry_price) - 1) * 100, 4),
            "hyb_reached_target": reached, "hyb_hours_held": len(df) - 1 - start}


def simulate_old_rule_realistic(df: pd.DataFrame, entry_time_ms: int, entry_price: float,
                                 stop_price: float, target_price: float) -> dict | None:
    """القاعدة القديمة بأسعار تنفيذ واقعية: الهدف عند لمسه، والوقف عند إغلاق الشمعة أسفله.

    هذا هو التطبيق العادل: نفس اصطلاح التنفيذ المستخدم في محاكاة الوقف المتحرك.
    """
    candidates = df.index[df["open_time"] <= int(entry_time_ms)]
    if len(candidates) == 0:
        return None
    start = int(candidates[-1])
    high = df["high"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    open_ms = df["open_time"].to_numpy(dtype=np.int64)

    for j in range(start + 1, len(df)):
        if float(high[j]) >= float(target_price):
            gross = (float(target_price) / float(entry_price) - 1.0) * 100.0
            return {"oldr_net_pct": round(gross - 2 * FEE_PER_SIDE_PCT, 4), "oldr_outcome": "target",
                    "oldr_hours_held": j - start}
        if float(close[j]) <= float(stop_price):
            gross = (float(close[j]) / float(entry_price) - 1.0) * 100.0
            return {"oldr_net_pct": round(gross - 2 * FEE_PER_SIDE_PCT, 4), "oldr_outcome": "stop",
                    "oldr_hours_held": j - start}
    gross = (float(close[-1]) / float(entry_price) - 1.0) * 100.0
    return {"oldr_net_pct": round(gross - 2 * FEE_PER_SIDE_PCT, 4), "oldr_outcome": "open_at_horizon",
            "oldr_hours_held": len(df) - 1 - start}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", nargs="?", default="data/trades_log.csv")
    parser.add_argument("--out", default="reports/exit_rule_replay.csv")
    args = parser.parse_args()

    rows = list(pd.read_csv(args.csv_path, dtype=str).fillna("").to_dict("records"))
    closed = [r for r in rows if r.get("outcome") in {"target", "stop", "win", "loss"}]
    print(f"صفقات مغلقة مسجّلة: {len(closed)}", file=sys.stderr)

    results = []
    with requests.Session() as session:
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = {
                pool.submit(fetch_forward, r["symbol"], int(r["entry_time_ms"]), 336, session): r
                for r in closed
                if r.get("entry_time_ms")
            }
            for i, fut in enumerate(as_completed(futures), 1):
                record = futures[fut]
                try:
                    df = fut.result()
                except Exception:
                    continue
                if df.empty:
                    continue
                sim = simulate_trailing(df, int(record["entry_time_ms"]), float(record["entry_price"]))
                if not sim:
                    continue
                extra = simulate_variants(
                    df, int(record["entry_time_ms"]), float(record["entry_price"]),
                    float(record.get("stop_price") or 0) or float(record["entry_price"]) * 0.985,
                    float(record.get("target_price") or 0) or float(record["entry_price"]) * 1.02,
                )
                if extra:
                    sim.update(extra)
                oldr = simulate_old_rule_realistic(
                    df, int(record["entry_time_ms"]), float(record["entry_price"]),
                    float(record.get("stop_price") or 0) or float(record["entry_price"]) * 0.985,
                    float(record.get("target_price") or 0) or float(record["entry_price"]) * 1.02,
                )
                if oldr:
                    sim.update(oldr)
                old_net = float(record["net_return_pct"]) if record.get("net_return_pct") not in ("", None) else np.nan
                results.append({
                    "symbol": record["symbol"],
                    "entry_time_ms": record["entry_time_ms"],
                    "entry_price": record["entry_price"],
                    "old_outcome": record["outcome"],
                    "old_net_pct": old_net,
                    **sim,
                })
                if i % 50 == 0:
                    print(f"  {i}/{len(futures)}", file=sys.stderr, flush=True)
                time.sleep(0)

    out = pd.DataFrame(results)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)

    old = out["old_net_pct"].to_numpy(dtype=float)
    new = out["rule_net_pct"].to_numpy(dtype=float)
    print("\n================ نتيجة إعادة التشغيل على الصفقات الحقيقية ================")
    print(f"عدد الصفقات المُعاد تشغيلها: {len(out)}")
    print(f"{'':28}{'القاعدة القديمة':>18}{'الوقف المتحرك':>18}")
    print(f"{'نسبة النجاح':28}{(old > 0).mean() * 100:17.1f}%{(new > 0).mean() * 100:17.1f}%")
    print(f"{'متوسط الصفقة':28}{old.mean():17.3f}%{new.mean():17.3f}%")
    print(f"{'الإجمالي':28}{old.sum():17.1f}%{new.sum():17.1f}%")
    print(f"{'متوسط الرابحة':28}{old[old > 0].mean():17.3f}%{new[new > 0].mean():17.3f}%")
    print(f"{'متوسط الخاسرة':28}{old[old <= 0].mean():17.3f}%{new[new <= 0].mean():17.3f}%")
    print(f"{'الوسيط':28}{np.median(old):17.3f}%{np.median(new):17.3f}%")

    reached = out[out["rule_peak_pct"] >= 2.0]
    print(f"\nبلغت الهدف المرجعي +2% أثناء الصفقة: {len(reached)} من {len(out)}")
    if len(reached):
        print(f"  • منها انتهت ربحًا بالقاعدة الجديدة: {(reached['rule_net_pct'] > 0).sum()}")
        print(f"  • منها انتهت خسارة بعد بلوغ الهدف: {(reached['rule_net_pct'] <= 0).sum()}")
        print(f"  • القاعدة القديمة كانت ستغلق هذه الـ{len(reached)} كلها بربح +1.8% صافي")
    print(f"\nالصفقات التي لم تُغلق خلال 14 يومًا: {(out['rule_outcome'] == 'open_at_horizon').sum()}")
    print(f"\nالمخرجات الكاملة: {out_path}")


if __name__ == "__main__":
    main()
