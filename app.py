from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src.binance_client import BinanceClient
from src.config import AppConfig
from src.halal import ensure_verdict, refresh_if_stale
from src.state import StateStore
from src.strategy import StrategySettings, compute_entry_signal
from src.telegram_client import TelegramClient
from src.trade_analysis import (
    avg_metric,
    build_daily_observations,
    load_trade_rows,
    rows_for_entry_day,
    rows_for_exit_day,
    strong_vs_normal_stats,
    success_rate_percent,
    top_symbols,
)
from src.trade_journal import append_trade_entry, update_trade_exit
from src.utils import (
    format_price,
    humanize_duration_ar,
    local_date_key_from_ms,
    ms_to_local_text,
    weekday_ar_from_ms,
)

HOUR_MS = 60 * 60 * 1000
MINUTE_MS = 60 * 1000
EVENT_RETENTION_DAYS = 120
WEEKLY_REPORT_WEEKDAY = 6  # الأحد، حيث الاثنين = 0


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

    def _format_ranked_symbols(self, counts: Counter, empty_text: str) -> str:
        if not counts:
            return empty_text
        top = counts.most_common(3)
        return " • ".join(f"{symbol} ({count})" for symbol, count in top)

    def _build_trade_log_record(self, trade: dict) -> dict[str, object]:
        entry_time_ms = int(trade["entry_time"])
        metrics = trade.get("metrics", {})
        entry_local_text = ms_to_local_text(entry_time_ms, self.config.timezone_name)
        entry_date = local_date_key_from_ms(entry_time_ms, self.config.timezone_name)
        weekday = weekday_ar_from_ms(entry_time_ms, self.config.timezone_name)
        return {
            "trade_id": trade["trade_id"],
            "symbol": trade["symbol"],
            "entry_time_ms": entry_time_ms,
            "entry_time_local": entry_local_text,
            "entry_date_local": entry_date,
            "entry_weekday_ar": weekday,
            "entry_price": trade["entry_price"],
            "target_price": trade["target_price"],
            "stop_price": trade["stop_price"],
            "strong_signal": int(bool(trade.get("strong"))),
            "buy_score": trade.get("buy_score", ""),
            "mode": trade.get("mode", ""),
            "rsi": metrics.get("rsi", ""),
            "stoch": metrics.get("stoch", ""),
            "adx": metrics.get("adx", ""),
            "plus_di": metrics.get("plus_di", ""),
            "minus_di": metrics.get("minus_di", ""),
            "relative_volume": metrics.get("relative_volume", ""),
            "reward_risk_ratio": metrics.get("reward_risk_ratio", ""),
            "buy_risk_pct": metrics.get("buy_risk_pct", ""),
            "distance_from_ema200_pct": metrics.get("distance_from_ema200_pct", ""),
            "quote_volume": metrics.get("quote_volume", ""),
            "bullish_divergence": int(bool(metrics.get("bullish_divergence", False))),
            "oversold_at_pivot": int(bool(metrics.get("oversold_at_pivot", False))),
            "volume_confirm": int(bool(metrics.get("volume_confirm", False))),
            "bullish_trend_ok": int(bool(metrics.get("bullish_trend_ok", False))),
            "adx_buy_ok": int(bool(metrics.get("adx_buy_ok", False))),
            "liquidity_ok": int(bool(metrics.get("liquidity_ok", False))),
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

    def _record_trade_exit(self, trade: dict, outcome: str, exit_reason: str, exit_time_ms: int, exit_price: float) -> None:
        trade_id = str(trade.get("trade_id") or f"{trade['symbol']}-{trade['entry_bar_open_time']}")
        duration_minutes = max(int((int(exit_time_ms) - int(trade["entry_time"])) // 60000), 0)
        duration_text = humanize_duration_ar(int(trade["entry_time"]), int(exit_time_ms))
        gross_return_pct = ((float(exit_price) / float(trade["entry_price"])) - 1.0) * 100.0
        net_return_pct = gross_return_pct - (2.0 * self.settings.commission_per_side_pct)
        update_trade_exit(
            self.data_dir,
            trade_id,
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
            },
        )

    def sync_open_trades_to_journal(self) -> None:
        changed = False
        for symbol, trade in self.state.get("open_trades", {}).items():
            if not trade.get("trade_id"):
                trade["trade_id"] = f"{symbol}-{trade.get('entry_bar_open_time', trade.get('entry_time', ''))}"
                changed = True
            append_trade_entry(self.data_dir, self._build_trade_log_record(trade))
        if changed:
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
        append_trade_entry(self.data_dir, self._build_trade_log_record(trade))

    def send_target_message(self, trade: dict, hit_price: float, event_time_ms: int) -> None:
        duration = humanize_duration_ar(trade["entry_time"], event_time_ms)
        verdict = trade.get("halal_verdict") or self.get_halal_verdict(trade["symbol"])
        text = (
            f"✅ تم تحقيق الهدف\n"
            f"الزوج: {trade['symbol']}\n"
            f"سعر الدخول: {format_price(trade['entry_price'])}\n"
            f"سعر تحقيق الهدف: {format_price(hit_price)}\n"
            f"وقت تحقيق الهدف: {ms_to_local_text(event_time_ms, self.config.timezone_name)}\n"
            f"المدة المستغرقة: {duration}\n"
            f"─────────────\n"
            f"الحكم الشرعي: {verdict}"
        )
        self.telegram.send_message(text)
        self.append_event("target", trade["symbol"], int(event_time_ms))
        self._record_trade_exit(trade, "target", "take_profit", int(event_time_ms), float(hit_price))

    def send_stop_message(self, trade: dict, event_time_ms: int) -> None:
        duration = humanize_duration_ar(trade["entry_time"], event_time_ms)
        verdict = trade.get("halal_verdict") or self.get_halal_verdict(trade["symbol"])
        text = (
            f"🛑 تم تفعيل وقف الخسارة\n"
            f"الزوج: {trade['symbol']}\n"
            f"سعر الدخول: {format_price(trade['entry_price'])}\n"
            f"سعر وقف الخسارة: {format_price(trade['stop_price'])}\n"
            f"وقت التفعيل: {ms_to_local_text(event_time_ms, self.config.timezone_name)}\n"
            f"المدة المستغرقة: {duration}\n"
            f"السبب: إغلاق شمعة 1H أسفل وقف الخسارة\n"
            f"─────────────\n"
            f"الحكم الشرعي: {verdict}"
        )
        self.telegram.send_message(text)
        self.append_event("stop", trade["symbol"], int(event_time_ms))
        self._record_trade_exit(trade, "stop", "stop_loss", int(event_time_ms), float(trade["stop_price"]))

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

            trade_id = f"{symbol}-{signal['bar_open_time']}"
            trade = {
                "trade_id": trade_id,
                "symbol": symbol,
                "entry_price": signal["entry_price"],
                "target_price": signal["target_price"],
                "stop_price": signal["stop_price"],
                "entry_time": signal["bar_close_time"],
                "entry_bar_open_time": signal["bar_open_time"],
                "last_target_check_ms": signal["bar_close_time"] + MINUTE_MS,
                "strong": signal["strong"],
                "buy_score": signal.get("buy_score"),
                "mode": signal["mode"],
                "metrics": signal.get("metrics", {}),
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
        win_rows = [row for row in closed_rows if row.get("outcome") == "target"]
        loss_rows = [row for row in closed_rows if row.get("outcome") == "stop"]

        wins = len(win_rows)
        losses = len(loss_rows)
        closed_count = wins + losses
        open_count = len(self.state.get("open_trades", {}))
        success_rate = success_rate_percent(wins, losses)
        strong_stats = strong_vs_normal_stats(closed_rows)
        observations = build_daily_observations(win_rows, loss_rows, closed_rows)

        avg_win_buy = avg_metric(win_rows, "buy_score")
        avg_loss_buy = avg_metric(loss_rows, "buy_score")
        avg_win_rvol = avg_metric(win_rows, "relative_volume")
        avg_loss_rvol = avg_metric(loss_rows, "relative_volume")

        report_anchor_ms = int(datetime(now_local.year, now_local.month, now_local.day, tzinfo=ZoneInfo(self.config.timezone_name)).astimezone(timezone.utc).timestamp() * 1000)
        weekday_name = weekday_ar_from_ms(report_anchor_ms - 1000, self.config.timezone_name)

        lines = [
            "🧠 التحليل اليومي للإشارات",
            f"🗓️ اليوم المشمول: {weekday_name} {report_day}",
            f"🕛 وقت التحليل: {ms_to_local_text(now_ms, self.config.timezone_name)}",
            "═════════════",
            f"📥 صفقات الدخول المسجلة: {len(entry_rows)}",
            f"✅ الصفقات المغلقة على الهدف: {wins}",
            f"🛑 الصفقات المغلقة على الوقف: {losses}",
            f"📌 ما زال مفتوحًا: {open_count}",
            f"📈 نسبة النجاح للصفقات المغلقة: {success_rate:.1f}%",
            "═════════════",
            f"🏆 أكثر العملات نجاحًا: {top_symbols(win_rows, 'لا توجد أهداف محققة اليوم')}",
            f"⚠️ أكثر العملات خسارة: {top_symbols(loss_rows, 'لا توجد صفقات خاسرة اليوم')}",
            f"💪 أداء الإشارات القوية: {float(strong_stats['strong_rate']):.1f}% من {int(strong_stats['strong_count'])} صفقة مغلقة",
            f"📎 أداء الإشارات العادية: {float(strong_stats['normal_rate']):.1f}% من {int(strong_stats['normal_count'])} صفقة مغلقة",
            "═════════════",
            f"📊 متوسط Buy Score — رابحة: {avg_win_buy:.2f}" if avg_win_buy is not None else "📊 متوسط Buy Score — رابحة: —",
            f"📊 متوسط Buy Score — خاسرة: {avg_loss_buy:.2f}" if avg_loss_buy is not None else "📊 متوسط Buy Score — خاسرة: —",
            f"🔊 متوسط الحجم النسبي — رابحة: {avg_win_rvol:.2f}x" if avg_win_rvol is not None else "🔊 متوسط الحجم النسبي — رابحة: —",
            f"🔊 متوسط الحجم النسبي — خاسرة: {avg_loss_rvol:.2f}x" if avg_loss_rvol is not None else "🔊 متوسط الحجم النسبي — خاسرة: —",
            "═════════════",
            "📝 ملاحظات تحليلية:",
        ]
        lines.extend([f"• {note}" for note in observations])
        if closed_count == 0:
            lines.append("• لا توجد صفقات مغلقة كافية لهذا اليوم بعد، لذلك التحليل النوعي ما زال محدودًا.")

        self.telegram.send_message("\n".join(lines))
        analysis_state["last_reported_for_date"] = report_day
        self.store.save(self.state)
        self.logger.info("Daily analysis sent for %s", report_day)

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

        entries = 0
        targets = 0
        stops = 0
        target_symbols: Counter[str] = Counter()
        stop_symbols: Counter[str] = Counter()

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
                if symbol:
                    target_symbols[symbol] += 1
            elif event_type == "stop":
                stops += 1
                if symbol:
                    stop_symbols[symbol] += 1

        closed_count = targets + stops
        success_rate = (targets / closed_count * 100.0) if closed_count else 0.0
        open_count = len(self.state.get("open_trades", {}))

        tz = ZoneInfo(self.config.timezone_name)
        start_anchor_ms = int(datetime(report_start_date.year, report_start_date.month, report_start_date.day, tzinfo=tz).astimezone(timezone.utc).timestamp() * 1000)
        end_anchor_ms = int(datetime(report_end_date.year, report_end_date.month, report_end_date.day, tzinfo=tz).astimezone(timezone.utc).timestamp() * 1000)
        start_weekday = weekday_ar_from_ms(start_anchor_ms, self.config.timezone_name)
        end_weekday = weekday_ar_from_ms(end_anchor_ms, self.config.timezone_name)

        best_symbols = self._format_ranked_symbols(target_symbols, "لا توجد أهداف محققة هذا الأسبوع")
        stop_symbols_text = self._format_ranked_symbols(stop_symbols, "لا توجد صفقات متوقفة هذا الأسبوع")

        text = (
            f"🗂️ التقرير الأسبوعي للإشارات\n"
            f"🗓️ الفترة: {start_weekday} {report_start_key} ← {end_weekday} {report_end_key}\n"
            f"🕛 وقت التقرير: {ms_to_local_text(now_ms, self.config.timezone_name)}\n"
            f"═════════════\n"
            f"📥 إجمالي الصفقات المرسلة: {entries}\n"
            f"✅ إجمالي النجاح: {targets}\n"
            f"🛑 إجمالي الخسارة: {stops}\n"
            f"📌 المفتوحة حاليًا: {open_count}\n"
            f"📈 نسبة النجاح: {success_rate:.1f}%\n"
            f"═════════════\n"
            f"🏆 أكثر العملات نجاحًا: {best_symbols}\n"
            f"⚠️ أكثر العملات وصولًا للوقف: {stop_symbols_text}"
        )
        self.telegram.send_message(text)
        weekly_state["last_reported_week_start"] = report_start_key
        self.store.save(self.state)
        self.logger.info("Weekly report sent for %s -> %s", report_start_key, report_end_key)

    def run_cycle(self) -> None:
        self.refresh_symbols(force=not self.symbols)
        self.refresh_halal_verdicts()
        self.sync_open_trades_to_journal()
        server_time = self.binance.get_server_time()
        self.monitor_open_trades_intrabar_targets(server_time)
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
