"""تقرير آخر إشارات Target Trend على كل أزواج USDT الفورية (فريم 1H).

الاستخدام:
    python tools/last_signals.py                 # مسح آخر 24 ساعة (طباعة فقط)
    python tools/last_signals.py --hours 48      # نافذة أوسع
    python tools/last_signals.py --top 5         # أهم الإشارات فقط عند الطباعة

إرسالها إلى تيليجرام يتطلب TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID في البيئة
مع --send (يستخدم نفس تنسيق رسائل البوت).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from app import EXCLUDE_PEGGED_STABLES, is_pegged_stable_symbol
from src.binance_client import BinanceClient
from src.config import AppConfig
from src.strategy import StrategySettings, compute_trend_frame
from src.utils import format_price, ms_to_local_text

HOUR_MS = 60 * 60 * 1000


def scan(hours: int) -> tuple[list[dict], int, int, int]:
    """يعيد (قائمة الإشارات، وقت إغلاق أحدث شمعة، عدد الرموز، عدد الرموز التي تحمل إشارة)."""
    cfg = AppConfig(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "unused"),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "unused"),
    )
    client = BinanceClient(
        timeout=cfg.request_timeout, max_workers=cfg.max_workers, base_url=cfg.binance_base_url
    )
    settings = StrategySettings()

    all_symbols = client.get_spot_usdt_symbols(cfg.quote_asset)
    if EXCLUDE_PEGGED_STABLES:
        symbols = [s for s in all_symbols if not is_pegged_stable_symbol(s, cfg.quote_asset)]
    else:
        symbols = list(all_symbols)
    excluded = len(all_symbols) - len(symbols)
    print(f"Loaded {len(symbols)} spot symbols ({excluded} pegged-stable excluded)", flush=True)

    klines = client.get_klines_for_symbols(symbols, interval=cfg.interval, limit=cfg.kline_limit)
    non_empty = {s: df for s, df in klines.items() if df is not None and not df.empty}
    if not non_empty:
        raise SystemExit("لا توجد بيانات شموع إطلاقًا.")

    # استبعاد الشمعة الجارية (لم تُغلق بعد) — البوت لا يصدر إشارة إلا على شمعة مغلقة
    server_time_ms = client.get_server_time()
    non_empty = {
        s: df[df["close_time"] <= server_time_ms]
        for s, df in non_empty.items()
    }
    non_empty = {s: df for s, df in non_empty.items() if not df.empty}
    if not non_empty:
        raise SystemExit("لا توجد شموع مغلقة إطلاقًا.")

    last_close_ms = max(int(df.iloc[-1]["close_time"]) for df in non_empty.values())
    cutoff_ms = last_close_ms - hours * HOUR_MS

    signals: list[dict] = []
    with_trend = 0
    for symbol, df in non_empty.items():
        frame = compute_trend_frame(df, settings)
        frame = frame[frame["atr_value"].notna()]
        if frame.empty:
            continue
        with_trend += 1
        for kind in ("signal_up", "signal_down"):
            hits = frame[frame[kind]]
            if hits.empty:
                continue
            row = hits.iloc[-1]
            open_ms = int(row["open_time"])
            if open_ms < cutoff_ms:
                continue
            signals.append(
                {
                    "symbol": symbol,
                    "kind": kind,
                    "open_ms": open_ms,
                    "close_ms": int(row["close_time"]),
                    "entry_price": float(row["close"]) if kind == "signal_up" else float(row["close"]),
                    "stop_price": float(row["sma_low"]) if kind == "signal_up" else float(row["sma_low"]),
                    "atr_value": float(row["atr_value"]),
                    "trend": float(row["trend"]),
                }
            )

    signals.sort(key=lambda s: s["open_ms"], reverse=True)
    return signals, last_close_ms, len(symbols), with_trend


def build_messages(
    signals: list[dict], last_close_ms: int, hours: int, tz: str, scope_label: str | None = None
) -> list[str]:
    buys = [s for s in signals if s["kind"] == "signal_up"]
    sells = [s for s in signals if s["kind"] == "signal_down"]
    scope = scope_label or f"آخر {hours} ساعة"
    header = (
        f"🔍 تقرير مسح — آخر إشارات Target Trend\n"
        f"الفريم: 1H • النطاق: {scope}\n"
        f"أُغلقت الشمعة: {ms_to_local_text(last_close_ms, tz)}\n"
        f"إشارة شراء: {len(buys)} • إشارة بيع/إغلاق: {len(sells)}"
    )

    def fmt(sig: dict, idx: int) -> str:
        entry = sig["entry_price"]
        stop = sig["stop_price"]
        stop_pct = (stop / entry - 1.0) * 100.0
        atr = sig["atr_value"]
        mults = StrategySettings().target_multipliers
        targets = [entry + m * atr for m in mults]
        time_text = ms_to_local_text(sig["open_ms"], tz)
        if sig["kind"] == "signal_up":
            lines = [
                f"{idx}. 📥 {sig['symbol']} — شراء",
                f"   الدخول: {format_price(entry)} • الوقف: {format_price(stop)} ({stop_pct:.2f}%)",
                f"   الأهداف: {format_price(targets[0])} / {format_price(targets[1])} / {format_price(targets[2])}",
                f"   وقت الإشارة: {time_text}",
            ]
        else:
            lines = [
                f"{idx}. 📤 {sig['symbol']} — بيع/إغلاق",
                f"   السعر: {format_price(entry)}",
                f"   وقت الإشارة: {time_text}",
            ]
        return "\n".join(lines)

    messages = [header]
    if buys:
        body = [f"\n📥 إشارات الشراء ({len(buys)}):"]
        body += [fmt(s, i + 1) for i, s in enumerate(buys)]
        messages.append("\n".join(body))
    if sells:
        body = [f"\n📤 إشارات البيع/الإغلاق ({len(sells)}):"]
        body += [fmt(s, i + 1) for i, s in enumerate(sells)]
        messages.append("\n".join(body))
    if not buys and not sells:
        messages.append(f"\nلا توجد إشارات خلال آخر {hours} ساعة.")
    return messages


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--hour", type=int, default=None,
                        help="إعادة إشارات شمعة ساعة معينة بتوقيت التوقيت المحلي (0-23)")
    parser.add_argument("--send", action="store_true", help="أرسل الرسائل إلى تيليجرام")
    args = parser.parse_args()

    started = time.time()
    signals, last_close_ms, total, with_trend = scan(args.hours)
    print(f"scan took {time.time() - started:.1f}s • symbols with valid trend: {with_trend}/{total}", flush=True)

    tz = os.getenv("TIMEZONE", "Asia/Riyadh")
    if args.hour is not None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        def local_hour(ms: int) -> int:
            return datetime.fromtimestamp(ms / 1000, ZoneInfo(tz)).hour

        signals = [s for s in signals if local_hour(s["open_ms"]) == args.hour]
        print(f"signals on the {args.hour}:00 ({tz}) bar: {len(signals)}", flush=True)
    else:
        print(f"signals in last {args.hours}h: {len(signals)}", flush=True)

    scope_label = f"شمعة الساعة {args.hour}:00 بتوقيت {tz}" if args.hour is not None else None
    messages = build_messages(signals, last_close_ms, args.hours, tz, scope_label)
    for msg in messages:
        print("-" * 60)
        print(msg)

    if args.send:
        from src.telegram_client import TelegramClient

        cfg = AppConfig.from_env()
        tg = TelegramClient(cfg.telegram_bot_token, cfg.telegram_chat_id)
        for msg in messages:
            tg.send_message(msg)
        print(f"SENT {len(messages)} message(s) to Telegram.")


if __name__ == "__main__":
    main()
