"""بوت نظام ZigZag++ (فريم الساعة) — نظام مستقل تمامًا عن بقية الأنظمة.

لا يقرأ ولا يكتب أي ملف يخص نظامًا آخر:
    الحالة:  data/state_zigzag.json      (Target Trend: data/state.json)
    الدفتر:  data/trades_log_zigzag.csv  (Target Trend: data/trades_log.csv)
    الكاش:   data/zigzag/                (Target Trend: data/halal_verdicts.json)

الرسائل (كلها باسم «ZigZag++ (ساعة)» + سطر الحكم الشرعي):
    📥 دخول — سعر الدخول/الهدف +2%/وقف قاع الشمعة
    🎯 نجاح — لمس +2% (يُرسل فورًا بلا انتظار إغلاق)
    🛑 خسارة — إغلاق شمعة ساعة تحت قاع الإشارة

القواعد:
    - القاع: أدنى قاع في آخر Depth=12 شمعة 1H مغلقة.
    - الشمعة التالية: خضراء وغير دوجي ⇒ دخول بإغلاقها؛ حمراء/دوجي ⇒ يُلغى القاع.
    - الهدف: +2% من سعر الدخول باللمس (أعلى سعر)، والفحص يُعاد كل دقيقة عبر شموع 1m.
    - الخسارة: إغلاق شمعة الساعة تحت أدنى سعر لشمعة القاع (ليس اللمس).
    - إشارة واحدة لكل عملة في وقت واحد؛ بلا حد لعدد الإشارات بين العملات.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src.binance_client import BinanceClient
from src.config import AppConfig
from src.halal import ensure_verdict, refresh_if_stale
from src.state import StateStore
from src.telegram_client import TelegramClient
from src.utils import (
    format_price,
    humanize_duration_ar,
    ms_to_local_text,
)
from src.zigzag import (
    HOUR_MS,
    MINUTE_MS,
    STRATEGY_LABEL,
    ZigZagSettings,
    evaluate_entry_candle,
    evaluate_path_1h,
    evaluate_path_1m,
    find_latest_bottom,
    next_closed_bar,
)
from src.zigzag_journal import append_zigzag_entry, update_zigzag_exit

EVENT_RETENTION_DAYS = 120
MAX_CATCHUP_HOURS = 24 * 7  # أقصى فترة استدراك لشموع الدقيقة عند توقف البوت

# مسارات النظام المستقل — ثابتة بغض النظر عن متغيرات الأنظمة الأخرى
ZZ_STATE_FILE = os.getenv("ZZ_STATE_FILE", "data/state_zigzag.json")
ZZ_HALAL_DIR = os.getenv("ZZ_HALAL_DIR", "data/zigzag")

_AR_MONTHS = (
    "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
    "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر",
)

# ─────────────────────────────────────────────────────────────────────────────
# استثناء أزواج العملات المستقرة المربوطة — نسخة مستقلة (عزل تام)
# ─────────────────────────────────────────────────────────────────────────────
EXCLUDE_PEGGED_STABLES = os.getenv("EXCLUDE_PEGGED_STABLES", "1").strip().lower() not in (
    "0", "false", "no", "off"
)

PEGGED_STABLE_BASES = frozenset({
    "USDC", "FDUSD", "TUSD", "RLUSD", "XUSD", "USD1", "USDE", "USDS", "BFUSD", "U", "EUR", "EURI",
    "BUSD", "USDP", "DAI", "PYUSD", "GUSD", "LUSD", "AEUR", "EURC", "USDY", "USDD", "USDF",
})


def is_pegged_stable_symbol(symbol: str, quote_asset: str = "USDT") -> bool:
    symbol = (symbol or "").upper()
    quote = (quote_asset or "USDT").upper()
    if not symbol.endswith(quote):
        return False
    return symbol[: -len(quote)] in PEGGED_STABLE_BASES


def pct_label(value: float) -> str:
    """نسبة مختصرة: +10.0% ← +10% (تبقى الخانة عند الحاجة)."""
    return f"{value:+.1f}%".replace(".0%", "%")


def stamp_ms(ms: int, timezone_name: str) -> str:
    """ختم زمني للرسائل: 29 أكتوبر — 3:05 مساءً."""
    moment = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(timezone_name))
    hour12 = moment.hour % 12 or 12
    day_part = "صباحًا" if moment.hour < 12 else "مساءً"
    return f"{moment.day} {_AR_MONTHS[moment.month - 1]} — {hour12}:{moment.minute:02d} {day_part}"


def hhmm_local(ms: int, timezone_name: str) -> str:
    """ساعة الشمعة بصيغة 24 ساعة: 14:00."""
    moment = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(timezone_name))
    return f"{moment.hour:02d}:{moment.minute:02d}"


class ZigZagSignalBot:
    """بوت إشارات ZigZag++ (1H) — مسح كل أزواج USDT سبوت."""

    INTERVAL = "1h"  # الفريم ساعة حصريًا — لا يتأثر بـ BINANCE_INTERVAL

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = logging.getLogger("zigzag-signal-bot")
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
        self.settings = ZigZagSettings()
        self.interval = self.INTERVAL
        self.interval_ms = BinanceClient.INTERVAL_MS[self.interval]
        self.store = StateStore(ZZ_STATE_FILE)
        self.state = self.store.load()
        self.state.setdefault("last_bottom_bar_time", {})
        self.state.setdefault("open_signals", {})
        self.data_dir = str(Path(ZZ_STATE_FILE).parent)
        self.halal_dir = ZZ_HALAL_DIR  # كاش شرعي منفصل — لا يلمس كاش أي نظام آخر
        self.price_ticks: dict[str, str] = {}
        self.halal_verdicts: dict[str, int] = {}
        self.symbols: list[str] = []
        self.last_symbols_refresh = 0.0

    # ─────────────────────────────────────────── البيانات الأساسية
    def refresh_symbols(self, force: bool = False) -> None:
        now = time.time()
        if not force and self.symbols and (now - self.last_symbols_refresh) < 6 * 60 * 60:
            return
        all_symbols = self.binance.get_spot_usdt_symbols(self.config.quote_asset)
        self.price_ticks = self.binance.get_price_ticks(self.config.quote_asset)
        if EXCLUDE_PEGGED_STABLES:
            self.symbols = [s for s in all_symbols if not is_pegged_stable_symbol(s, self.config.quote_asset)]
        else:
            self.symbols = list(all_symbols)
        self.last_symbols_refresh = now
        excluded = len(all_symbols) - len(self.symbols)
        self.logger.info(
            "ZigZag: Loaded %s spot symbols with quote asset %s%s",
            len(self.symbols), self.config.quote_asset,
            f" (استُثني {excluded} زوجًا لعملات مستقرة مربوطة)" if excluded else "",
        )

    def refresh_halal_verdicts(self) -> None:
        self.halal_verdicts = refresh_if_stale(
            self.halal_dir,
            max_age_hours=self.config.halal_refresh_hours,
        )

    def get_halal_verdict(self, symbol: str) -> str:
        return ensure_verdict(self.halal_dir, symbol, self.halal_verdicts)

    def append_event(self, event_type: str, symbol: str, event_time_ms: int) -> None:
        events = self.state.setdefault("event_log", [])
        events.append({"type": event_type, "symbol": symbol, "time_ms": int(event_time_ms)})
        cutoff_ms = int(event_time_ms) - (EVENT_RETENTION_DAYS * 24 * 60 * 60 * 1000)
        self.state["event_log"] = [e for e in events if int(e.get("time_ms", 0)) >= cutoff_ms]
        self.store.save(self.state)

    # ─────────────────────────────────────────── دفتر الصفقات
    def _build_trade_log_record(self, signal: dict) -> dict[str, object]:
        entry_time_ms = int(signal["entry_time"])
        entry_bar_ms = int(signal["entry_bar_open_time"])
        bottom_ms = int(signal["bottom_bar_open_time"])
        return {
            "trade_id": signal["trade_id"],
            "symbol": signal["symbol"],
            "strategy": STRATEGY_LABEL,
            "entry_time_ms": entry_time_ms,
            "entry_time_local": ms_to_local_text(entry_time_ms, self.config.timezone_name),
            "entry_bar_open_time_ms": entry_bar_ms,
            "entry_bar_open_time_local": ms_to_local_text(entry_bar_ms, self.config.timezone_name),
            "entry_price": round(float(signal["entry_price"]), 10),
            "target_price": round(float(signal["target_price"]), 10),
            "stop_price": round(float(signal["stop_price"]), 10),
            "bottom_bar_open_time_ms": bottom_ms,
            "bottom_bar_open_time_local": ms_to_local_text(bottom_ms, self.config.timezone_name),
            "bottom_low": round(float(signal["bottom_low"]), 10),
            "halal_verdict": signal.get("halal_verdict", ""),
            "max_price": signal.get("max_price", signal["entry_price"]),
            "max_gain_pct": 0.0,
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

    def _record_trade_exit(self, signal: dict, event: dict) -> None:
        entry = float(signal["entry_price"])
        exit_price = float(event["fill_price"])
        exit_time_ms = int(event["event_time"])
        duration_minutes = max(int((exit_time_ms - int(signal["entry_time"])) // 60000), 0)
        gross = (exit_price / entry - 1.0) * 100.0
        net = gross - (2.0 * self.settings.commission_side_pct)
        max_price = float(signal.get("max_price", entry))
        update_zigzag_exit(
            self.data_dir,
            str(signal.get("trade_id")),
            {
                "outcome": event["outcome"],
                "exit_reason": event["reason"],
                "exit_time_ms": exit_time_ms,
                "exit_time_local": ms_to_local_text(exit_time_ms, self.config.timezone_name),
                "exit_price": exit_price,
                "duration_minutes": duration_minutes,
                "duration_text": humanize_duration_ar(int(signal["entry_time"]), exit_time_ms),
                "gross_return_pct": round(gross, 6),
                "net_return_pct": round(net, 6),
                "max_price": max_price,
                "max_gain_pct": round((max_price / entry - 1.0) * 100.0, 6),
            },
        )

    def sync_open_signals_to_journal(self) -> None:
        for _symbol, signal in self.state.get("open_signals", {}).items():
            append_zigzag_entry(self.data_dir, self._build_trade_log_record(signal))

    # ─────────────────────────────────────────── رسائل تيليجرام
    def _fmt(self, signal: dict, value: float | str) -> str:
        return format_price(float(value), self.price_ticks.get(str(signal.get("symbol", ""))))

    def _safe_send(self, text: str) -> None:
        """فشل رسالة واحدة لا يوقف الدورة — يُسجَّل ويُكمل."""
        try:
            self.telegram.send_message(text)
        except Exception:
            self.logger.error(
                "ZigZag: فشل إرسال رسالة تيليجرام — تُجتاز وتُسجَّل وتُكمل الدورة",
                exc_info=True,
            )

    def send_entry_message(self, signal: dict) -> None:
        verdict = signal.get("halal_verdict") or self.get_halal_verdict(signal["symbol"])
        text = (
            f"📥 إشارة دخول — {signal['symbol']}\n"
            f"{STRATEGY_LABEL} • فريم 1H\n"
            "\n"
            f"الدخول: {self._fmt(signal, signal['entry_price'])}\n"
            f"الهدف: {self._fmt(signal, signal['target_price'])} (+{self.settings.target_pct:g}%)\n"
            f"الوقف: {self._fmt(signal, signal['stop_price'])} — إغلاق تحت قاع الشمعة\n"
            f"شمعة القاع: {hhmm_local(int(signal['bottom_bar_open_time']), self.config.timezone_name)}"
            f" | شمعة الدخول: {hhmm_local(int(signal['entry_bar_open_time']), self.config.timezone_name)}\n"
            "\n"
            f"⏱ {stamp_ms(int(signal['entry_time']), self.config.timezone_name)}\n"
            f"⚖️ الحكم الشرعي: {verdict}"
        )
        self._safe_send(text)
        self.append_event("entry", signal["symbol"], int(signal["entry_time"]))
        append_zigzag_entry(self.data_dir, self._build_trade_log_record(signal))

    def send_success_message(self, signal: dict, event: dict) -> None:
        verdict = signal.get("halal_verdict") or self.get_halal_verdict(signal["symbol"])
        entry = float(signal["entry_price"])
        fill = float(event["fill_price"])
        gross = (fill / entry - 1.0) * 100.0
        duration = humanize_duration_ar(int(signal["entry_time"]), int(event["event_time"]))
        fill_line = (
            f"سعر التنفيذ: {self._fmt(signal, fill)}\n"
            if abs(fill - float(signal["target_price"])) > 1e-12
            else ""
        )
        text = (
            f"🎯 نجاح — {signal['symbol']}\n"
            f"{STRATEGY_LABEL}\n"
            "\n"
            f"الدخول: {self._fmt(signal, entry)} ← الهدف: {self._fmt(signal, signal['target_price'])}\n"
            f"{fill_line}"
            f"الربح: {pct_label(gross)}\n"
            f"المدة: {duration}\n"
            "\n"
            f"⏱ {stamp_ms(int(event['event_time']), self.config.timezone_name)}\n"
            f"⚖️ الحكم الشرعي: {verdict}"
        )
        self._safe_send(text)
        self.append_event("exit_win", signal["symbol"], int(event["event_time"]))
        self._record_trade_exit(signal, event)
        self.close_signal(signal["symbol"])

    def send_loss_message(self, signal: dict, event: dict) -> None:
        verdict = signal.get("halal_verdict") or self.get_halal_verdict(signal["symbol"])
        entry = float(signal["entry_price"])
        fill = float(event["fill_price"])
        gross = (fill / entry - 1.0) * 100.0
        duration = humanize_duration_ar(int(signal["entry_time"]), int(event["event_time"]))
        text = (
            f"🛑 خسارة — {signal['symbol']}\n"
            f"{STRATEGY_LABEL}\n"
            "\n"
            f"الدخول: {self._fmt(signal, entry)} ← الإغلاق: {self._fmt(signal, fill)}\n"
            f"الخسارة: {pct_label(gross)}\n"
            "سبب الخروج: إغلاق شمعة تحت قاع الإشارة\n"
            f"المدة: {duration}\n"
            "\n"
            f"⏱ {stamp_ms(int(event['event_time']), self.config.timezone_name)}\n"
            f"⚖️ الحكم الشرعي: {verdict}"
        )
        self._safe_send(text)
        self.append_event("exit_loss", signal["symbol"], int(event["event_time"]))
        self._record_trade_exit(signal, event)
        self.close_signal(signal["symbol"])

    def close_signal(self, symbol: str) -> None:
        self.state["open_signals"].pop(symbol, None)
        self.store.save(self.state)
        self.logger.info("ZigZag: Closed signal for %s", symbol)

    # ─────────────────────────────────────────── متابعة الإشارات المفتوحة
    def monitor_open_signals(self, server_time: int, klines_map: dict) -> None:
        """يمشي على شموع الدقيقة بالترتيب: لمس الهدف أو إغلاق ساعة تحت القاع."""
        open_signals = self.state.get("open_signals", {})
        if not open_signals:
            return

        state_changed = False
        for symbol, signal in list(open_signals.items()):
            start_ms = int(signal.get("last_check_ms", int(signal["entry_time"]) + 1))
            if start_ms > server_time:
                continue
            # سقف الاستدراك حتى لا نجلب آلاف الشموع بعد توقف طويل
            start_ms = max(start_ms, server_time - MAX_CATCHUP_HOURS * HOUR_MS)

            klines_1m = self.binance.get_klines_range(
                symbol=symbol, interval="1m", start_time=start_ms, end_time=server_time,
            )

            event = evaluate_path_1m(
                klines_1m,
                float(signal["target_price"]),
                float(signal["stop_price"]),
            )
            if event is None and (klines_1m is None or klines_1m.empty):
                # احتياطي: شموع الساعة عند تعذّر شموع الدقيقة
                closed_df = self._closed_hours(klines_map.get(symbol), server_time)
                event = evaluate_path_1h(
                    closed_df,
                    int(signal["entry_bar_open_time"]),
                    float(signal["target_price"]),
                    float(signal["stop_price"]),
                )

            if event is not None:
                signal["max_price"] = max(float(signal.get("max_price", signal["entry_price"])), event["max_price"])
                if event["outcome"] == "win":
                    self.send_success_message(signal, event)
                else:
                    self.send_loss_message(signal, event)
                state_changed = True
                continue

            if klines_1m is not None and not klines_1m.empty:
                max_high = float(klines_1m["high"].max())
                signal["max_price"] = max(float(signal.get("max_price", signal["entry_price"])), max_high)
                last = klines_1m.iloc[-1]
                last_close = int(last["close_time"])
                # الشمعة الجارية تُعاد في الدورة القادمة لالتقاط آخر تحديث لها
                signal["last_check_ms"] = (
                    int(last["open_time"]) if last_close >= server_time else last_close + 1
                )
                state_changed = True

        if state_changed:
            self.store.save(self.state)

    # ─────────────────────────────────────────── اكتشاف القيعان الجديدة
    def _closed_hours(self, df, server_time: int):
        if df is None or df.empty:
            return df
        return df[df["close_time"] < server_time]

    def process_new_bottoms(self, klines_map: dict, server_time: int) -> None:
        open_signals = self.state["open_signals"]
        for symbol in self.symbols:
            closed_df = self._closed_hours(klines_map.get(symbol), server_time)
            if closed_df is None or closed_df.empty:
                continue

            bottom = find_latest_bottom(closed_df, self.settings)
            if bottom is None:
                continue

            last_seen = int(self.state["last_bottom_bar_time"].get(symbol, 0))
            if bottom["bar_open_time"] <= last_seen:
                continue

            # إشارة مفتوحة لنفس العملة: سجّل القاع كمرئي فقط (إشارة واحدة لكل عملة)
            if symbol in open_signals:
                self.state["last_bottom_bar_time"][symbol] = bottom["bar_open_time"]
                self.store.save(self.state)
                continue

            next_row = next_closed_bar(closed_df, bottom["bar_open_time"])
            if next_row is None:
                # لم تُغلق شمعة الدخول بعد — ننتظر الدورة القادمة
                continue

            # منع الإشارات المتأخرة (توقف البوت أو بيانات قديمة)
            age_ms = server_time - int(next_row["close_time"])
            if age_ms > int(self.settings.max_entry_age_hours * HOUR_MS):
                self.state["last_bottom_bar_time"][symbol] = bottom["bar_open_time"]
                self.store.save(self.state)
                self.logger.info("ZigZag: تخطّي قاع قديم لـ %s (عمر الإشارة %s دقيقة)", symbol, age_ms // MINUTE_MS)
                continue

            candidate = evaluate_entry_candle(bottom, next_row, self.settings)
            self.state["last_bottom_bar_time"][symbol] = bottom["bar_open_time"]

            if candidate is None:
                # حمراء أو دوجي أو إغلاق تحت القاع ⇒ يُلغى القاع وننتظر آخر
                self.store.save(self.state)
                self.logger.info("ZigZag: قاع %s أُلغي (شمعة تالية غير صالحة)", symbol)
                continue

            signal = {
                "trade_id": f"{symbol}-{candidate['entry_bar_open_time']}",
                "symbol": symbol,
                "entry_price": candidate["entry_price"],
                "target_price": candidate["target_price"],
                "stop_price": candidate["stop_price"],
                "bottom_low": candidate["bottom_low"],
                "bottom_bar_open_time": candidate["bottom_bar_open_time"],
                "entry_bar_open_time": candidate["entry_bar_open_time"],
                "entry_time": candidate["entry_time"],
                "last_check_ms": int(candidate["entry_time"]) + 1,
                "max_price": candidate["entry_price"],
                "halal_verdict": self.get_halal_verdict(symbol),
            }
            open_signals[symbol] = signal
            self.store.save(self.state)
            self.send_entry_message(signal)
            self.logger.info(
                "ZigZag: New signal for %s @ %s (target %s, stop %s)",
                symbol, candidate["entry_price"], candidate["target_price"], candidate["stop_price"],
            )

    def purge_pegged_stable_signals(self) -> None:
        """يحذف أي إشارة لعملة مستقرة مربوطة تسللت."""
        open_signals = self.state.get("open_signals", {})
        doomed = [s for s in list(open_signals) if is_pegged_stable_symbol(s, self.config.quote_asset)]
        if not doomed:
            return
        for symbol in doomed:
            signal = open_signals.pop(symbol, {}) or {}
            trade_id = str(signal.get("trade_id") or "")
            if trade_id:
                update_zigzag_exit(self.data_dir, trade_id, {
                    "outcome": "removed",
                    "exit_reason": "removed_pegged_stable",
                    "exit_time_ms": int(signal.get("entry_time") or 0),
                    "exit_price": signal.get("entry_price"),
                    "duration_minutes": 0,
                    "duration_text": "أُزيلت (عملة مستقرة مربوطة)",
                })
            self.append_event("removed_pegged", symbol, int(signal.get("entry_time") or 0))
        self.store.save(self.state)

    # ─────────────────────────────────────────── التشغيل
    def run_cycle(self) -> None:
        self.refresh_symbols(force=not self.symbols)
        self.refresh_halal_verdicts()
        self.purge_pegged_stable_signals()
        self.sync_open_signals_to_journal()
        server_time = self.binance.get_server_time()
        klines_map = self.binance.get_klines_for_symbols(
            self.symbols, self.interval, self.config.kline_limit
        )
        self.monitor_open_signals(server_time, klines_map)
        self.process_new_bottoms(klines_map, server_time)

    def run(self) -> None:
        self.refresh_symbols(force=True)
        self.logger.info("ZigZag bot started. %s", self.settings.describe())
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
        self.logger.info("ZigZag bot one-shot run started. %s", self.settings.describe())
        self.run_cycle()
        self.logger.info("ZigZag bot one-shot run completed.")


def build_zigzag_bot() -> ZigZagSignalBot:
    config = AppConfig.from_env()
    Path(ZZ_STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    return ZigZagSignalBot(config)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, AppConfig.from_env().log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    bot = build_zigzag_bot()
    bot.run()


if __name__ == "__main__":
    main()
