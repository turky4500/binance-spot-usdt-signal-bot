"""بوت إشارات Smart Flow PRO MAX (نسخة معايَرة) — نظام موازٍ مستقل تمامًا.

لا يقرأ ولا يكتب أي ملف يخص الأنظمة الأخرى:
    الحالة:        data/state_smartflow.json   (Target Trend: state.json — QAA: state_qaa.json — SA: state_sa.json)
    الحكم الشرعي:  data/smartflow/halal_verdicts.json (كاش منفصل عن data/ وdata/qaa/ وdata/sa/)
    سجل الإشارات:  data/smartflow/signals_log.csv

الرسائل (🟢 شراء / 🔴 بيع) — إشارات للاطلاع فقط بلا دورة صفقات:
    - فرضية v1.1: انقلاب نافذة القمة/القاع (تفاصيلها في src/smartflow_strategy.py)
    - كل رسالة متبوعة بسطر الحكم الشرعي من cryptohalal.cc وسطر «نسخة قيد المعايرة»
    - دورة الشغيل: تطابق انتهاء شمعة 1H مع معالجة واحدة لها بغض النظر عن عدد الدورات.
"""
from __future__ import annotations

import csv
import logging
import os
import time
from pathlib import Path

from src.binance_client import BinanceClient
from src.config import AppConfig
from src.halal import ensure_verdict, refresh_if_stale
from src.smartflow_strategy import SmartFlowSettings, evaluate
from src.state import StateStore
from src.telegram_client import TelegramClient
from src.utils import format_price, ms_to_local_text

EVENT_RETENTION_DAYS = 120
STRATEGY_LABEL = "Smart Flow PRO MAX (نسخة معايَرة)"
CALIBRATION_NOTE = "⚠️ نسخة تقريبية قيد المعايرة مع نسخة البائع (الكود الأصلي مغلق)"

# ─────────────────────────────────────────────────────────────────────────────
# استثناء أزواج العملات المستقرة المربوطة — نسخة مستقلة (عزل تام كما في الأنظمة الأخرى)
# ─────────────────────────────────────────────────────────────────────────────
EXCLUDE_PEGGED_STABLES = os.getenv("EXCLUDE_PEGGED_STABLES", "1").strip().lower() not in ("0", "false", "no", "off")

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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return raw.lower() not in ("0", "false", "no", "off")


# ─────────────────────────────────────────────────────────────────────────────
# سجل الإشارات (CSV)
# ─────────────────────────────────────────────────────────────────────────────
SIGNAL_LOG_HEADER = [
    "time_local", "time_ms", "symbol", "kind",
    "close", "rsi", "ema20", "ema50", "ema10", "sma20", "atr",
    "breakout_level", "stop_level",
]


