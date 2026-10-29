"""بوت نظام القاع (يومي) — نظام موازٍ مستقل تمامًا عن Target Trend.

لا يقرأ ولا يكتب أي ملف يخص النظام الآخر:
    الحالة:  data/state_qaa.json      (النظام الآخر: data/state.json)
    الدفتر:  data/trades_log_qaa.csv  (النظام الآخر: data/trades_log.csv)
    الكاش:   data/qaa/                (النظام الآخر: data/halal_verdicts.json)

الرسائل (كلها باسم المؤشر «نظام القاع (يومي)» + سطر الحكم الشرعي):
    📥 دخول — سعر/وقف القاع/هدف عرض 2R/حجم مقترح
    📈 تحديث وقف متحرك — عند ارتفاع الوقف ≥ عتبة (1% افتراضيًا) ليعدّل المستخدم يدويًا
    🎉/🛑 إغلاق — نسب فعلية وصافية، سبب الإغلاق (وقف/مهلة)، وأعلى سعر تحقق

قواعد النظام كما في الاختبارات (swing_bottom_backtest):
    دخول: شمعة خضراء فوق قاع مؤكد (3/1 صارم) وفوق EMA200، مهلة >3 شموع من آخر دخول.
    وقف:  يبدأ عند القاع ثم max(الوقف، أعلى قمة × 0.92) — يرتفع فقط.
    خروج: لمس الوقف أو انتهاء 40 يومًا — لا هدف ثابت (الهدف 2R عرض فقط).
    الفريم: يومي حصريًا (1h/4h خسارة بالأرقام).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from src.binance_client import BinanceClient
from src.config import AppConfig
from src.halal import ensure_verdict, refresh_if_stale
from src.qaa_journal import append_qaa_entry, update_qaa_exit
from src.qaa_strategy import (
    DAY_MS,
    QaaSettings,
    evaluate_entry,
    is_timeout,
    next_stop,
    should_raise_alert,
    stop_fill,
)
from src.state import StateStore
from src.telegram_client import TelegramClient, is_muted
from src.utils import (
    format_price,
    humanize_duration_ar,
    local_date_key_from_ms,
    ms_to_local_text,
)

MINUTE_MS = 60 * 1000
EVENT_RETENTION_DAYS = 120
STRATEGY_LABEL = "نظام القاع (يومي)"

_AR_MONTHS = (
    "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
    "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر",
)


def pct_label(value: float) -> str:
    """نسبة مختصرة أنيقة: +10.0% ← +10% (تبقى بالخانة عند الحاجة)."""
    return f"{value:+.1f}%".replace(".0%", "%")


def stamp_ms(ms: int, timezone_name: str) -> str:
    """ختم زمني مرتّب للرسائل: 22 أكتوبر — 6:16 صباحًا."""
    moment = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(timezone_name))
    hour12 = moment.hour % 12 or 12
    day_part = "صباحًا" if moment.hour < 12 else "مساءً"
    return f"{moment.day} {_AR_MONTHS[moment.month - 1]} — {hour12}:{moment.minute:02d} {day_part}"

# ─────────────────────────────────────────────────────────────────────────────
# استثناء أزواج العملات المستقرة المربوطة — نسخة مستقلة عن app.py (عزل تام)
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


# مسارات النظام الموازي — ثابتة بغض النظر عن STATE_FILE الخاص بالنظام الآخر
QAA_STATE_FILE = os.getenv("QAA_STATE_FILE", "data/state_qaa.json")
QAA_HALAL_DIR = os.getenv("QAA_HALAL_DIR", "data/qaa")


class QaaSignalBot:
    """بوت إشارات نظام القاع (يومي) — مسح كل أزواج USDT سبوت."""

    INTERVAL = "1d"  # الفريم يومي حصريًا — لا يتأثر بـ BINANCE_INTERVAL

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = logging.getLogger("qaa-signal-bot")
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
        self.settings = QaaSettings()
        self.interval = self.INTERVAL
        self.interval_ms = BinanceClient.INTERVAL_MS[self.interval]
        self.store = StateStore(QAA_STATE_FILE)
        self.state = self.store.load()
        self.data_dir = str(Path(QAA_STATE_FILE).parent)
        self.halal_dir = QAA_HALAL_DIR  # كاش شرعي منفصل — لا يلمس كاش النظام الآخر
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
            "QAA: Loaded %s spot symbols with quote asset %s%s",
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

    # ─────────────────────────────────────────── دفتر الصفقات (الملف المنفصل)
    def _build_trade_log_record(self, trade: dict) -> dict[str, object]:
        entry_time_ms = int(trade["entry_time"])
        return {
            "trade_id": trade["trade_id"],
            "symbol": trade["symbol"],
            "strategy": STRATEGY_LABEL,
            "entry_time_ms": entry_time_ms,
            "entry_time_local": ms_to_local_text(entry_time_ms, self.config.timezone_name),
            "entry_date_local": local_date_key_from_ms(entry_time_ms, self.config.timezone_name),
            "entry_price": trade["entry_price"],
            "pivot_stop": trade["pivot_stop"],
            "entry_stop_dist_pct": trade["stop_dist_pct"],
            "size_suggest_pct": trade["size_suggest_pct"],
            "target_price": trade["target_price"],
            "peak_price": trade.get("peak_price", trade["entry_price"]),
            "stop_updates_count": int(trade.get("stop_updates_count", 0)),
            "halal_verdict": trade.get("halal_verdict", ""),
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
        entry = float(trade["entry_price"])
        gross = ((float(exit_price) / entry) - 1.0) * 100.0
        net = gross - (2.0 * self.settings.commission_side_pct)
        peak = float(trade.get("peak_price", entry))
        peak_gain = (peak / entry - 1.0) * 100.0
        update_qaa_exit(
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
                "gross_return_pct": round(gross, 6),
                "net_return_pct": round(net, 6),
                "peak_price": peak,
                "peak_gain_pct": round(peak_gain, 6),
                "stop_updates_count": int(trade.get("stop_updates_count", 0)),
            },
        )

    def sync_open_trades_to_journal(self) -> None:
        for _symbol, trade in self.state.get("open_trades", {}).items():
            append_qaa_entry(self.data_dir, self._build_trade_log_record(trade))

    # ─────────────────────────────────────────── رسائل تيليجرام
    def _fmt_price(self, trade: dict, value: float | str) -> str:
        return format_price(float(value), self.price_ticks.get(str(trade.get("symbol", ""))))

    def _safe_send(self, text: str) -> None:
        """إرسال آمن: فشل رسالة واحدة (تجاوز 4096، شبكة...) لا يوقف دورة المراقبة.

        الدرس: يوم 2026-10-20 — تقرير بـ15 صفقة تجاوز حد تيليجرام (HTTP400)
        قتل الدورة قبل حفظ الحالة → أربع دورات متعاقبة فشلت ودفعت النظام18 ساعة.
        """
        try:
            self.telegram.send_message(text)
        except Exception:
            self.logger.error("QAA: فشل إرسال رسالة تيليجرام — تُجتاز وتُسجَّل وتُكمل الدورة", exc_info=True)

    def _send_trade_message(self, trade: dict, text: str) -> None:
        """يتجاوز رسائل الصفقة إذا فُتحت أثناء الإسكات (silent) حتى بعد الاستئناف."""
        if trade.get("silent"):
            self.logger.info(
                "QAA: صفقة صامتة %s — رسالة لم تُرسل (فُتحت أثناء الإسكات)",
                trade.get("symbol", "?"),
            )
            return
        self._safe_send(text)

    def send_entry_message(self, trade: dict) -> None:
        verdict = trade.get("halal_verdict") or self.get_halal_verdict(trade["symbol"])
        entry = float(trade["entry_price"])
        stop = float(trade["pivot_stop"])
        target = float(trade["target_price"])
        dist = float(trade["stop_dist_pct"])
        size = float(trade["size_suggest_pct"])
        text = (
            f"📥 إشارة دخول — {trade['symbol']}\n"
            f"{STRATEGY_LABEL} • يومي حصري\n"
            "\n"
            f"الدخول: {self._fmt_price(trade, entry)}\n"
            f"الوقف: {self._fmt_price(trade, stop)} ({pct_label(-dist)}) ← الهدف: "
            f"{self._fmt_price(trade, target)} ({pct_label(self.settings.target_r * dist)} — عرض فقط)\n"
            f"الحجم: {size:.1f}% من رأس المال | المخاطرة: {self.settings.risk_pct:.1f}%\n"
            f"الوقف المتحرك: +{self.settings.trail_pct:g}% من القمة "
            f"(أول تحديث عند +{self.settings.stop_alert_step_pct:g}%)\n"
            "\n"
            f"وقت الإشارة: {stamp_ms(int(trade['entry_time']), self.config.timezone_name)}\n"
            f"⚖️ الحكم الشرعي: {verdict}"
        )
        self._send_trade_message(trade, text)
        self.append_event("entry", trade["symbol"], int(trade["entry_time"]))
        append_qaa_entry(self.data_dir, self._build_trade_log_record(trade))

    def send_stop_update_message(
        self, trade: dict, old_stop: float, new_stop: float, event_ms: int, current_price: float | None = None
    ) -> None:
        """رسالة ارتفاع الوقف المتحرك — ليعدّل المستخدم وقفه يدويًا على منصته.

        current_price: السعر الحالي وقت التحديث ليقرّر المستخدم مسارته/تعديل وقفه.
        """
        verdict = trade.get("halal_verdict") or self.get_halal_verdict(trade["symbol"])
        entry = float(trade["entry_price"])
        rise_pct = (float(new_stop) / float(old_stop) - 1.0) * 100.0
        lock_pct = (float(new_stop) / entry - 1.0) * 100.0
        lock_part = (
            f"ربح مضمون: {pct_label(lock_pct)}"
            if lock_pct >= 0
            else f"⚠️ الوقف دون الدخول ({pct_label(lock_pct)})"
        )
        price_line = ""
        dist_line = ""
        if current_price:
            now_pct = (float(current_price) / entry - 1.0) * 100.0
            gap = (float(current_price) / float(new_stop) - 1.0) * 100.0
            gap_text = f"{abs(gap):.1f}% فوق الوقف الجديد" if gap >= 0 else f"{abs(gap):.1f}% دون الوقف الجديد"
            price_line = (
                f"الدخول: {self._fmt_price(trade, entry)} | "
                f"السعر الآن: {self._fmt_price(trade, current_price)} ({pct_label(now_pct)})\n"
            )
            dist_line = f"المسافة: {gap_text} | "
        text = (
            f"📈 وقف متحرك — {trade['symbol']}\n"
            f"{STRATEGY_LABEL}\n"
            "\n"
            f"{price_line}"
            f"الوقف: {self._fmt_price(trade, old_stop)} ← {self._fmt_price(trade, new_stop)} "
            f"({pct_label(rise_pct)}) | {lock_part}\n"
            f"{dist_line}تحديث رقم: {int(trade.get('stop_updates_count', 0)) + 1}\n"
            "\n"
            f"⬆️ ارفع وقفك إلى {self._fmt_price(trade, new_stop)} على منصتك\n"
            "\n"
            f"{stamp_ms(int(event_ms), self.config.timezone_name)}\n"
            f"⚖️ الحكم الشرعي: {verdict}"
        )
        self._send_trade_message(trade, text)
        self.append_event("stop_alert", trade["symbol"], int(event_ms))

    def send_close_message(
        self, trade: dict, exit_price: float, event_time_ms: int, reason: str, reason_ar: str
    ) -> None:
        """رسالة انتهاء الصفقة — تفصيلية: نسب فعلية وصافية، سبب الإغلاق، وأعلى سعر."""
        verdict = trade.get("halal_verdict") or self.get_halal_verdict(trade["symbol"])
        entry = float(trade["entry_price"])
        gross = (float(exit_price) / entry - 1.0) * 100.0
        net = gross - (2.0 * self.settings.commission_side_pct)
        if net > 0:
            outcome, emoji, headline = "win", "🎉", f"إغلاق رابح — {trade['symbol']}"
        else:
            outcome, emoji, headline = "loss", "🛑", f"إغلاق خاسر — {trade['symbol']}"
        peak = float(trade.get("peak_price", entry))
        peak_gain = (peak / entry - 1.0) * 100.0
        duration = humanize_duration_ar(trade["entry_time"], event_time_ms)
        text = (
            f"{emoji} {headline}\n"
            f"{STRATEGY_LABEL}\n"
            "\n"
            f"الدخول: {self._fmt_price(trade, entry)} ← الخروج: {self._fmt_price(trade, exit_price)}\n"
            f"الصافي: {net:+.2f}% (الخام: {gross:+.2f}%)\n"
            "\n"
            f"سبب الإغلاق: {reason_ar}\n"
            f"المدة: {duration} | أعلى سعر: {self._fmt_price(trade, peak)} (+{peak_gain:.2f}%)\n"
            "\n"
            f"{stamp_ms(int(event_time_ms), self.config.timezone_name)}\n"
            f"⚖️ الحكم الشرعي: {verdict}"
        )
        self._send_trade_message(trade, text)
        self.append_event("exit_win" if outcome == "win" else "exit_loss", trade["symbol"], int(event_time_ms))
        self._record_trade_exit(trade, outcome, reason, int(event_time_ms), float(exit_price))

    def close_trade(self, symbol: str, exit_bar_open_time: int, reason: str) -> None:
        self.state["open_trades"].pop(symbol, None)
        self.state.setdefault("last_exit_bar_time", {})[symbol] = exit_bar_open_time
        self.store.save(self.state)
        self.logger.info("QAA: Closed %s بسبب %s", symbol, reason)

    def _stop_reason(self, trade: dict, stop_value: float) -> tuple[str, str]:
        """سبب الإغلاق عند الوقف: متحرك (رُفع فوق القاع) أم وقف القاع نفسه."""
        pivot_stop = float(trade.get("pivot_stop", trade["stop_price"]))
        if float(stop_value) > pivot_stop:
            return "stop_trail", "لمس الوقف المتحرك 🔺"
        return "stop_pivot", "لمس وقف القاع"

    # ─────────────────────────────────────────── متابعة لحظية (شموع 1 دقيقة)
    def monitor_open_trades_intrabar(self, now_ms: int) -> None:
        """يتابع كل صفقة مفتوحة على شموع 1m: لمس الوقف يُغلق، وارتفاع القمّة يرفع الوقف.

        ترتيب كل شمعة مطابق للاختبارات: تعديل الوقف من قمم سابقة ← فحص القاع ←
        رسالة عند تجاوز عتبة الارتفاع ← تحديث القمة. الشمعة نفسها لا تحمي نفسها.
        """
        open_trades = self.state.get("open_trades", {})
        if not open_trades:
            return

        state_changed = False
        for symbol, trade in list(open_trades.items()):
            start_ms = int(trade.get("last_check_ms", int(trade["entry_time"]) + 1))
            if start_ms > now_ms:
                continue

            klines = self.binance.get_klines_range(
                symbol=symbol, interval="1m", start_time=start_ms, end_time=now_ms,
            )
            if klines.empty:
                continue

            closed_trade = False
            for row in klines.itertuples(index=False):
                low = float(row.low)
                high = float(row.high)
                open_price = float(row.open)
                event_ms = int(row.close_time)
                peak = float(trade["peak_price"])
                cur_stop = float(trade["stop_price"])

                # 1) رفع الوقف أولاً من قمم الشموع السابقة (بلا انتحار نفس الشمعة)
                new_stop = next_stop(cur_stop, peak, self.settings)
                if new_stop > cur_stop:
                    trade["stop_price"] = new_stop  # دائمًا (صامت أو برسالة)

                # 2) فحص الوقف — تنفيذ الفجوة عند الفتح إن فاقته
                if low <= new_stop:
                    fill = stop_fill(new_stop, open_price)
                    reason_key, reason_ar = self._stop_reason(trade, new_stop)
                    self.send_close_message(trade, fill, event_ms, reason_key, reason_ar)
                    self.close_trade(symbol, event_ms // self.interval_ms * self.interval_ms, reason_key)
                    closed_trade = True
                    break

                # 3) رسالة تحديث الوقف عند تجاوز العتبة عن آخر رسالة (القيمة سُجّلت دائمًا أعلاه)
                if new_stop > cur_stop:
                    step = self.settings.stop_alert_step_pct
                    if should_raise_alert(new_stop, float(trade.get("last_stop_alert_price", cur_stop)), step):
                        self.send_stop_update_message(trade, cur_stop, new_stop, event_ms, float(row.close))
                        trade["last_stop_alert_price"] = new_stop
                        trade["stop_updates_count"] = int(trade.get("stop_updates_count", 0)) + 1

                # 4) قمة الشمعة (تُستخدم لرفع الوقف في الشمعة التالية)
                if high > float(trade["peak_price"]):
                    trade["peak_price"] = high

            if not closed_trade:
                trade["last_check_ms"] = int(klines.iloc[-1]["close_time"]) + 1
                state_changed = True

        if state_changed:
            self.store.save(self.state)

    # ─────────────────────────────────────────── معالجة الشمعة اليومية المغلقة
    def process_new_closed_day(self, server_time: int) -> None:
        self.purge_pegged_stable_trades()
        last_processed = int(self.state.get("last_processed_open_time", 0))
        # محااذاة شبكة الفريم اليومي (منتصف الليل UTC) — تغيير أي إعداد لا يترك البوت يطارد شموعًا غير موجودة
        if last_processed and last_processed % self.interval_ms:
            last_processed = (last_processed // self.interval_ms) * self.interval_ms
            self.state["last_processed_open_time"] = last_processed
            self.store.save(self.state)

        klines_map = self.binance.get_klines_for_symbols(self.symbols, self.interval, self.config.kline_limit)
        try:
            sample = next(iter(klines_map.values()))
            if sample is None or sample.empty:
                self.logger.warning("No klines data; skipping cycle")
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
            "QAA: Processing closed day (%s): target=%s (last_processed=%s)",
            self.interval, target_closed, last_processed,
        )

        # catch-up: عالج كل الشموع منذ آخر معالجة (حد أقصى 24 يومًا)
        if last_processed == 0:
            cursor = target_closed
        else:
            cursor = max(last_processed + self.interval_ms, target_closed - 23 * self.interval_ms)
        if cursor > target_closed:
            cursor = last_processed + self.interval_ms
        while cursor <= target_closed:
            self._process_one_closed_day(klines_map, cursor)
            self.state["last_processed_open_time"] = cursor
            self.store.save(self.state)
            cursor += self.interval_ms

    def _process_one_closed_day(self, klines_map: dict, last_closed_open_time: int) -> None:
        """يوم واحد مغلق: متابعة المفتوحة (وقف/مهلة/رفع الوقف) ثم فتح الجديد."""
        open_trades = self.state.get("open_trades", {})

        # ── 1) متابعة الصفقات المفتوحة ──
        for symbol, trade in list(open_trades.items()):
            df = klines_map.get(symbol)
            if df is None or df.empty:
                continue
            candle = df[df["open_time"] == last_closed_open_time]
            if candle.empty:
                continue
            row = candle.iloc[-1]
            candle_open = float(row["open"])
            candle_high = float(row["high"])
            candle_low = float(row["low"])
            candle_close = float(row["close"])
            candle_close_time = int(row["close_time"])

            # 1a) وقف الخسارة (احتياطي إن فاتت المتابعة اللحظية) — تنفيذ الفجوة عند الفتح
            cur_stop = float(trade["stop_price"])
            if candle_low <= cur_stop:
                fill = stop_fill(cur_stop, candle_open)
                reason_key, reason_ar = self._stop_reason(trade, cur_stop)
                self.send_close_message(trade, fill, candle_close_time, reason_key, reason_ar)
                self.close_trade(symbol, last_closed_open_time, reason_key)
                continue

            # 1b) المهلة: انتهاء 40 يومًا = خروج بسعر إغلاق الشمعة (تسبق رفع الوقف)
            if is_timeout(int(trade["entry_bar_open_time"]), last_closed_open_time, self.settings.max_hold):
                self.send_close_message(
                    trade, candle_close, candle_close_time,
                    "timeout", f"انتهت المهلة ({self.settings.max_hold} يومًا)",
                )
                self.close_trade(symbol, last_closed_open_time, "timeout")
                continue

            # 1c) رفع الوقف (逮补 إن فات لحظيًا) + رسالة عند العتبة
            new_stop = next_stop(cur_stop, float(trade["peak_price"]), self.settings)
            if new_stop > cur_stop:
                trade["stop_price"] = new_stop
                step = self.settings.stop_alert_step_pct
                if should_raise_alert(new_stop, float(trade.get("last_stop_alert_price", cur_stop)), step):
                    self.send_stop_update_message(trade, cur_stop, new_stop, candle_close_time, candle_close)
                    trade["last_stop_alert_price"] = new_stop
                    trade["stop_updates_count"] = int(trade.get("stop_updates_count", 0)) + 1

            # 1d) قمة الشمعة — الوقف يُرفع من قمة اليوم في الغد (لا حماية بنفس الشمعة)
            trade["peak_price"] = max(float(trade["peak_price"]), candle_high)

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

            signal = evaluate_entry(
                closed_df,
                self.settings,
                int(self.state.get("last_entry_bar_time", {}).get(symbol, 0)),
                int(self.state.get("last_exit_bar_time", {}).get(symbol, 0)),
            )
            if not signal:
                continue

            trade = {
                "trade_id": f"{symbol}-{signal['bar_open_time']}",
                "symbol": symbol,
                "entry_price": signal["entry_price"],
                "stop_price": signal["stop_price"],          # وقف القاع — يبدأ هنا
                "pivot_stop": signal["stop_price"],           # الأصلي للعرض والتمييز
                "target_price": signal["target_price"],       # 2R — عرض فقط
                "stop_dist_pct": signal["stop_dist_pct"],
                "size_suggest_pct": signal["size_suggest_pct"],
                "entry_time": signal["bar_close_time"],
                "entry_bar_open_time": signal["bar_open_time"],
                "last_check_ms": signal["bar_close_time"] + MINUTE_MS,
                "peak_price": signal["peak_price"],           # قمة شمعة الإشارة (كما في Pine)
                "last_stop_alert_price": signal["stop_price"],
                "stop_updates_count": 0,
                "halal_verdict": self.get_halal_verdict(symbol),
                # صفقة فُتحت أثناء الإسكات: تُسجَّل كاملة لكن لا تُرسل رسائلها
                # (دخول/وقف متحرك/إغلاق) حتى بعد رفع الإسكات.
                "silent": is_muted(),
            }
            self.state["open_trades"][symbol] = trade
            self.state.setdefault("last_entry_bar_time", {})[symbol] = signal["bar_open_time"]
            self.store.save(self.state)
            self.send_entry_message(trade)
            self.logger.info("QAA: New trade opened for %s @ %s", symbol, signal["entry_price"])

    def purge_pegged_stable_trades(self) -> None:
        """يحذف أي صفقة لعملة مستقرة مربوطة تسللت (لا دخل للضجيج في نظام القاع)."""
        open_trades = self.state.get("open_trades", {})
        doomed = [s for s in list(open_trades) if is_pegged_stable_symbol(s, self.config.quote_asset)]
        if not doomed:
            return
        for symbol in doomed:
            trade = open_trades.pop(symbol, {}) or {}
            self.state.get("last_entry_bar_time", {}).pop(symbol, None)
            trade_id = str(trade.get("trade_id") or "")
            if trade_id:
                update_qaa_exit(self.data_dir, trade_id, {
                    "outcome": "removed",
                    "exit_reason": "removed_pegged_stable",
                    "exit_time_ms": int(trade.get("entry_time") or 0),
                    "exit_price": trade.get("entry_price"),
                    "duration_minutes": 0,
                    "duration_text": "أُزيلت (عملة مستقرة مربوطة)",
                })
            self.append_event("removed_pegged", symbol, int(trade.get("entry_time") or 0))

    # ─────────────────────────────────────────── التشغيل
    def run_cycle(self) -> None:
        self.refresh_symbols(force=not self.symbols)
        self.refresh_halal_verdicts()
        self.sync_open_trades_to_journal()
        server_time = self.binance.get_server_time()
        self.monitor_open_trades_intrabar(server_time)
        self.process_new_closed_day(server_time)

    def run(self) -> None:
        self.refresh_symbols(force=True)
        self.logger.info("QAA bot started. %s", self.settings.describe())
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
        self.logger.info("QAA bot one-shot run started. %s", self.settings.describe())
        self.run_cycle()
        self.logger.info("QAA bot one-shot run completed.")


def build_qaa_bot() -> QaaSignalBot:
    config = AppConfig.from_env()
    Path(QAA_STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    return QaaSignalBot(config)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, AppConfig.from_env().log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    bot = build_qaa_bot()
    bot.run()


if __name__ == "__main__":
    main()
