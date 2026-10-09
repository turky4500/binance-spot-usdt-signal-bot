from __future__ import annotations

import logging
import os
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from src.binance_client import BinanceClient
from src.config import AppConfig
from src.halal import ensure_verdict, refresh_if_stale
from src.state import StateStore
from src.strategy import StrategySettings, latest_signal
from src.telegram_client import TelegramClient
from src.trade_analysis import (
    avg_metric,
    build_daily_observations,
    is_loss,
    is_win,
    load_trade_rows,
    rows_for_entry_day,
    rows_for_entry_day_range,
    rows_for_exit_day,
    rows_for_exit_day_range,
    success_rate_percent,
    target_hit_stats,
    top_symbols,
)
from src.trade_journal import append_trade_entry, update_trade_exit
from src.utils import (
    format_price,
    humanize_duration_ar,
    local_date_key_from_ms,
    local_hour_from_ms,
    ms_to_local_text,
    weekday_ar_from_ms,
)

HOUR_MS = 60 * 60 * 1000
MINUTE_MS = 60 * 1000
EVENT_RETENTION_DAYS = 120
WEEKLY_REPORT_WEEKDAY = 6  # الأحد، حيث الاثنين = 0
# سجل إضافي (بُعيد) يُجلب فقط عند «uncertain»: تقاطع بلا تقاطع سابق معروف في النافذة
EXTENDED_KLINE_HOURS = 3000


# ─────────────────────────────────────────────────────────────────────────────
# استثناء أزواج العملات المستقرة المربوطة (هدف ATR عليها غير منطقي والضجيج يقتل الوقف)
# الإيقاف: EXCLUDE_PEGGED_STABLES=0
# ─────────────────────────────────────────────────────────────────────────────
EXCLUDE_PEGGED_STABLES = os.getenv("EXCLUDE_PEGGED_STABLES", "1").strip().lower() not in ("0", "false", "no", "off")

PEGGED_STABLE_BASES = frozenset({
    "USDC", "FDUSD", "TUSD", "RLUSD", "XUSD", "USD1", "USDE", "USDS", "BFUSD", "U", "EUR", "EURI",
    "BUSD", "USDP", "DAI", "PYUSD", "GUSD", "LUSD", "AEUR", "EURC", "USDY", "USDD", "USDF",
})


def is_pegged_stable_symbol(symbol: str, quote_asset: str = "USDT") -> bool:
    """هل الزوج مبني على عملة مستقرة مربوطة؟"""
    symbol = (symbol or "").upper()
    quote = (quote_asset or "USDT").upper()
    if not symbol.endswith(quote):
        return False
    return symbol[: -len(quote)] in PEGGED_STABLE_BASES


