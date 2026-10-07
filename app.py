from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src.binance_client import BinanceClient
from src.config import AppConfig
from src.entry_filters import EntryGateSettings, evaluate_entry_gates
from src.exit_rules import ExitSettings
from src.halal import ensure_verdict, refresh_if_stale
from src.shadow_journal import append_shadow_candidate, resolve_shadow_candidate
from src.smart_entry import REJECTION_REASONS_AR as SMART_REJECTION_REASONS_AR, SmartEntrySettings
from src.state import StateStore
from src.strategy import StrategySettings, compute_entry_signal
from src.telegram_client import TelegramClient
from src.trade_analysis import (
    avg_metric,
    rows_for_entry_day_range,
    rows_for_exit_day_range,
    build_daily_observations,
    is_loss,
    is_win,
    load_shadow_rows,
    load_trade_rows,
    rows_for_entry_day,
    rows_for_exit_day,
    shadow_rows_for_day,
    shadow_stats,
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
SHADOW_MAX_ACTIVE = 400  # حد أقصى للمرشحات المتابعة في وقت واحد
SHADOW_MAX_AGE_MS = 48 * HOUR_MS  # بعدها تُعتبر منتهية بدون نتيجة


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
        self.entry_gates = EntryGateSettings()
        self.exit_rules = ExitSettings()
        self.smart_entry = SmartEntrySettings(timezone_name=config.timezone_name)
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
            "atr_at_entry": trade.get("atr_at_entry", metrics.get("atr", "")),
            "trail_atr_mult": self.exit_rules.trail_atr_mult if self.exit_rules.uses_trailing_stop else "",
            "trail_initial_stop_pct": trade.get("trail_initial_stop_pct", ""),
            "reference_target_pct": self.exit_rules.reference_target_pct,
            "max_favorable_pct": trade.get("max_favorable_pct", ""),
            "reference_target_hit": int(bool(trade.get("reference_target_hit", False))),
            "exit_mode": self.exit_rules.mode,
            "smart_score": trade.get("smart_score", ""),
            "entry_hour_local": trade.get("entry_hour_local", self.smart_entry.local_hour(int(trade["entry_time"]))),
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
                "max_favorable_pct": round(float(trade.get("max_favorable_pct", 0.0)), 6),
                "reference_target_hit": int(bool(trade.get("reference_target_hit", False))),
                "exit_mode": self.exit_rules.mode,
            },
        )

    def sync_open_trades_to_journal(self) -> None:
        changed = False
        for symbol, trade in self.state.get("open_trades", {}).items():
            if not trade.get("trade_id"):
                trade["trade_id"] = f"{symbol}-{trade.get('entry_bar_open_time', trade.get('entry_time', ''))}"
                changed = True
            # تهيئة حقول قواعد الخروج للصفقات التي فُتحت قبل تفعيلها
            if "peak_price" not in trade:
                trade["peak_price"] = float(trade["entry_price"])
                changed = True
            if "max_favorable_pct" not in trade:
                trade["max_favorable_pct"] = 0.0
                changed = True
            if "reference_target_hit" not in trade:
                trade["reference_target_hit"] = False
                changed = True
            append_trade_entry(self.data_dir, self._build_trade_log_record(trade))
        if changed:
            self.store.save(self.state)

    def send_entry_message(self, trade: dict) -> None:
        strength = "قوية" if trade.get("strong") else "عادية"
        verdict = trade.get("halal_verdict") or self.get_halal_verdict(trade["symbol"])
        stop_line = f"وقف الخسارة: {format_price(trade['stop_price'])}"
        if self.exit_rules.is_hybrid:
            stop_line = (
                f"وقف الخسارة: {format_price(trade['stop_price'])}\n"
                f"خطة الخروج: عند +{self.exit_rules.reference_target_pct:.1f}% يتحول الوقف إلى متحرك "
                f"({self.exit_rules.trail_atr_mult:.1f}×ATR) بأرضية {self.exit_rules.hybrid_lock_pct:+.2f}%"
            )
        elif self.exit_rules.uses_trailing_stop:
            trail_stop = trade.get("trail_stop_price")
            initial_pct = trade.get("trail_initial_stop_pct")
            stop_line = (
                f"الوقف المتحرك: {format_price(trail_stop)}"
                f" ({initial_pct:.2f}% تحت الدخول — يرتفع مع الصعود)"
                if trail_stop is not None and initial_pct is not None
                else "الوقف المتحرك: يُحسب بعد أول شمعة"
            )
        text = (
            f"📥 إشارة دخول شراء\n"
            f"الزوج: {trade['symbol']}\n"
            f"الفريم: 1H\n"
            f"قوة الإشارة: {strength}\n"
            f"سعر الدخول: {format_price(trade['entry_price'])}\n"
            f"الهدف المرجعي: {format_price(trade['target_price'])}\n"
            f"{stop_line}\n"
            f"وقت الإشارة: {ms_to_local_text(trade['entry_time'], self.config.timezone_name)}\n"
            f"─────────────\n"
            f"الحكم الشرعي: {verdict}"
        )
        self.telegram.send_message(text)
        self.append_event("entry", trade["symbol"], int(trade["entry_time"]))
        append_trade_entry(self.data_dir, self._build_trade_log_record(trade))

    def send_reference_target_message(self, trade: dict, event_time_ms: int) -> None:
        """تنبيه فقط: السعر بلغ الهدف المرجعي والصفقة مستمرة بقاعدة الوقف المتحرك."""
        duration = humanize_duration_ar(trade["entry_time"], event_time_ms)
        verdict = trade.get("halal_verdict") or self.get_halal_verdict(trade["symbol"])
        trail_stop = trade.get("trail_stop_price")
        text = (
            f"🎯 بلوغ الهدف المرجعي (+{self.exit_rules.reference_target_pct:.1f}%)\n"
            f"الزوج: {trade['symbol']}\n"
            f"سعر الدخول: {format_price(trade['entry_price'])}\n"
            f"سعر الإشارة: {format_price(trade['target_price'])}\n"
            f"وقت البلاغ: {ms_to_local_text(event_time_ms, self.config.timezone_name)}\n"
            f"المدة المستغرقة: {duration}\n"
            f"─────────────\n"
            f"ℹ️ الصفقة مستمرة — الخروج بقاعدة الوقف المتحرك\n"
            f"الوقف المتحرك الحالي: {format_price(trail_stop) if trail_stop is not None else '—'}\n"
            f"─────────────\n"
            f"الحكم الشرعي: {verdict}"
        )
        self.telegram.send_message(text)
        self.append_event("reference_target", trade["symbol"], int(event_time_ms))

    def send_trail_exit_message(self, trade: dict, event_time_ms: int, exit_price: float) -> None:
        duration = humanize_duration_ar(trade["entry_time"], event_time_ms)
        verdict = trade.get("halal_verdict") or self.get_halal_verdict(trade["symbol"])
        gross = (float(exit_price) / float(trade["entry_price"]) - 1.0) * 100.0
        net = gross - (2.0 * self.settings.commission_per_side_pct)
        peak = float(trade.get("peak_price", trade["entry_price"]))
        peak_gain = (peak / float(trade["entry_price"]) - 1.0) * 100.0
        emoji = "✅" if net > 0 else "🛑"
        headline = "إغلاق على ربح (وقف متحرك)" if net > 0 else "إغلاق على خسارة (وقف متحرك)"
        text = (
            f"{emoji} {headline}\n"
            f"الزوج: {trade['symbol']}\n"
            f"سعر الدخول: {format_price(trade['entry_price'])}\n"
            f"سعر الخروج: {format_price(exit_price)}\n"
            f"أعلى سعر تحقق: {format_price(peak)} (+{peak_gain:.2f}%)\n"
            f"النتيجة الصافية: {net:+.2f}%\n"
            f"وقت الخروج: {ms_to_local_text(event_time_ms, self.config.timezone_name)}\n"
            f"المدة المستغرقة: {duration}\n"
            f"─────────────\n"
            f"الحكم الشرعي: {verdict}"
        )
        self.telegram.send_message(text)
        self.append_event("exit_win" if net > 0 else "exit_loss", trade["symbol"], int(event_time_ms))
        self._record_trade_exit(trade, "win" if net > 0 else "loss", "trail_stop", int(event_time_ms), float(exit_price))

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
        """متابعة دقيقة‑بدقيقة لكل صفقة مفتوحة.

        - fixed_target: إغلاق فوري عند بلوغ الهدف (السلوك القديم).
        - trailing: تحديث أعلى سعر + الوقف المتحرك، وتنبيه عند بلوغ الهدف المرجعي دون إغلاق.
        """
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

            peak_price = max(float(trade.get("peak_price", trade["entry_price"])), float(klines["high"].max()))
            trade["peak_price"] = peak_price
            trade["max_favorable_pct"] = round((peak_price / float(trade["entry_price"]) - 1.0) * 100.0, 6)

            if not self.exit_rules.uses_trailing_stop:
                hit_rows = klines[klines["high"] >= float(trade["target_price"])]
                if not hit_rows.empty:
                    first_hit = hit_rows.iloc[0]
                    event_time_ms = int(first_hit["close_time"])
                    self.send_target_message(trade, float(trade["target_price"]), event_time_ms)
                    self.close_trade(symbol, int(event_time_ms // HOUR_MS * HOUR_MS), "target_intrabar")
                    continue
            else:
                if (
                    not trade.get("reference_target_hit")
                    and float(trade["target_price"]) <= peak_price
                ):
                    hit_rows = klines[klines["high"] >= float(trade["target_price"])]
                    event_time_ms = int(hit_rows.iloc[0]["close_time"]) if not hit_rows.empty else now_ms
                    trade["reference_target_hit"] = True
                    self.send_reference_target_message(trade, event_time_ms)

            trade["last_target_check_ms"] = int(klines.iloc[-1]["close_time"]) + 1
            state_changed = True

        if state_changed:
            self.store.save(self.state)

    def _atr_value_for(self, df, settings_atr_len: int = 14) -> float | None:
        """آخر قيمة ATR محسوبة على الشموع المتاحة (تُستخدم لمسافة الوقف المتحرك)."""
        if df is None or df.empty or len(df) < settings_atr_len + 1:
            return None
        from src.strategy import atr as compute_atr

        series = compute_atr(df, settings_atr_len)
        value = series.iloc[-1]
        if value is None or value != value:  # NaN
            return None
        return float(value)

    def _record_peak_fields(self, trade: dict) -> None:
        """يثبّت أعلى سعر وبلوغ الهدف المرجعي في سجل الصفقات عند الخروج."""
        update_trade_exit(
            self.data_dir,
            str(trade.get("trade_id")),
            {
                "max_favorable_pct": round(float(trade.get("max_favorable_pct", 0.0)), 6),
                "reference_target_hit": int(bool(trade.get("reference_target_hit", False))),
            },
        )

    def register_rejected_candidate(self, symbol: str, signal: dict, reason: str, reason_ar: str) -> None:
        """يسجّل الإشارة التي رفضتها البوابات الجديدة لمتابعتها لاحقًا (قياس مضاد للواقع)."""
        bar_open_time = int(signal["bar_open_time"])
        if int(self.state["last_rejected_bar_time"].get(symbol, 0)) == bar_open_time:
            return
        self.state["last_rejected_bar_time"][symbol] = bar_open_time

        candidate_id = f"{symbol}-{bar_open_time}"
        metrics = signal.get("metrics", {}) or {}
        rejected_ms = int(signal["bar_close_time"])

        record = {
            "candidate_id": candidate_id,
            "symbol": symbol,
            "rejected_time_ms": rejected_ms,
            "rejected_time_local": ms_to_local_text(rejected_ms, self.config.timezone_name),
            "rejected_date_local": local_date_key_from_ms(rejected_ms, self.config.timezone_name),
            "entry_bar_open_time": bar_open_time,
            "reject_reason": reason,
            "reject_reason_ar": reason_ar,
            "entry_price": signal["entry_price"],
            "target_price": signal["target_price"],
            "stop_price": signal["stop_price"],
            "strong_signal": 1 if signal.get("strong") else 0,
            "buy_score": signal.get("buy_score"),
            **{key: metrics.get(key) for key in (
                "rsi",
                "stoch",
                "adx",
                "plus_di",
                "minus_di",
                "relative_volume",
                "reward_risk_ratio",
                "buy_risk_pct",
                "distance_from_ema200_pct",
            )},
        }
        append_shadow_candidate(self.data_dir, record)

        shadow = self.state.setdefault("shadow_candidates", {})
        if len(shadow) >= SHADOW_MAX_ACTIVE:
            self.expire_oldest_shadow_candidates(needed=1)
        shadow[candidate_id] = {
            "candidate_id": candidate_id,
            "symbol": symbol,
            "entry_price": float(signal["entry_price"]),
            "target_price": float(signal["target_price"]),
            "stop_price": float(signal["stop_price"]),
            "entry_time": int(signal["bar_close_time"]),
            "entry_bar_open_time": bar_open_time,
            "reason": reason,
        }
        self.store.save(self.state)
        self.logger.info("Rejected candidate %s بسبب %s", symbol, reason)

    def expire_oldest_shadow_candidates(self, needed: int = 1) -> None:
        shadow = self.state.get("shadow_candidates", {})
        if not shadow:
            return
        ordered = sorted(shadow.values(), key=lambda item: int(item.get("entry_bar_open_time", 0)))
        for candidate in ordered[:needed]:
            self.resolve_shadow(candidate, outcome="expired", exit_reason="age_limit", exit_time_ms=0, exit_price=None)
        self.store.save(self.state)

    def resolve_shadow(self, candidate: dict, outcome: str, exit_reason: str, exit_time_ms: int, exit_price: float | None) -> None:
        candidate_id = str(candidate.get("candidate_id"))
        duration_minutes = 0
        if exit_time_ms:
            duration_minutes = max(int((int(exit_time_ms) - int(candidate["entry_time"])) // 60000), 0)
        updates = {
            "outcome": outcome,
            "exit_reason": exit_reason,
            "exit_time_ms": int(exit_time_ms) if exit_time_ms else "",
            "exit_time_local": ms_to_local_text(int(exit_time_ms), self.config.timezone_name) if exit_time_ms else "",
            "exit_price": "" if exit_price is None else float(exit_price),
            "duration_minutes": duration_minutes,
            "resolution_method": "hourly_approx",
        }
        resolve_shadow_candidate(self.data_dir, candidate_id, updates)
        self.state.get("shadow_candidates", {}).pop(candidate_id, None)

    def monitor_shadow_candidates(self, klines_map: dict, last_closed_open_time: int) -> None:
        """يتابع المرشحات المستبعدة بنفس قواعد الصفقات الحقيقية لتقيس أثر الفلاتر."""
        shadow = self.state.get("shadow_candidates", {})
        if not shadow:
            return

        changed = False
        for candidate in list(shadow.values()):
            candidate_id = str(candidate.get("candidate_id"))
            entry_bar_open_time = int(candidate.get("entry_bar_open_time", 0))
            if entry_bar_open_time >= last_closed_open_time:
                continue

            age_ms = (last_closed_open_time + HOUR_MS) - int(candidate["entry_time"])
            if age_ms > SHADOW_MAX_AGE_MS:
                self.resolve_shadow(candidate, "expired", "age_limit", 0, None)
                changed = True
                continue

            df = klines_map.get(candidate["symbol"])
            if df is None or df.empty:
                continue
            candle = df[df["open_time"] == last_closed_open_time]
            if candle.empty:
                continue

            row = candle.iloc[-1]
            close_time = int(row["close_time"])
            if float(row["high"]) >= float(candidate["target_price"]):
                self.resolve_shadow(candidate, "target", "take_profit_simulated", close_time, float(candidate["target_price"]))
                changed = True
            elif float(row["close"]) < float(candidate["stop_price"]):
                self.resolve_shadow(candidate, "stop", "stop_loss_simulated", close_time, float(candidate["stop_price"]))
                changed = True

        if changed:
            self.store.save(self.state)

    def process_new_closed_hour(self, server_time: int) -> None:
        last_closed_open_time = ((server_time // HOUR_MS) - 1) * HOUR_MS
        if int(self.state.get("last_processed_open_time", 0)) >= last_closed_open_time:
            return

        self.logger.info("Processing closed hour at open_time=%s", last_closed_open_time)
        klines_map = self.binance.get_klines_for_symbols(self.symbols, self.config.interval, self.config.kline_limit)

        self.monitor_shadow_candidates(klines_map, last_closed_open_time)

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

            peak_price = max(float(trade.get("peak_price", trade["entry_price"])), candle_high)
            trade["peak_price"] = peak_price
            trade["max_favorable_pct"] = round((peak_price / float(trade["entry_price"]) - 1.0) * 100.0, 6)

            if self.exit_rules.uses_trailing_stop:
                # بلوغ الهدف المرجعي = لحظة التحول إلى الوقف المتحرك، ولا يُغلق الصفقة
                reached_now = False
                if not trade.get("reference_target_hit") and candle_high >= float(trade["target_price"]):
                    trade["reference_target_hit"] = True
                    reached_now = True
                    self.send_reference_target_message(trade, candle_close_time)

                atr_now = self._atr_value_for(df, settings_atr_len=self.exit_rules.trail_atr_len)
                if self.exit_rules.is_hybrid and not trade.get("reference_target_hit"):
                    # الطور الأول: وقف الإشارة الأصلي كما هو
                    if candle_close < float(trade["stop_price"]):
                        self.send_stop_message(trade, candle_close_time)
                        self.close_trade(symbol, last_closed_open_time, "stop_close")
                    continue

                if atr_now is not None:
                    trail = self.exit_rules.trail_level(peak_price, atr_now)
                    if self.exit_rules.is_hybrid:
                        trail = max(trail, self.exit_rules.hybrid_lock_level(float(trade["entry_price"])))
                    trade["trail_stop_price"] = round(trail, 10)
                    if trade.get("trail_initial_stop_pct") in (None, ""):
                        trade["trail_initial_stop_pct"] = round(
                            (float(trade["entry_price"]) - self.exit_rules.trail_level(
                                float(trade["entry_price"]), atr_now)) / float(trade["entry_price"]) * 100.0,
                            6,
                        )
                if reached_now:
                    continue  # نبدأ تقييم الوقف المتحرك من الشمعة التالية (مطابق للمحاكاة)

                trail_stop = trade.get("trail_stop_price")
                if trail_stop is not None and candle_close <= float(trail_stop):
                    self.send_trail_exit_message(trade, candle_close_time, candle_close)
                    self._record_peak_fields(trade)
                    self.close_trade(symbol, last_closed_open_time, "trail_stop")
                continue

            if candle_high >= float(trade["target_price"]):
                self.send_target_message(trade, float(trade["target_price"]), candle_close_time)
                self.close_trade(symbol, last_closed_open_time, "target_hour_recovery")
                continue

            if candle_close < float(trade["stop_price"]):
                self.send_stop_message(trade, candle_close_time)
                self.close_trade(symbol, last_closed_open_time, "stop_close")

        candidates: list[dict] = []
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

            allowed, reject_reason, reject_reason_ar = evaluate_entry_gates(signal.get("metrics", {}), self.entry_gates)
            if not allowed:
                self.register_rejected_candidate(symbol, signal, reject_reason, reject_reason_ar)
                continue

            # البوابة الزمنية: أقوى مكوّن مُتحقَّق منه في القياس
            if not self.smart_entry.is_hour_allowed(signal["bar_close_time"]):
                self.register_rejected_candidate(
                    symbol, signal, "outside_time_window", SMART_REJECTION_REASONS_AR["outside_time_window"]
                )
                continue

            candidates.append({"symbol": symbol, "signal": signal})

        # ترتيب المرشحين وتطبيق الحد الأقصى للصفقات المتزامنة
        selected, dropped = self.smart_entry.select(candidates, len(self.state["open_trades"]))
        for candidate in dropped:
            self.register_rejected_candidate(
                candidate["symbol"], candidate["signal"], "concurrency_cap",
                SMART_REJECTION_REASONS_AR["concurrency_cap"],
            )

        for candidate in selected:
            symbol = candidate["symbol"]
            signal = candidate["signal"]
            trade_id = f"{symbol}-{signal['bar_open_time']}"
            metrics = signal.get("metrics", {}) or {}
            atr_at_entry = metrics.get("atr")
            trail_stop_price = None
            trail_initial_stop_pct = None
            if self.exit_rules.uses_trailing_stop and atr_at_entry:
                trail_stop_price = round(self.exit_rules.trail_level(float(signal["entry_price"]), float(atr_at_entry)), 10)
                trail_initial_stop_pct = round(
                    (float(signal["entry_price"]) - trail_stop_price) / float(signal["entry_price"]) * 100.0, 6
                )

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
                "metrics": metrics,
                "atr_at_entry": atr_at_entry,
                "peak_price": float(signal["entry_price"]),
                "max_favorable_pct": 0.0,
                "reference_target_hit": False,
                "trail_stop_price": trail_stop_price,
                "trail_initial_stop_pct": trail_initial_stop_pct,
                "smart_score": self.smart_entry.quality_score(metrics, signal.get("buy_score")),
                "entry_hour_local": self.smart_entry.local_hour(signal["bar_close_time"]),
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
            elif event_type in ("target", "exit_win"):
                targets += 1
            elif event_type in ("stop", "exit_loss"):
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
        win_rows = [row for row in closed_rows if is_win(row.get("outcome"))]
        loss_rows = [row for row in closed_rows if is_loss(row.get("outcome"))]

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
        ]

        shadow_all = load_shadow_rows(self.data_dir)
        shadow_day = shadow_stats(shadow_rows_for_day(shadow_all, report_day))
        shadow_total = shadow_stats(shadow_all)
        lines.extend([
            f"🕵️ مرشحات استبعدتها الفلاتر اليوم: {int(shadow_day['total'])}",
            (
                f"   • من المستبعد اليوم: هدف {int(shadow_day['wins'])} / وقف {int(shadow_day['losses'])}"
                f" → نسبة {float(shadow_day['rate']):.1f}%"
            ),
            (
                f"   • إجمالي المستبعد منذ التفعيل: {int(shadow_total['total'])}"
                f" (هدف {int(shadow_total['wins'])} / وقف {int(shadow_total['losses'])} = {float(shadow_total['rate']):.1f}%)"
            ),
            "   • القاعدة: إذا كانت نسبة المستبعد أقل من نسبة الصفقات المأخوذة فالفلاتر تعمل لصالحنا",
            "═════════════",
        ])

        # مقارنة قواعد الخروج على نفس الصفقات: الوقف المتحرك مقابل الهدف الثابت +2%
        reference_hits = [row for row in closed_rows if str(row.get("reference_target_hit") or "0") == "1"]
        reverted = [row for row in reference_hits if is_loss(row.get("outcome"))]
        extended = [row for row in reference_hits if is_win(row.get("outcome"))]
        lines.extend([
            f"🧭 قاعدة الخروج المفعّلة: {self.exit_rules.describe()}",
            f"⏰ الدخول الذكي: {self.smart_entry.describe()}",
            (lambda counts: "   • المرشحون المرفوضون اليوم — خارج النافذة الزمنية: %d • تجاوز حد التزامن: %d"
             % (counts.get("outside_time_window", 0), counts.get("concurrency_cap", 0)))(
                Counter(r.get("reject_reason", "") for r in shadow_rows_for_day(shadow_all, report_day))
            ),
            f"🔁 صفقات مغلقة بلغت الهدف المرجعي +{self.exit_rules.reference_target_pct:.1f}%: {len(reference_hits)}",
            f"   • أكملت ربحًا بعد ذلك: {len(extended)}",
            f"   • ارتدت وأُغلقت خسارة بعد بلوغه: {len(reverted)}",
            "   • القاعدة القديمة كانت ستغلق كلها بربح +1.8% صافي عند بلوغ الهدف",
            "═════════════",
        ])

        lines.extend([
            f"📊 متوسط Buy Score — رابحة: {avg_win_buy:.2f}" if avg_win_buy is not None else "📊 متوسط Buy Score — رابحة: —",
            f"📊 متوسط Buy Score — خاسرة: {avg_loss_buy:.2f}" if avg_loss_buy is not None else "📊 متوسط Buy Score — خاسرة: —",
            f"🔊 متوسط الحجم النسبي — رابحة: {avg_win_rvol:.2f}x" if avg_win_rvol is not None else "🔊 متوسط الحجم النسبي — رابحة: —",
            f"🔊 متوسط الحجم النسبي — خاسرة: {avg_loss_rvol:.2f}x" if avg_loss_rvol is not None else "🔊 متوسط الحجم النسبي — خاسرة: —",
            "═════════════",
            "📝 ملاحظات تحليلية:",
        ])
        lines.extend([f"• {note}" for note in observations])
        if closed_count == 0:
            lines.append("• لا توجد صفقات مغلقة كافية لهذا اليوم بعد، لذلك التحليل النوعي ما زال محدودًا.")

        target_rows = [row for row in closed_rows if is_win(row.get("outcome"))]
        stop_rows = [row for row in closed_rows if is_loss(row.get("outcome"))]
        target_dur = self._exit_duration_summary(target_rows)
        stop_dur = self._exit_duration_summary(stop_rows)
        lines.extend([
            f"⏱️ متوسط مدة الوصول للهدف: {self._duration_h_text(target_dur['avg_h'])}"
            + (f" (أسرع {self._duration_h_text(target_dur['min_h'])} • أبطأ {self._duration_h_text(target_dur['max_h'])})" if target_dur["count"] else ""),
            f"⏱️ متوسط مدة ضرب الوقف: {self._duration_h_text(stop_dur['avg_h'])}"
            + (f" (أسرع {self._duration_h_text(stop_dur['min_h'])} • أبطأ {self._duration_h_text(stop_dur['max_h'])})" if stop_dur["count"] else ""),
            "═════════════",
        ])

        self.telegram.send_message("\n".join(lines))
        analysis_state["last_reported_for_date"] = report_day
        self.store.save(self.state)
        self.logger.info("Daily analysis sent for %s", report_day)


    @staticmethod
    def _exit_duration_summary(rows: list[dict]) -> dict:
        """متوسط/أسرع/أبطأ مدة حتى الإغلاق (هدف أو وقف) — بالساعات، من دفتر الصفقات."""
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
        """صياغة عربية سليمة للعدد: 1 ساعة • ساعتان • 5 ساعات • 15 ساعة."""
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
    def _outcome_metrics(rows: list[dict]) -> dict:
        """صافي النسبة والنتيجة لكل صفقة مغلقة (للتقرير الأسبوعي)."""
        nets = []
        for row in rows:
            value = row.get("net_return_pct")
            try:
                nets.append(float(value))
            except (TypeError, ValueError):
                continue
        if not nets:
            return {"count": 0, "avg": None, "best": None, "worst": None}
        return {"count": len(nets), "avg": sum(nets) / len(nets), "best": max(nets), "worst": min(nets)}

    @staticmethod
    def _smart_window_stats(rows: list[dict], hours: list[int]) -> dict:
        total = len(rows)
        inside = sum(1 for r in rows if int(float(r.get("entry_hour_local") or -1)) in hours) if total else 0
        return {"total": total, "inside": inside}

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
            elif event_type in ("target", "exit_win"):
                targets += 1
                if symbol:
                    target_symbols[symbol] += 1
            elif event_type in ("stop", "exit_loss"):
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

        # ---- تحليل من دفتر الصفقات الحقيقي (نفس مصدر التحليل اليومي) ----
        rows = load_trade_rows(self.data_dir)
        entry_rows = rows_for_entry_day_range(rows, report_start_key, report_end_key)
        closed_rows = rows_for_exit_day_range(rows, report_start_key, report_end_key, self.config.timezone_name)
        win_rows = [row for row in closed_rows if is_win(row.get("outcome"))]
        loss_rows = [row for row in closed_rows if is_loss(row.get("outcome"))]
        win_count, loss_count = len(win_rows), len(loss_rows)
        file_rate = success_rate_percent(win_count, loss_count)
        outcome = self._outcome_metrics(closed_rows)
        strong_stats = strong_vs_normal_stats(closed_rows)
        target_dur = self._exit_duration_summary(win_rows)
        stop_dur = self._exit_duration_summary(loss_rows)
        target_dur_all = self._exit_duration_summary(closed_rows)

        # ---- أداء الدخول الذكي: هل التزمت الصفقات بالنافذة الزمنية؟ ----
        allowed_hours = self.smart_entry.allowed_hours
        window = self._smart_window_stats(entry_rows, allowed_hours)
        inside_rows = [r for r in entry_rows if int(float(r.get("entry_hour_local") or -1)) in allowed_hours] if allowed_hours else entry_rows
        inside_win = sum(1 for r in inside_rows if is_win(r.get("outcome")))
        inside_loss = sum(1 for r in inside_rows if is_loss(r.get("outcome")))
        window_rate = success_rate_percent(inside_win, inside_loss)

        # ---- أفضل وأسوأ ساعات الدخول هذا الأسبوع ----
        hour_stats: dict[int, list[float]] = {}
        for row in closed_rows:
            try:
                hour = int(float(row.get("entry_hour_local")))
            except (TypeError, ValueError):
                continue
            try:
                hour_stats.setdefault(hour, []).append(float(row.get("net_return_pct")))
            except (TypeError, ValueError):
                continue
        ranked_hours = sorted(
            ((h, sum(v) / len(v), len(v)) for h, v in hour_stats.items() if len(v) >= 2),
            key=lambda item: item[1],
            reverse=True,
        )
        hours_text = " • ".join(f"{h:02d}:00 ({avg:+.2f}% / {n})" for h, avg, n in ranked_hours[:3]) or "لا توجد بيانات كافية"
        topped = {h for h, _, _ in ranked_hours[:3]}
        worst_pool = [item for item in ranked_hours[::-1] if item[0] not in topped]
        worst_hours_text = (
            " • ".join(f"{h:02d}:00 ({avg:+.2f}% / {n})" for h, avg, n in worst_pool[:2])
            if worst_pool else "لا توجد ساعات كافية للمقارنة بعد"
        )

        shadow_all = load_shadow_rows(self.data_dir)
        shadow_week_rows = [
            r for r in shadow_all
            if report_start_key <= str(r.get("rejected_date_local") or "") <= report_end_key
        ]
        shadow_week = shadow_stats(shadow_week_rows)
        week_reject_reasons = Counter(str(r.get("reject_reason") or "") for r in shadow_week_rows)

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
            f"📒 تحليل دفتر الصفقات (نفس مصدر التحليل اليومي)\n"
            f"   • صفقات دخلت هذا الأسبوع: {len(entry_rows)}\n"
            f"   • أُغلقت على هدف: {win_count} • أُغلقت على وقف: {loss_count} • ما زالت مفتوحة: {open_count}\n"
            f"   • نسبة نجاح المغلقة: {file_rate:.1f}%\n"
            + (
                f"   • متوسط نتيجة الصفقة المغلقة: {outcome['avg']:+.2f}% (أفضل {outcome['best']:+.2f}% • أسوأ {outcome['worst']:+.2f}%)\n"
                if outcome["count"] else "   • متوسط نتيجة الصفقة المغلقة: —\n"
            )
            + (
                f"   • 💪 الإشارات القوية: {float(strong_stats['strong_rate']):.1f}% من {int(strong_stats['strong_count'])}\n"
                f"   • 📎 الإشارات العادية: {float(strong_stats['normal_rate']):.1f}% من {int(strong_stats['normal_count'])}\n"
                if (int(strong_stats["strong_count"]) + int(strong_stats["normal_count"])) else ""
            )
            + f"═════════════\n"
            f"⏱️ المدد\n"
            f"   • متوسط مدة الوصول للهدف: {self._duration_h_text(target_dur['avg_h'])}"
            + (f" (أسرع {self._duration_h_text(target_dur['min_h'])} • أبطأ {self._duration_h_text(target_dur['max_h'])})\n" if target_dur["count"] else "\n")
            + f"   • متوسط مدة ضرب الوقف: {self._duration_h_text(stop_dur['avg_h'])}"
            + (f" (أسرع {self._duration_h_text(stop_dur['min_h'])} • أبطأ {self._duration_h_text(stop_dur['max_h'])})\n" if stop_dur["count"] else "\n")
            + f"   • متوسط مدة الصفقة المغلقة عمومًا: {self._duration_h_text(target_dur_all['avg_h'])}\n"
            f"═════════════\n"
            f"⏰ الدخول الذكي: {self.smart_entry.describe()}\n"
            f"   • الصفقات داخل النافذة الزمنية: {window['inside']} من {window['total']}"
            + (f" • نسبة نجاحها: {window_rate:.1f}%\n" if (inside_win + inside_loss) else "\n")
            + f"   • 🟢 أفضل ساعات الدخول: {hours_text}\n"
            f"   • 🔴 أسوأ ساعات الدخول: {worst_hours_text}\n"
            f"   • 🕵️ مرشحون مستبعدون هذا الأسبوع: {int(shadow_week['total'])}"
            + (f" (هدف {int(shadow_week['wins'])} / وقف {int(shadow_week['losses'])} = {float(shadow_week['rate']):.1f}%)\n" if int(shadow_week["total"]) else "\n")
            + (
                "   • أسباب الاستبعاد: "
                + " • ".join(f"{k}={v}" for k, v in week_reject_reasons.most_common(4)) + "\n"
                if week_reject_reasons else ""
            )
            + f"═════════════\n"
            f"🧭 قاعدة الخروج المفعّلة: {self.exit_rules.describe()}\n"
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
