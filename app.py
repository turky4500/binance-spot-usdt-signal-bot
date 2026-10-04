from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from src.binance_client import BinanceClient
from src.config import AppConfig
from src.state import StateStore
from src.strategy import StrategySettings, compute_entry_signal
from src.telegram_client import TelegramClient
from src.utils import format_price, humanize_duration_ar, ms_to_local_text

HOUR_MS = 60 * 60 * 1000


class SpotSignalBot:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = logging.getLogger("spot-signal-bot")
        self.binance = BinanceClient(timeout=config.request_timeout, max_workers=config.max_workers)
        self.telegram = TelegramClient(
            token=config.telegram_bot_token,
            chat_id=config.telegram_chat_id,
            timeout=config.request_timeout,
        )
        self.settings = StrategySettings()
        self.store = StateStore(config.state_file)
        self.state = self.store.load()
        self.symbols: list[str] = []
        self.last_symbols_refresh = 0.0

    def refresh_symbols(self, force: bool = False) -> None:
        now = time.time()
        if not force and self.symbols and (now - self.last_symbols_refresh) < 6 * 60 * 60:
            return
        self.symbols = self.binance.get_spot_usdt_symbols(self.config.quote_asset)
        self.last_symbols_refresh = now
        self.logger.info("Loaded %s spot symbols with quote asset %s", len(self.symbols), self.config.quote_asset)

    def send_entry_message(self, trade: dict) -> None:
        strength = "قوية" if trade.get("strong") else "عادية"
        text = (
            f"📥 إشارة دخول شراء\n"
            f"الزوج: {trade['symbol']}\n"
            f"الفريم: 1H\n"
            f"قوة الإشارة: {strength}\n"
            f"سعر الدخول: {format_price(trade['entry_price'])}\n"
            f"الهدف: {format_price(trade['target_price'])}\n"
            f"وقف الخسارة: {format_price(trade['stop_price'])}\n"
            f"وقت الإشارة: {ms_to_local_text(trade['entry_time'], self.config.timezone_name)}"
        )
        self.telegram.send_message(text)

    def send_target_message(self, trade: dict, hit_price: float, event_time_ms: int) -> None:
        duration = humanize_duration_ar(trade["entry_time"], event_time_ms)
        text = (
            f"✅ تم تحقيق الهدف\n"
            f"الزوج: {trade['symbol']}\n"
            f"سعر الدخول: {format_price(trade['entry_price'])}\n"
            f"سعر تحقيق الهدف: {format_price(hit_price)}\n"
            f"المدة: {duration}"
        )
        self.telegram.send_message(text)

    def send_stop_message(self, trade: dict, event_time_ms: int) -> None:
        duration = humanize_duration_ar(trade["entry_time"], event_time_ms)
        text = (
            f"🛑 تم تفعيل وقف الخسارة\n"
            f"الزوج: {trade['symbol']}\n"
            f"سعر الدخول: {format_price(trade['entry_price'])}\n"
            f"سعر وقف الخسارة: {format_price(trade['stop_price'])}\n"
            f"المدة: {duration}\n"
            f"السبب: إغلاق شمعة 1H أسفل وقف الخسارة"
        )
        self.telegram.send_message(text)

    def close_trade(self, symbol: str, exit_bar_open_time: int, reason: str) -> None:
        self.state["open_trades"].pop(symbol, None)
        self.state["last_exit_bar_time"][symbol] = exit_bar_open_time
        self.store.save(self.state)
        self.logger.info("Closed %s بسبب %s", symbol, reason)

    def monitor_open_trades_live(self) -> None:
        open_trades = self.state.get("open_trades", {})
        if not open_trades:
            return
        prices = self.binance.get_all_prices()
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        for symbol, trade in list(open_trades.items()):
            current_price = prices.get(symbol)
            if current_price is None:
                continue
            if current_price >= float(trade["target_price"]):
                self.send_target_message(trade, float(trade["target_price"]), now_ms)
                self.close_trade(symbol, int(now_ms // HOUR_MS * HOUR_MS), "target_live")

    def process_new_closed_hour(self) -> None:
        server_time = self.binance.get_server_time()
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
                "strong": signal["strong"],
                "mode": signal["mode"],
            }
            self.state["open_trades"][symbol] = trade
            self.state["last_entry_bar_time"][symbol] = signal["bar_open_time"]
            self.store.save(self.state)
            self.send_entry_message(trade)
            self.logger.info("New trade opened for %s", symbol)

        self.state["last_processed_open_time"] = last_closed_open_time
        self.store.save(self.state)

    def run(self) -> None:
        self.refresh_symbols(force=True)
        self.logger.info("Bot started.")
        while True:
            try:
                self.refresh_symbols()
                self.monitor_open_trades_live()
                self.process_new_closed_hour()
            except KeyboardInterrupt:
                raise
            except Exception:
                self.logger.exception("Unhandled error in main loop")
            time.sleep(self.config.poll_seconds)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, AppConfig.from_env().log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    config = AppConfig.from_env()
    Path(config.state_file).parent.mkdir(parents=True, exist_ok=True)
    bot = SpotSignalBot(config)
    bot.run()


if __name__ == "__main__":
    main()