class SpotSignalBot:
    """بوت إشارات Target Trend [BigBeluga] — المؤشر الوحيد المعتمد.

    - شراء: عند إشارة signal_up (تقاطع close فوق sma_high)
    - الأهداف الثلاثة: رسائل تحقق عند لمسها (لا تُغلق الصفقة)
    - الوقف: لمس sma_low عند الدخول يُغلق الصفقة
    - البيع: إشارة signal_down (تقاطع close تحت sma_low) تُغلق الصفقة
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = logging.getLogger("spot-signal-bot")
        self.binance = BinanceClient(
            timeout=config.request_timeout,
            max_workers=config.max_workers,
            base_url=config.binance_base_url,
        )
        self.telegram = TelegramClient(
            token=config.telegram_bot_token,
            chat_id=config.telegram_chat_id,
            timeout=config.request_timeout,
        )
        self.settings = StrategySettings()
        self.store = StateStore(config.state_file)
        self.state = self.store.load()
        self.data_dir = str(Path(config.state_file).parent)
        self.halal_verdicts: dict[str, int] = {}
        self.symbols: list[str] = []
        self.last_symbols_refresh = 0.0

    # ─────────────────────────────────────────── البيانات الأساسية
    def refresh_symbols(self, force: bool = False) -> None:
        now = time.time()
        if not force and self.symbols and (now - self.last_symbols_refresh) < 6 * 60 * 60:
            return
        all_symbols = self.binance.get_spot_usdt_symbols(self.config.quote_asset)
        if EXCLUDE_PEGGED_STABLES:
            self.symbols = [s for s in all_symbols if not is_pegged_stable_symbol(s, self.config.quote_asset)]
        else:
            self.symbols = list(all_symbols)
        self.last_symbols_refresh = now
        excluded = len(all_symbols) - len(self.symbols)
        self.logger.info(
            "Loaded %s spot symbols with quote asset %s%s",
            len(self.symbols), self.config.quote_asset,
            f" (استُثني {excluded} زوجًا لعملات مستقرة مربوطة)" if excluded else "",
        )

    def refresh_halal_verdicts(self) -> None:
        self.halal_verdicts = refresh_if_stale(
            self.data_dir,
            max_age_hours=self.config.halal_refresh_hours,
        )

    def get_halal_verdict(self, symbol: str) -> str:
        return ensure_verdict(self.data_dir, symbol, self.halal_verdicts)

    def append_event(self, event_type: str, symbol: str, event_time_ms: int) -> None:
        events = self.state.setdefault("event_log", [])
        events.append({"type": event_type, "symbol": symbol, "time_ms": int(event_time_ms)})
        cutoff_ms = int(event_time_ms) - (EVENT_RETENTION_DAYS * 24 * 60 * 60 * 1000)
        self.state["event_log"] = [e for e in events if int(e.get("time_ms", 0)) >= cutoff_ms]
        self.store.save(self.state)

    # ─────────────────────────────────────────── دفتر الصفقات
    def _build_trade_log_record(self, trade: dict) -> dict[str, object]:
        entry_time_ms = int(trade["entry_time"])
        targets = list(trade.get("targets") or [None, None, None])
        targets = (targets + [None, None, None])[:3]
        hit = list(trade.get("hit") or [False, False, False])
        hit = (hit + [False, False, False])[:3]
        return {
            "trade_id": trade["trade_id"],
            "symbol": trade["symbol"],
            "entry_time_ms": entry_time_ms,
            "entry_time_local": ms_to_local_text(entry_time_ms, self.config.timezone_name),
            "entry_date_local": local_date_key_from_ms(entry_time_ms, self.config.timezone_name),
            "entry_weekday_ar": weekday_ar_from_ms(entry_time_ms, self.config.timezone_name),
            "entry_hour_local": local_hour_from_ms(entry_time_ms, self.config.timezone_name),
            "entry_price": trade["entry_price"],
            "stop_price": trade["stop_price"],
            "target1_price": targets[0] if targets[0] is not None else "",
            "target2_price": targets[1] if targets[1] is not None else "",
            "target3_price": targets[2] if targets[2] is not None else "",
            "atr_at_entry": trade.get("atr_at_entry", ""),
            "trend_length": trade.get("trend_length", self.settings.length),
            "hit_target1": int(bool(hit[0])),
            "hit_target2": int(bool(hit[1])),
            "hit_target3": int(bool(hit[2])),
            "targets_hit_count": int(sum(1 for h in hit if h)),
            "outcome": "",
            "exit_reason": "",
            "exit_time_ms": "",
            "exit_time_local": "",
            "exit_price": "",
            "duration_minutes": "",
            "duration_text": "",
            "gross_return_pct": "",
            "net_return_pct": "",
        }

    def _record_trade_exit(
        self, trade: dict, outcome: str, exit_reason: str, exit_time_ms: int, exit_price: float
    ) -> None:
        duration_minutes = max(int((int(exit_time_ms) - int(trade["entry_time"])) // 60000), 0)
        duration_text = humanize_duration_ar(int(trade["entry_time"]), int(exit_time_ms))
        gross_return_pct = ((float(exit_price) / float(trade["entry_price"])) - 1.0) * 100.0
        net_return_pct = gross_return_pct - (2.0 * self.settings.commission_per_side_pct)
        hit = list(trade.get("hit") or [False, False, False])
        hit = (hit + [False, False, False])[:3]
        update_trade_exit(
            self.data_dir,
            str(trade.get("trade_id")),
            {
                "outcome": outcome,
                "exit_reason": exit_reason,
                "exit_time_ms": int(exit_time_ms),
                "exit_time_local": ms_to_local_text(int(exit_time_ms), self.config.timezone_name),
                "exit_price": float(exit_price),
                "duration_minutes": duration_minutes,
                "duration_text": duration_text,
                "gross_return_pct": round(gross_return_pct, 6),
                "net_return_pct": round(net_return_pct, 6),
                "hit_target1": int(bool(hit[0])),
                "hit_target2": int(bool(hit[1])),
                "hit_target3": int(bool(hit[2])),
                "targets_hit_count": int(sum(1 for h in hit if h)),
            },
        )

    def sync_open_trades_to_journal(self) -> None:
        changed = False
        for symbol, trade in self.state.get("open_trades", {}).items():
            if not trade.get("trade_id"):
                trade["trade_id"] = f"{symbol}-{trade.get('entry_bar_open_time', trade.get('entry_time', ''))}"
                changed = True
            if "hit" not in trade or not isinstance(trade.get("hit"), list):
                trade["hit"] = [False, False, False]
                changed = True
            append_trade_entry(self.data_dir, self._build_trade_log_record(trade))
        if changed:
            self.store.save(self.state)

    # ─────────────────────────────────────────── رسائل تيليجرام
    def send_entry_message(self, trade: dict) -> None:
        verdict = trade.get("halal_verdict") or self.get_halal_verdict(trade["symbol"])
        entry = float(trade["entry_price"])
        stop = float(trade["stop_price"])
        stop_pct = (stop / entry - 1.0) * 100.0
        targets = list(trade.get("targets") or [])
        target_lines = []
        for i, tp in enumerate(targets, start=1):
            pct = (float(tp) / entry - 1.0) * 100.0
            target_lines.append(f"الهدف {i}: {format_price(float(tp))} (+{pct:.2f}%)")

        text = (
            f"📥 إشارة شراء — Target Trend\n"
            f"الزوج: {trade['symbol']}\n"
            f"الفريم: 1H\n"
            f"سعر الدخول: {format_price(entry)}\n"
            + ("\n".join(target_lines) + "\n" if target_lines else "")
            + f"وقف الخسارة: {format_price(stop)} ({stop_pct:.2f}%)\n"
            f"وقت الإشارة: {ms_to_local_text(trade['entry_time'], self.config.timezone_name)}\n"
            f"─────────────\n"
            f"الحكم الشرعي: {verdict}"
        )
        self.telegram.send_message(text)
        self.append_event("entry", trade["symbol"], int(trade["entry_time"]))
        append_trade_entry(self.data_dir, self._build_trade_log_record(trade))

    def send_target_hit_message(self, trade: dict, level: int, target_price: float, event_time_ms: int) -> None:
        """رسالة تحقق أحد الأهداف الثلاثة — الصفقة تبقى مفتوحة."""
        verdict = trade.get("halal_verdict") or self.get_halal_verdict(trade["symbol"])
        entry = float(trade["entry_price"])
        gain_pct = (float(target_price) / entry - 1.0) * 100.0
        hit = list(trade.get("hit") or [False, False, False])
        hit_count = sum(1 for h in hit if h)
        duration = humanize_duration_ar(trade["entry_time"], event_time_ms)
        text = (
            f"🎯 تحقق الهدف {level} من 3\n"
            f"الزوج: {trade['symbol']}\n"
            f"سعر الدخول: {format_price(entry)}\n"
            f"سعر الهدف: {format_price(float(target_price))} (+{gain_pct:.2f}%)\n"
            f"الأهداف المحققة: {hit_count} من 3\n"
            f"وقت التحقق: {ms_to_local_text(event_time_ms, self.config.timezone_name)}\n"
            f"المدة المستغرقة: {duration}\n"
            f"─────────────\n"
            f"ℹ️ الصفقة مستمرة — الإغلاق بإشارة البيع أو الوقف\n"
            f"─────────────\n"
            f"الحكم الشرعي: {verdict}"
        )
        self.telegram.send_message(text)
        self.append_event("target", trade["symbol"], int(event_time_ms))

    def send_close_message(
        self, trade: dict, exit_price: float, event_time_ms: int, reason: str, reason_ar: str
    ) -> None:
        """رسالة انتهاء الصفقة: رابحة أم خاسرة + كم هدفًا حققت."""
        verdict = trade.get("halal_verdict") or self.get_halal_verdict(trade["symbol"])
        entry = float(trade["entry_price"])
        gross = (float(exit_price) / entry - 1.0) * 100.0
        net = gross - (2.0 * self.settings.commission_per_side_pct)
        won = net > 0
        emoji = "✅" if won else "🛑"
        headline = "انتهت الصفقة — رابحة 🎉" if won else "انتهت الصفقة — خاسرة"
        hit = list(trade.get("hit") or [False, False, False])
        hit = (hit + [False, False, False])[:3]
        hit_count = sum(1 for h in hit if h)
        hit_detail = " • ".join(
            f"الهدف {i}: {'✔' if h else '✖'}" for i, h in enumerate(hit, start=1)
        )
        peak = float(trade.get("peak_price", entry))
        peak_gain = (peak / entry - 1.0) * 100.0
        duration = humanize_duration_ar(trade["entry_time"], event_time_ms)

        text = (
            f"{emoji} {headline}\n"
            f"الزوج: {trade['symbol']}\n"
            f"سعر الدخول: {format_price(entry)}\n"
            f"سعر الخروج: {format_price(float(exit_price))}\n"
            f"النتيجة الصافية: {net:+.2f}%\n"
            f"أعلى سعر تحقق: {format_price(peak)} (+{peak_gain:.2f}%)\n"
            f"الأهداف المحققة: {hit_count} من 3 ({hit_detail})\n"
            f"سبب الإغلاق: {reason_ar}\n"
            f"وقت الإغلاق: {ms_to_local_text(event_time_ms, self.config.timezone_name)}\n"
            f"المدة المستغرقة: {duration}\n"
            f"─────────────\n"
            f"الحكم الشرعي: {verdict}"
        )
        self.telegram.send_message(text)
        self.append_event("exit_win" if won else "exit_loss", trade["symbol"], int(event_time_ms))
        self._record_trade_exit(trade, "win" if won else "loss", reason, int(event_time_ms), float(exit_price))

    def close_trade(self, symbol: str, exit_bar_open_time: int, reason: str) -> None:
        self.state["open_trades"].pop(symbol, None)
        self.state["last_exit_bar_time"][symbol] = exit_bar_open_time
        self.store.save(self.state)
        self.logger.info("Closed %s بسبب %s", symbol, reason)

    # ─────────────────────────────────────────── متابعة لحظية (شموع 1 دقيقة)
    def monitor_open_trades_intrabar(self, now_ms: int) -> None:
        """يتابع كل صفقة مفتوحة على شموع 1m: لمس الوقف يُغلق، ولمس الأهداف يُرسل."""
        open_trades = self.state.get("open_trades", {})
        if not open_trades:
            return

        state_changed = False
        for symbol, trade in list(open_trades.items()):
            start_ms = int(trade.get("last_target_check_ms", int(trade["entry_time"]) + 1))
            if start_ms > now_ms:
                continue

            klines = self.binance.get_klines_range(
                symbol=symbol, interval="1m", start_time=start_ms, end_time=now_ms,
            )
            if klines.empty:
                continue

            closed_by_stop = False
            for row in klines.itertuples(index=False):
                low = float(row.low)
                high = float(row.high)
                event_ms = int(row.close_time)

                # 1) الوقف أولًا (الاحتياط عند التداخل داخل الشمعة الواحدة)
                if low <= float(trade["stop_price"]):
                    self.send_close_message(
                        trade, float(trade["stop_price"]), event_ms,
                        "stop_loss", "لمس وقف الخسارة",
                    )
                    self.close_trade(symbol, event_ms // HOUR_MS * HOUR_MS, "stop_intrabar")
                    closed_by_stop = True
                    break

                # 2) الأهداف الثلاثة — رسائل تحقق دون إغلاق
                targets = list(trade.get("targets") or [])
                hit = list(trade.get("hit") or [False, False, False])
                hit = (hit + [False, False, False])[:3]
                for i, tp in enumerate(targets):
                    if i > 2 or hit[i]:
                        continue
                    if high >= float(tp):
                        hit[i] = True
                        trade["hit"] = hit
                        self.send_target_hit_message(trade, i + 1, float(tp), event_ms)

            if not closed_by_stop:
                trade["last_target_check_ms"] = int(klines.iloc[-1]["close_time"]) + 1
            peak = max(float(trade.get("peak_price", trade["entry_price"])), float(klines["high"].max()))
            trade["peak_price"] = peak
            state_changed = True

        if state_changed:
            self.store.save(self.state)

    # ─────────────────────────────────────────── معالجة الشمعة المغلقة
    def resolve_signal(self, symbol: str, closed_df: pd.DataFrame) -> dict | None:
        """إشارة شمعة مغلقة، مع استكشاف سجل أطول عند غموض حالة الاتجاه السابقة.

        Pine على شارت TradingView يملك تاريخًا كاملًا؛ نافذتنا 499 شمعة قد تبدأ
        بحالة trend=na. عند تقاطع بلا تقاطع سابق معروف نُعيد الحساب بسجل ~3000
        ساعة حتى تتأكد الحالة (وإلا أُعيد تطبيق منطق Pine: لا إشارة عند na).
        """
        signal = latest_signal(closed_df, self.settings)
        if not signal or signal.get("action") != "uncertain":
            return signal

        try:
            first_open = int(closed_df.iloc[0]["open_time"])
            extended = self.binance.get_klines_range(
                symbol=symbol,
                interval=self.config.interval,
                start_time=max(first_open - EXTENDED_KLINE_HOURS * HOUR_MS, 0),
                end_time=int(closed_df.iloc[-1]["close_time"]),
            )
            if extended is not None and not extended.empty and len(extended) > len(closed_df):
                signal = latest_signal(extended, self.settings)
                if signal and signal.get("action") == "uncertain":
                    # لا يوجد أي تقاطع سابق حتى في السجل الممتد — نتبع Pine: لا إشارة
                    return None
                return signal
        except Exception:
            self.logger.warning("Extended klines failed for %s; treating uncertain as no signal", symbol, exc_info=True)
        return None

    def process_new_closed_hour(self, server_time: int) -> None:
        self.purge_pegged_stable_trades()
        last_processed = int(self.state.get("last_processed_open_time", 0))

        klines_map = self.binance.get_klines_for_symbols(self.symbols, self.config.interval, self.config.kline_limit)
        try:
            sample = next(iter(klines_map.values()))
            if sample is None or sample.empty:
                self.logger.warning("No klines data; skipping cycle")
                return
            last_in_data = int(sample.iloc[-1]["open_time"])
            close_time_of_last = int(sample.iloc[-1].get("close_time", last_in_data + HOUR_MS - 1))
        except StopIteration:
            return

        # الشمعة المغلقة فعلًا = آخر شمعة انتهى زمنها
        if close_time_of_last >= server_time:
            last_in_data = last_in_data - HOUR_MS

        target_closed = last_in_data
        if last_processed >= target_closed:
            return

        self.logger.info(
            "Processing closed hour: target=%s (last_processed=%s)", target_closed, last_processed
        )

        # catch-up: عالج كل الشموع منذ آخر معالجة (حد أقصى 24)
        if last_processed == 0:
            cursor = target_closed
        else:
            cursor = max(last_processed + HOUR_MS, target_closed - 23 * HOUR_MS)
        if cursor > target_closed:
            cursor = last_processed + HOUR_MS
        while cursor <= target_closed:
            self._process_one_closed_hour(klines_map, cursor)
            self.state["last_processed_open_time"] = cursor
            self.store.save(self.state)
            cursor += HOUR_MS

    def _process_one_closed_hour(self, klines_map: dict, last_closed_open_time: int) -> None:
        """شمعة واحدة مغلقة: إغلاق المفتوحة (وقف/أهداف/إشارة بيع) ثم فتح الجديد."""
        open_trades = self.state.get("open_trades", {})

        # ── 1) متابعة الصفقات المفتوحة ──
        for symbol, trade in list(open_trades.items()):
            df = klines_map.get(symbol)
            if df is None or df.empty:
                continue
            candle = df[df["open_time"] == last_closed_open_time]
            closed_df = df[df["open_time"] <= last_closed_open_time]
            if candle.empty or closed_df.empty:
                continue
            row = candle.iloc[-1]
            candle_high = float(row["high"])
            candle_low = float(row["low"])
            candle_close_time = int(row["close_time"])

            peak_price = max(float(trade.get("peak_price", trade["entry_price"])), candle_high)
            trade["peak_price"] = peak_price

            # 1a) وقف الخسارة (احتياطي إن فاتت المتابعة اللحظية)
            if candle_low <= float(trade["stop_price"]):
                self.send_close_message(
                    trade, float(trade["stop_price"]), candle_close_time,
                    "stop_loss", "إغلاق شمعة 1H عند/أسفل وقف الخسارة",
                )
                self.close_trade(symbol, last_closed_open_time, "stop_hour")
                continue

            # 1b) أهداف لم تُرصد لحظيًا (احتياطي)
            targets = list(trade.get("targets") or [])
            hit = list(trade.get("hit") or [False, False, False])
            hit = (hit + [False, False, False])[:3]
            for i, tp in enumerate(targets):
                if i > 2 or hit[i]:
                    continue
                if candle_high >= float(tp):
                    hit[i] = True
                    trade["hit"] = hit
                    self.send_target_hit_message(trade, i + 1, float(tp), candle_close_time)

            # 1c) إشارة البيع = إغلاق الصفقة
            signal = self.resolve_signal(symbol, closed_df)
            if signal and signal["action"] == "sell":
                self.send_close_message(
                    trade, float(signal["price"]), candle_close_time,
                    "signal_down", "إشارة بيع (تقاطع تحت خط الاتجاه)",
                )
                self.close_trade(symbol, last_closed_open_time, "signal_down")
                continue

        # ── 2) إشارات دخول جديدة ──
        for symbol in self.symbols:
            if symbol in self.state["open_trades"]:
                continue
            df = klines_map.get(symbol)
            if df is None or df.empty:
                continue
            closed_df = df[df["open_time"] <= last_closed_open_time].copy()
            if closed_df.empty:
                continue
            if int(closed_df.iloc[-1]["open_time"]) != last_closed_open_time:
                continue

            signal = self.resolve_signal(symbol, closed_df)
            if not signal or signal["action"] != "buy":
                continue
            if int(self.state["last_entry_bar_time"].get(symbol, 0)) == signal["bar_open_time"]:
                continue

            trade = {
                "trade_id": f"{symbol}-{signal['bar_open_time']}",
                "symbol": symbol,
                "entry_price": signal["entry_price"],
                "stop_price": signal["stop_price"],
                "targets": list(signal["targets"]),
                "hit": [False, False, False],
                "entry_time": signal["bar_close_time"],
                "entry_bar_open_time": signal["bar_open_time"],
                "last_target_check_ms": signal["bar_close_time"] + MINUTE_MS,
                "atr_at_entry": signal["atr_value"],
                "trend_length": signal["trend_length"],
                "peak_price": signal["entry_price"],
                "halal_verdict": self.get_halal_verdict(symbol),
            }
            self.state["open_trades"][symbol] = trade
            self.state["last_entry_bar_time"][symbol] = signal["bar_open_time"]
            self.store.save(self.state)
            self.send_entry_message(trade)
            self.logger.info("New trade opened for %s @ %s", symbol, signal["entry_price"])

    def purge_pegged_stable_trades(self) -> None:
        """يحذف أي صفقة لعملة مستقرة مربوطة تسللت (هدف ATR عليها غير منطقي)."""
        open_trades = self.state.get("open_trades", {})
        doomed = [s for s in list(open_trades) if is_pegged_stable_symbol(s, self.config.quote_asset)]
        if not doomed:
            return
        for symbol in doomed:
            trade = open_trades.pop(symbol, {}) or {}
            self.state.get("last_entry_bar_time", {}).pop(symbol, None)
            trade_id = str(trade.get("trade_id") or "")
            if trade_id:
                update_trade_exit(self.data_dir, trade_id, {
                    "outcome": "removed",
                    "exit_reason": "removed_pegged_stable",
                    "exit_time_ms": int(trade.get("entry_time") or 0),
                    "exit_price": trade.get("entry_price"),
                    "duration_minutes": 0,
                    "duration_text": "أُزيلت (عملة مستقرة مربوطة)",
                })
            self.append_event("removed_pegged", symbol, int(trade.get("entry_time") or 0))

    # ─────────────────────────────────────────── التقارير
    def send_daily_report_if_due(self, now_ms: int) -> None:
        now_local = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(self.config.timezone_name))
        if now_local.hour != 0:
            return

        report_day = (now_local.date() - timedelta(days=1)).isoformat()
        report_state = self.state.setdefault("daily_report", {})
        if report_state.get("last_reported_for_date") == report_day:
            return

        entries = targets = wins = losses = 0
        for event in self.state.get("event_log", []):
            event_time_ms = int(event.get("time_ms", 0))
            if local_date_key_from_ms(event_time_ms, self.config.timezone_name) != report_day:
                continue
            event_type = event.get("type")
            if event_type == "entry":
                entries += 1
            elif event_type == "target":
                targets += 1
            elif event_type == "exit_win":
                wins += 1
            elif event_type == "exit_loss":
                losses += 1

        open_count = len(self.state.get("open_trades", {}))
        closed_count = wins + losses
        success_rate = (wins / closed_count * 100.0) if closed_count else 0.0
        report_anchor_ms = int(
            datetime(now_local.year, now_local.month, now_local.day, tzinfo=ZoneInfo(self.config.timezone_name))
            .astimezone(timezone.utc).timestamp() * 1000
        )
        weekday_name = weekday_ar_from_ms(report_anchor_ms - 1000, self.config.timezone_name)

        text = (
            f"📊 التقرير اليومي للإشارات\n"
            f"🗓️ اليوم المشمول: {weekday_name} {report_day}\n"
            f"🕛 وقت التقرير: {ms_to_local_text(now_ms, self.config.timezone_name)}\n"
            f"🧭 المؤشر: {self.settings.describe()}\n"
            f"─────────────\n"
            f"📥 صفقات الدخول: {entries}\n"
            f"🎯 أهداف تحققت: {targets}\n"
            f"✅ صفقات أُغلقت رابحة: {wins}\n"
            f"🛑 صفقات أُغلقت خاسرة: {losses}\n"
            f"📌 مفتوحة حاليًا: {open_count}\n"
            f"📈 نسبة النجاح: {success_rate:.1f}%"
        )
        self.telegram.send_message(text)
        report_state["last_reported_for_date"] = report_day
        self.store.save(self.state)
        self.logger.info("Daily report sent for %s", report_day)

    def send_daily_analysis_if_due(self, now_ms: int) -> None:
        now_local = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(self.config.timezone_name))
        if now_local.hour != 0:
            return

        report_day = (now_local.date() - timedelta(days=1)).isoformat()
        analysis_state = self.state.setdefault("daily_analysis", {})
        if analysis_state.get("last_reported_for_date") == report_day:
            return

        rows = load_trade_rows(self.data_dir)
        entry_rows = rows_for_entry_day(rows, report_day)
        closed_rows = rows_for_exit_day(rows, report_day, self.config.timezone_name)
        win_rows = [row for row in closed_rows if is_win(row.get("outcome"))]
        loss_rows = [row for row in closed_rows if is_loss(row.get("outcome"))]

        wins, losses = len(win_rows), len(loss_rows)
        closed_count = wins + losses
        open_count = len(self.state.get("open_trades", {}))
        success_rate = success_rate_percent(wins, losses)
        observations = build_daily_observations(win_rows, loss_rows, closed_rows)

        avg_net = avg_metric(closed_rows, "net_return_pct")
        hits = target_hit_stats(closed_rows)
        all_hits = target_hit_stats(rows)

        report_anchor_ms = int(
            datetime(now_local.year, now_local.month, now_local.day, tzinfo=ZoneInfo(self.config.timezone_name))
            .astimezone(timezone.utc).timestamp() * 1000
        )
        weekday_name = weekday_ar_from_ms(report_anchor_ms - 1000, self.config.timezone_name)

        lines = [
            "🧠 التحليل اليومي للإشارات",
            f"🗓️ اليوم المشمول: {weekday_name} {report_day}",
            f"🕛 وقت التحليل: {ms_to_local_text(now_ms, self.config.timezone_name)}",
            f"🧭 المؤشر: {self.settings.describe()}",
            "═════════════",
            f"📥 صفقات دخلت اليوم: {len(entry_rows)}",
            f"✅ أُغلقت رابحة: {wins} • 🛑 خاسرة: {losses} • 📌 ما زالت مفتوحة: {open_count}",
            f"📈 نسبة النجاح للمغلقة: {success_rate:.1f}%",
            f"💰 متوسط نتيجة الصفقة المغلقة: {f'{avg_net:+.2f}%' if avg_net is not None else '—'}",
            "═════════════",
            f"🏆 أكثر العملات نجاحًا: {top_symbols(win_rows, 'لا توجد صفقات رابحة اليوم')}",
            f"⚠️ أكثر العملات خسارة: {top_symbols(loss_rows, 'لا توجد صفقات خاسرة اليوم')}",
            "═════════════",
            f"🎯 الأهداف (صفقات مغلقة اليوم): الهدف 1: {hits['t1']} • الهدف 2: {hits['t2']} • الهدف 3: {hits['t3']}",
            f"🎯 الأهداف (كل الصفقات): الهدف 1: {all_hits['t1']} • الهدف 2: {all_hits['t2']} • الهدف 3: {all_hits['t3']}",
            "═════════════",
            "📝 ملاحظات تحليلية:",
        ]
        lines.extend([f"• {note}" for note in observations])
        if closed_count == 0:
            lines.append("• لا توجد صفقات مغلقة كافية لهذا اليوم، التحليل النوعي يتراكم مع الأيام.")

        self.telegram.send_message("\n".join(lines))
        analysis_state["last_reported_for_date"] = report_day
        self.store.save(self.state)
        self.logger.info("Daily analysis sent for %s", report_day)

    @staticmethod
    def _exit_duration_summary(rows: list[dict]) -> dict:
        """متوسط/أسرع/أبطأ مدة حتى الإغلاق — بالساعات، من دفتر الصفقات."""
        durations = []
        for row in rows:
            minutes = None
            try:
                minutes = float(row.get("duration_minutes"))
            except (TypeError, ValueError):
                minutes = None
            if minutes is None or minutes <= 0:
                try:
                    start_ms = int(float(row.get("entry_time_ms") or 0))
                    end_ms = int(float(row.get("exit_time_ms") or 0))
                except (TypeError, ValueError):
                    continue
                if start_ms > 0 and end_ms > start_ms:
                    minutes = (end_ms - start_ms) / 60_000.0
                else:
                    continue
            durations.append(minutes / 60.0)
        if not durations:
            return {"count": 0, "avg_h": None, "min_h": None, "max_h": None}
        return {
            "count": len(durations),
            "avg_h": sum(durations) / len(durations),
            "min_h": min(durations),
            "max_h": max(durations),
        }

    @staticmethod
    def _ar_count(n: int, one: str, two: str, few: str, many: str) -> str:
        if n == 1:
            return one
        if n == 2:
            return two
        if 3 <= n <= 10:
            return f"{n} {few}"
        return f"{n} {many}"

    @classmethod
    def _duration_h_text(cls, hours: float | None) -> str:
        if hours is None:
            return "—"
        total_minutes = int(round(hours * 60))
        if total_minutes < 60:
            return cls._ar_count(total_minutes, "دقيقة", "دقيقتان", "دقائق", "دقيقة")
        h, m = divmod(total_minutes, 60)
        if h < 24:
            text = cls._ar_count(h, "ساعة", "ساعتان", "ساعات", "ساعة")
            if m:
                text += " و" + cls._ar_count(m, "دقيقة", "دقيقتان", "دقائق", "دقيقة")
            return text
        d, h = divmod(h, 24)
        text = cls._ar_count(d, "يوم", "يومان", "أيام", "يومًا")
        if h:
            text += " و" + cls._ar_count(h, "ساعة", "ساعتان", "ساعات", "ساعة")
        return text

    @staticmethod
    def _format_ranked_symbols(counts: Counter, empty_text: str) -> str:
        if not counts:
            return empty_text
        top = counts.most_common(3)
        return " • ".join(f"{symbol} ({count})" for symbol, count in top)

    def send_weekly_report_if_due(self, now_ms: int) -> None:
        now_local = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(self.config.timezone_name))
        if now_local.hour != 0 or now_local.weekday() != WEEKLY_REPORT_WEEKDAY:
            return

        report_end_date = now_local.date() - timedelta(days=1)
        report_start_date = report_end_date - timedelta(days=6)
        report_start_key = report_start_date.isoformat()
        report_end_key = report_end_date.isoformat()

        weekly_state = self.state.setdefault("weekly_report", {})
        if weekly_state.get("last_reported_week_start") == report_start_key:
            return

        entries = targets = wins_events = losses_events = 0
        target_symbols: Counter = Counter()
        stop_symbols: Counter = Counter()

        for event in self.state.get("event_log", []):
            event_time_ms = int(event.get("time_ms", 0))
            event_day = local_date_key_from_ms(event_time_ms, self.config.timezone_name)
            if not (report_start_key <= event_day <= report_end_key):
                continue
            event_type = event.get("type")
            symbol = str(event.get("symbol") or "")
            if event_type == "entry":
                entries += 1
            elif event_type == "target":
                targets += 1
            elif event_type == "exit_win":
                wins_events += 1
                if symbol:
                    target_symbols[symbol] += 1
            elif event_type == "exit_loss":
                losses_events += 1
                if symbol:
                    stop_symbols[symbol] += 1

        closed_count = wins_events + losses_events
        success_rate = (wins_events / closed_count * 100.0) if closed_count else 0.0
        open_count = len(self.state.get("open_trades", {}))

        tz = ZoneInfo(self.config.timezone_name)
        start_anchor_ms = int(datetime(report_start_date.year, report_start_date.month, report_start_date.day, tzinfo=tz).astimezone(timezone.utc).timestamp() * 1000)
        end_anchor_ms = int(datetime(report_end_date.year, report_end_date.month, report_end_date.day, tzinfo=tz).astimezone(timezone.utc).timestamp() * 1000)
        start_weekday = weekday_ar_from_ms(start_anchor_ms, self.config.timezone_name)
        end_weekday = weekday_ar_from_ms(end_anchor_ms, self.config.timezone_name)

        # ---- تحليل من دفتر الصفقات ----
        rows = load_trade_rows(self.data_dir)
        entry_rows = rows_for_entry_day_range(rows, report_start_key, report_end_key)
        closed_rows = rows_for_exit_day_range(rows, report_start_key, report_end_key, self.config.timezone_name)
        win_rows = [row for row in closed_rows if is_win(row.get("outcome"))]
        loss_rows = [row for row in closed_rows if is_loss(row.get("outcome"))]
        file_rate = success_rate_percent(len(win_rows), len(loss_rows))
        nets = [float(r["net_return_pct"]) for r in closed_rows if r.get("net_return_pct") not in (None, "")]
        avg_net = (sum(nets) / len(nets)) if nets else None
        hits = target_hit_stats(closed_rows)
        target_dur = self._exit_duration_summary(win_rows)
        stop_dur = self._exit_duration_summary(loss_rows)

        text = (
            f"🗂️ التقرير الأسبوعي للإشارات\n"
            f"🗓️ الفترة: {start_weekday} {report_start_key} ← {end_weekday} {report_end_key}\n"
            f"🕛 وقت التقرير: {ms_to_local_text(now_ms, self.config.timezone_name)}\n"
            f"🧭 المؤشر: {self.settings.describe()}\n"
            f"═════════════\n"
            f"📥 صفقات الدخول: {entries}\n"
            f"🎯 أهداف تحققت: {targets}\n"
            f"✅ رابحة: {wins_events} • 🛑 خاسرة: {losses_events} • 📌 مفتوحة حاليًا: {open_count}\n"
            f"📈 نسبة النجاح: {success_rate:.1f}%\n"
            f"═════════════\n"
            f"📒 تحليل دفتر الصفقات\n"
            f"   • دخلت هذا الأسبوع: {len(entry_rows)} • أُغلقت: {len(closed_rows)}\n"
            f"   • نسبة نجاح المغلقة: {file_rate:.1f}%\n"
            + (
                f"   • متوسط نتيجة الصفقة: {avg_net:+.2f}% (أفضل {max(nets):+.2f}% • أسوأ {min(nets):+.2f}%)\n"
                if nets else "   • متوسط نتيجة الصفقة: —\n"
            )
            + f"   • الأهداف المحققة: الهدف 1: {hits['t1']} • الهدف 2: {hits['t2']} • الهدف 3: {hits['t3']}\n"
            + "═════════════\n"
            f"⏱️ المدد\n"
            f"   • متوسط مدة الرابحة: {self._duration_h_text(target_dur['avg_h'])}"
            + (f" (أسرع {self._duration_h_text(target_dur['min_h'])} • أبطأ {self._duration_h_text(target_dur['max_h'])})\n" if target_dur["count"] else "\n")
            + f"   • متوسط مدة الخاسرة: {self._duration_h_text(stop_dur['avg_h'])}"
            + (f" (أسرع {self._duration_h_text(stop_dur['min_h'])} • أبطأ {self._duration_h_text(stop_dur['max_h'])})\n" if stop_dur["count"] else "\n")
            + "═════════════\n"
            f"🏆 أكثر العملات ربحًا: {self._format_ranked_symbols(target_symbols, 'لا توجد صفقات رابحة هذا الأسبوع')}\n"
            f"⚠️ أكثر العملات خسارة: {self._format_ranked_symbols(stop_symbols, 'لا توجد صفقات خاسرة هذا الأسبوع')}"
        )
        self.telegram.send_message(text)
        weekly_state["last_reported_week_start"] = report_start_key
        self.store.save(self.state)
        self.logger.info("Weekly report sent for %s -> %s", report_start_key, report_end_key)

    # ─────────────────────────────────────────── التشغيل
    def run_cycle(self) -> None:
        self.refresh_symbols(force=not self.symbols)
        self.refresh_halal_verdicts()
        self.sync_open_trades_to_journal()
        server_time = self.binance.get_server_time()
        self.monitor_open_trades_intrabar(server_time)
        self.process_new_closed_hour(server_time)
        self.send_daily_report_if_due(server_time)
        self.send_daily_analysis_if_due(server_time)
        self.send_weekly_report_if_due(server_time)

    def run(self) -> None:
        self.refresh_symbols(force=True)
        self.logger.info("Bot started.")
        while True:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                raise
            except Exception:
                self.logger.exception("Unhandled error in main loop")
            time.sleep(self.config.poll_seconds)

    def run_once(self) -> None:
        self.refresh_symbols(force=True)
        self.refresh_halal_verdicts()
        self.logger.info("Bot one-shot run started.")
        self.run_cycle()
        self.logger.info("Bot one-shot run completed.")


def build_bot() -> SpotSignalBot:
    config = AppConfig.from_env()
    Path(config.state_file).parent.mkdir(parents=True, exist_ok=True)
    return SpotSignalBot(config)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, AppConfig.from_env().log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    bot = build_bot()
    bot.run()


if __name__ == "__main__":
    main()