class SmartFlowSignalBot:
    """بوت إشارات Smart Flow — مسح كل أزواج USDT سبوت على فريم 1H."""

    INTERVAL = "1h"  # فريم الإنتاج كما اختار المالك — المنطق نفسه يعمل على أي فريم

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = logging.getLogger("smartflow-signal-bot")
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
        self.settings = SmartFlowSettings.from_env()
        self.interval = self.INTERVAL
        self.interval_ms = BinanceClient.INTERVAL_MS[self.interval]
        self.kline_limit = max(_int_env("SMARTFLOW_KLINE_LIMIT", 200), 100)
        self.max_catchup_hours = max(_int_env("SMARTFLOW_MAX_CATCHUP_HOURS", 24), 1)

        # مسارات النظام الموازي — تُقرأ هنا (وليس عند الاستيراد) ليسهل الاختبار
        self.state_file = os.getenv("SMARTFLOW_STATE_FILE", "data/state_smartflow.json")
        self.halal_dir = os.getenv("SMARTFLOW_HALAL_DIR", "data/smartflow")
        self.log_path = Path(os.getenv("SMARTFLOW_SIGNALS_LOG", "data/smartflow/signals_log.csv"))
        self.dry_run = _env_bool("SMARTFLOW_DRY_RUN", False)
        self.calibration_note_enabled = _env_bool("SMARTFLOW_CALIBRATION_NOTE", True)

        self.store = StateStore(self.state_file)
        self.state = self.store.load()
        self.price_ticks: dict[str, str] = {}
        self.halal_verdicts: dict[str, int] = {}
        self.symbols: list[str] = []
        self.last_symbols_refresh = 0.0
        self.last_signal_log_key = ""

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
            "SmartFlow: Loaded %s spot symbols with quote asset %s%s",
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

    # ─────────────────────────────────────────── الإرسال والسجل
    def _fmt_price(self, symbol: str, value: float) -> str:
        return format_price(float(value), self.price_ticks.get(symbol))

    def _send(self, text: str) -> None:
        if self.dry_run:
            self.logger.info("[DRY-RUN] رسالة تيليجرام:\n%s", text)
            return
        try:
            self.telegram.send_message(text)
        except Exception:
            self.logger.error("SmartFlow: فشل إرسال رسالة تيليجرام — تتجاوز وتُسجَّل وتُكمّل الدورة", exc_info=True)

    def send_signal_message(self, symbol: str, sig: dict, bar_close_time_ms: int) -> None:
        kind = sig["kind"]
        if kind == "buy":
            emoji = "🟢"
            side = "شراء"
            level_word = "اختراق أعلى قمة نافذة"
        else:
            emoji = "🔴"
            side = "بيع"
            level_word = "كسر أدنى قاع نافذة"
        tick_line = (
            f"{emoji} إشارة {side} — {STRATEGY_LABEL}\n"
            f"الزوج: {symbol}\n"
            f"الفريم: 1H\n"
            f"سعر الدخول (افتتاح شمعة الإشارة): {self._fmt_price(symbol, sig['open'])}\n"
            f"سعر الإغلاق: {self._fmt_price(symbol, sig['close'])}\n"
            f"{level_word} ({self.settings.break_len} شموع): "
            f"{self._fmt_price(symbol, sig['breakout_level'])}\n"
            f"⏹️ وقف مبدئي (قيد المعايرة): {self._fmt_price(symbol, sig['stop_level'])}\n"
            f"الاتجاه: {'صاعد — EMA20 فوق EMA50' if sig['trend_up'] else 'هابط — EMA20 تحت EMA50'}\n"
            f"RSI({self.settings.rsi_len}): {sig['rsi']:.1f} · "
            f"SMA{self.settings.sma_len}: {self._fmt_price(symbol, sig['sma'])} · "
            f"EMA{self.settings.ema_fast_len}: {self._fmt_price(symbol, sig['ema_fast'])} · "
            f"ATR{self.settings.atr_len}: {self._fmt_price(symbol, sig['atr'])}\n"
            f"وقت الإشارة: {ms_to_local_text(int(bar_close_time_ms), self.config.timezone_name)}\n"
            f"─────────────\n"
        )
        if self.calibration_note_enabled:
            tick_line += f"{CALIBRATION_NOTE}\n─────────────\n"
        tick_line += f"الحكم الشرعي: {self.get_halal_verdict(symbol)}"
        self._send(tick_line)

    def append_signal_log(self, row: dict) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        exists = self.log_path.exists()
        with self.log_path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if not exists:
                writer.writerow(SIGNAL_LOG_HEADER)
            writer.writerow([
                row.get("time_local", ""),
                row.get("time_ms", ""),
                row.get("symbol", ""),
                row.get("kind", ""),
                row.get("close", ""),
                row.get("rsi", ""),
                row.get("ema20", ""),
                row.get("ema50", ""),
                row.get("ema10", ""),
                row.get("sma20", ""),
                row.get("atr", ""),
                row.get("breakout_level", ""),
                row.get("stop_level", ""),
            ])

    # ─────────────────────────────────────────── قاعدة منع الإزعاج
    def send_allowed(self, symbol: str, kind: str, bar_open_time: int) -> bool:
        """on_flip: أول إشارة للرمز أو انقلاب الاتجاه فقط.

        v1.2 alternate_only: التبادل الإلزامي (معايَر من لقطة Binance 1H) —
        بعد شراء لا يُقبل إلا بيع وبعد بيع لا يُقبل إلا شراء حتى انقلاب جديد؛
        (المعايرة: المؤشر تجاهل 6 بيعات مكررة بعد بيع 10-19 وحالة «انتظار»).
        """
        if self.settings.signal_mode == "every_bar":
            return True
        last = (self.state.get("last_signal") or {}).get(symbol)
        if not last:
            return True
        if str(last.get("kind")) != kind:
            return True  # انقلاب الاتجاه — مسموح دائمًا
        if self.settings.alternate_only:
            return False
        gap = (int(bar_open_time) - int(last.get("bar_open_time", 0))) // self.interval_ms
        return gap >= self.settings.cooldown_bars

    # ─────────────────────────────────────────── معالجة الشمعة المغلقة
    def process_new_closed_hour(self, server_time: int) -> None:
        last_processed = int(self.state.get("last_processed_open_time", 0))
        # محاذاة شبكة الفريم (ساعة كاملة UTC) — التغييرات بعدها لا تُسرِق البوت
        if last_processed and last_processed % self.interval_ms:
            last_processed = (last_processed // self.interval_ms) * self.interval_ms
            self.state["last_processed_open_time"] = last_processed
            self.store.save(self.state)

        klines_map = self.binance.get_klines_for_symbols(self.symbols, self.interval, self.kline_limit)
        try:
            sample = next(iter(klines_map.values()))
            if sample is None or sample.empty:
                self.logger.warning("SmartFlow: no klines data; skipping cycle")
                return
            last_in_data = int(sample.iloc[-1]["open_time"])
            close_time_of_last = int(sample.iloc[-1].get("close_time", last_in_data + self.interval_ms - 1))
        except StopIteration:
            return

        # الشمعة المغلقة فعلًا = آخر شمعة انتهى زمنها
        if close_time_of_last >= server_time:
            last_in_data = last_in_data - self.interval_ms

        target_closed = last_in_data
        if last_processed >= target_closed:
            return

        self.logger.info(
            "SmartFlow: Processing closed hour (%s): target=%s (last_processed=%s)",
            self.interval, target_closed, last_processed,
        )

        max_catchup_ms = (self.max_catchup_hours - 1) * self.interval_ms
        if last_processed == 0:
            cursor = target_closed  # أول تشغيل: أحدث شمعة فقط — بلا تاريخ
        else:
            cursor = max(last_processed + self.interval_ms, target_closed - max_catchup_ms)

        while cursor <= target_closed:
            self._process_one_closed_hour(klines_map, cursor)
            self.state["last_processed_open_time"] = cursor
            self.store.save(self.state)
            cursor += self.interval_ms

    def _process_one_closed_hour(self, klines_map: dict, last_closed_open_time: int) -> None:
        for symbol in self.symbols:
            df = klines_map.get(symbol)
            if df is None or df.empty:
                continue
            closed_df = df[df["open_time"] <= last_closed_open_time]
            if closed_df.empty:
                continue
            if int(closed_df.iloc[-1]["open_time"]) != last_closed_open_time:
                continue

            sig = evaluate(closed_df, self.settings)
            if not sig:
                continue

            kind = str(sig["kind"])
            if not self.send_allowed(symbol, kind, last_closed_open_time):
                continue

            bar_close_time = int(closed_df.iloc[-1]["close_time"])
            self.send_signal_message(symbol, sig, bar_close_time)
            self.append_event(f"signal_{kind}", symbol, bar_close_time)
            self.state.setdefault("last_signal", {})[symbol] = {
                "kind": kind,
                "bar_open_time": last_closed_open_time,
            }
            self.append_signal_log({
                "time_local": ms_to_local_text(bar_close_time, self.config.timezone_name),
                "time_ms": bar_close_time,
                "symbol": symbol,
                "kind": kind,
                "close": sig["close"],
                "rsi": round(sig["rsi"], 4),
                "ema20": sig["ema_mid"],
                "ema50": sig["ema_slow"],
                "ema10": sig["ema_fast"],
                "sma20": sig["sma"],
                "atr": sig["atr"],
                "breakout_level": sig["breakout_level"],
                "stop_level": sig["stop_level"],
            })
        self.store.save(self.state)

    # ─────────────────────────────────────────── التشغيل
    def run_cycle(self) -> None:
        self.refresh_symbols(force=not self.symbols)
        self.refresh_halal_verdicts()
        server_time = self.binance.get_server_time()
        self.process_new_closed_hour(server_time)

    def run(self) -> None:
        self.refresh_symbols(force=True)
        self.logger.info("SmartFlow bot started. %s", self.settings.describe())
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
        self.logger.info("SmartFlow one-shot run started. %s", self.settings.describe())
        self.run_cycle()
        self.logger.info("SmartFlow one-shot run completed.")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(float(raw)) if raw else default
    except ValueError:
        return default


def build_smartflow_bot() -> SmartFlowSignalBot:
    config = AppConfig.from_env()
    Path(os.getenv("SMARTFLOW_STATE_FILE", "data/state_smartflow.json")).parent.mkdir(parents=True, exist_ok=True)
    return SmartFlowSignalBot(config)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, AppConfig.from_env().log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    bot = build_smartflow_bot()
    bot.run()


if __name__ == "__main__":
    main()
