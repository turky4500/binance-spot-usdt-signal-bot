from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src.binance_client import BinanceClient
from src.config import AppConfig
from src.halal import ensure_verdict, refresh_if_stale
from src.state import StateStore
from src.strategy import StrategySettings, compute_entry_signal
from src.telegram_client import TelegramClient
from src.utils import (
    format_price,
    humanize_duration_ar,
    local_date_key_from_ms,
    ms_to_local_text,
    weekday_ar_from_ms,
)

HOUR_MS = 60 * 60 * 1000
MINUTE_MS = 60 * 1000
EVENT_RETENTION_DAYS = 45


class SpotSignalBot:
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

    def refresh_symbols(self, force: bool = False) -> None:
        now = time.time()
        if not force and self.symbols and (now - self.last_symbols_refresh) < 6 * 60 * 60:
            return
        self.symbols = self.binance.get_spot_usdt_symbols(self.config.quote_asset)
        self.last_symbols_refresh = now
        self.logger.info("Loaded %s spot symbols with quote asset %s", len(self.symbols), self.config.quote_asset)

    def refresh_halal_verdicts(self) -> None:
        self.halal_verdicts = refresh_if_stale(
            self.data_dir,
            max_age_hours=self.config.halal_refresh_hours,
        )

    def get_halal_verdict(self, symbol: str) -> str:
        return ensure_verdict(self.data_dir, symbol, self.halal_verdicts)

    def append_event(self, event_type: str, symbol: str, event_time_ms: int) -> None:
        events = self.state.setdefault("event_log", [])
        events.append(
            {
                "type": event_type,
                "symbol": symbol,
                "time_ms": int(event_time_ms),
            }
        )
        cutoff_ms = int(event_time_ms) - (EVENT_RETENTION_DAYS * 24 * 60 * 60 * 1000)
        self.state["event_log"] = [e for e in events if int(e.get("time_ms", 0)) >= cutoff_ms]
        self.store.save(self.state)

    def send_entry_message(self, trade: dict) -> None:
        strength = "قوية" if trade.get("strong") else "عادية"
        verdict = trade.get("halal_verdict") or self.get_halal_verdict(trade["symbol"])
        text = (
            f"📥 إشارة دخول شراء\n"
            f"الزوج: {trade['symbol']}\n"
            f"الفريم: 1H\n"
            f"قوة الإشارة: {strength}\n"
            f"سعر الدخول: {format_price(trade['entry_price'])}\n"
            f"الهدف: {format_price(trade['target_price'])}\n"
            f"وقف الخسارة: {format_price(trade['stop_price'])}\n"
            f"وقت الإشارة: {ms_to_local_text(trade['entry_time'], self.config.timezone_name)}\n"
            f"─────────────\n"
            f"الحكم الشرعي: {verdict}"
        )
        self.telegram.send_message(text)
        self.append_event("entry", trade["symbol"], int(trade["entry_time"]))

    def send_target_message(self, trade: dict, hit_price: float, event_time_ms: int) -> None:
        duration = humanize_duration_ar(trade["entry_time"], event_time_ms)
        verdict = trade.get("halal_verdict") or self.get_halal_verdict(trade["symbol"])
        text = (
            f"✅ تم تحقيق الهدف\n"
            f"الزوج: {trade['symbol']}\n"
            f"سعر الدخول: {format_price(trade['entry_price'])}\n"
            f"سعر تحقيق الهدف: {format_price(hit_price)}\n"
            f"وقت تحقيق الهدف: {ms_to_local_text(event_time_ms, self.config.timezone_name)}\n"
            f"المدة: {duration}\n"
            f"─────────────\n"
            f"الحكم الشرعي: {verdict}"
        )
        self.telegram.send_message(text)
        self.append_event("target", trade["symbol"], int(event_time_ms))

    def send_stop_message(self, trade: dict, event_time_ms: int) -> None:
        duration = humanize_duration_ar(trade["entry_time"], event_time_ms)
        verdict = trade.get("halal_verdict") or self.get_halal_verdict(trade["symbol"])
        text = (
            f"🛑 تم تفعيل وقف الخسارة\n"
            f"الزوج: {trade['symbol']}\n"
            f"سعر الدخول: {format_price(trade['entry_price'])}\n"
            f"سعر وقف الخسارة: {format_price(trade['stop_price'])}\n"
            f"وقت التفعيل: {ms_to_local_text(event_time_ms, self.config.timezone_name)}\n"
            f"المدة: {duration}\n"
            f"السبب: إغلاق شمعة 1H أسفل وقف الخسارة\n"
            f"─────────────\n"
            f"الحكم الشرعي: {verdict}"
        )
        self.telegram.send_message(text)
        self.append_event("stop", trade["symbol"], int(event_time_ms))

    def close_trade(self, symbol: str, exit_bar_open_time: int, reason: str) -> None:
        self.state["open_trades"].pop(symbol, None)
        self.state["last_exit_bar_time"][symbol] = exit_bar_open_time
        self.store.save(self.state)
        self.logger.info("Closed %s بسبب %s", symbol, reason)

    def monitor_open_trades_intrabar_targets(self, now_ms: int) -> None:
        open_trades = self.state.get("open_trades", {})
        if not open_trades:
            return

        state_changed = False
        for symbol, trade in list(open_trades.items()):
            start_ms = int(trade.get("last_target_check_ms", int(trade["entry_time"]) + 1))
            if start_ms > now_ms:
                continue

            klines = self.binance.get_klines_range(
                symbol=symbol,
                interval="1m",
                start_time=start_ms,
                end_time=now_ms,
            )
            if klines.empty:
                continue

            hit_rows = klines[klines["high"] >= float(trade["target_price"])]
            if not hit_rows.empty:
                first_hit = hit_rows.iloc[0]
                event_time_ms = int(first_hit["close_time"])
                self.send_target_message(trade, float(trade["target_price"]), event_time_ms)
                self.close_trade(symbol, int(event_time_ms // HOUR_MS * HOUR_MS), "target_intrabar")
                continue

            trade["last_target_check_ms"] = int(klines.iloc[-1]["close_time"]) + 1
            state_changed = True

        if state_changed:
            self.store.save(self.state)

    def process_new_closed_hour(self, server_time: int) -> None:
        last_closed_open_time = ((server_time // HOUR_MS) - 1) * HOUR_MS
        if int(self.state.get("last_processed_open_time", 0)) >= last_closed_open_time:
            return

        self.logger.info("Processing closed hour at open_time=%s", last_closed_open_time)
        klines_map = self.binance.get_klines_for_symbols(self.symbols, self.config.interval, self.config.kline_limit)

        open_trades = self.state.get("open_trades", {})
        for symbol, trade in list(open_trades.items()):
            df = klines_map.get(symbol)
            if df is None or df.empty:
                continue
            candle = df[df["open_time"] == last_closed_open_time]
            if candle.empty:
                continue
            row = candle.iloc[-1]
            candle_high = float(row["high"])
            candle_close = float(row["close"])
            candle_close_time = int(row["close_time"])

            if candle_high >= float(trade["target_price"]):
                self.send_target_message(trade, float(trade["target_price"]), candle_close_time)
                self.close_trade(symbol, last_closed_open_time, "target_hour_recovery")
                continue

            if candle_close < float(trade["stop_price"]):
                self.send_stop_message(trade, candle_close_time)
                self.close_trade(symbol, last_closed_open_time, "stop_close")

        for symbol in self.symbols:
            if symbol in self.state["open_trades"]:
                continue

            df = klines_map.get(symbol)
            if df is None or df.empty:
                continue
            closed_df = df[df["open_time"] <= last_closed_open_time].copy()
            if closed_df.empty:
                continue
            closed_df["symbol"] = symbol
            last_bar_open_time = int(closed_df.iloc[-1]["open_time"])
            if last_bar_open_time != last_closed_open_time:
                continue

            signal = compute_entry_signal(
                closed_df,
                self.settings,
                has_open_trade=False,
                last_exit_bar_time=self.state["last_exit_bar_time"].get(symbol),
            )
            if not signal:
                continue
            if int(self.state["last_entry_bar_time"].get(symbol, 0)) == signal["bar_open_time"]:
                continue

            trade = {
                "symbol": symbol,
                "entry_price": signal["entry_price"],
                "target_price": signal["target_price"],
                "stop_price": signal["stop_price"],
                "entry_time": signal["bar_close_time"],
                "entry_bar_open_time": signal["bar_open_time"],
                "last_target_check_ms": signal["bar_close_time"] + MINUTE_MS,
                "strong": signal["strong"],
                "mode": signal["mode"],
                "halal_verdict": self.get_halal_verdict(symbol),
            }
            self.state["open_trades"][symbol] = trade
            self.state["last_entry_bar_time"][symbol] = signal["bar_open_time"]
            self.store.save(self.state)
            self.send_entry_message(trade)
            self.logger.info("New trade opened for %s", symbol)

        self.state["last_processed_open_time"] = last_closed_open_time
        self.store.save(self.state)

    def send_daily_report_if_due(self, now_ms: int) -> None:
        now_local = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(self.config.timezone_name))
        if now_local.hour != 0:
            return

        report_day = (now_local.date() - timedelta(days=1)).isoformat()
        report_state = self.state.setdefault("daily_report", {})
        if report_state.get("last_reported_for_date") == report_day:
            return

        entries = 0
        targets = 0
        stops = 0
        for event in self.state.get("event_log", []):
            event_time_ms = int(event.get("time_ms", 0))
            if local_date_key_from_ms(event_time_ms, self.config.timezone_name) != report_day:
                continue
            event_type = event.get("type")
            if event_type == "entry":
                entries += 1
            elif event_type == "target":
                targets += 1
            elif event_type == "stop":
                stops += 1

        open_count = len(self.state.get("open_trades", {}))
        closed_count = targets + stops
        success_rate = (targets / closed_count * 100.0) if closed_count else 0.0
        report_anchor_ms = int(datetime(now_local.year, now_local.month, now_local.day, tzinfo=ZoneInfo(self.config.timezone_name)).astimezone(timezone.utc).timestamp() * 1000)
        weekday_name = weekday_ar_from_ms(report_anchor_ms - 1000, self.config.timezone_name)

        text = (
            f"📊 التقرير اليومي للإشارات\n"
            f"🗓️ اليوم المشمول: {weekday_name} {report_day}\n"
            f"🕛 وقت التقرير: {ms_to_local_text(now_ms, self.config.timezone_name)}\n"
            f"─────────────\n"
            f"📥 عدد الصفقات المرسلة: {entries}\n"
            f"✅ عدد النجاح: {targets}\n"
            f"🛑 عدد الخسارة: {stops}\n"
            f"📌 عدد المفتوحة حاليًا: {open_count}\n"
            f"📈 نسبة النجاح: {success_rate:.1f}%"
        )
        self.telegram.send_message(text)
        report_state["last_reported_for_date"] = report_day
        self.store.save(self.state)
        self.logger.info("Daily report sent for %s", report_day)

    def run_cycle(self) -> None:
        self.refresh_symbols(force=not self.symbols)
        self.refresh_halal_verdicts()
        server_time = self.binance.get_server_time()
        self.monitor_open_trades_intrabar_targets(server_time)
        self.process_new_closed_hour(server_time)
        self.send_daily_report_if_due(server_time)

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
