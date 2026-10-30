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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src.binance_client import BinanceClient
from src.config import AppConfig
from src.halal import ensure_verdict, refresh_if_stale
from src.state import StateStore
from src.telegram_client import TelegramClient, is_muted
from src.utils import (
    format_price,
    humanize_duration_ar,
    local_date_key_from_ms,
    ms_to_local_text,
    weekday_ar_from_ms,
)
from src.zigzag import (
    HOUR_MS,
    MINUTE_MS,
    STRATEGY_LABEL,
    ZigZagSettings,
    evaluate_entry_candle,
    evaluate_path_1h,
    evaluate_path_1m,
    ema_at_bar,
    ema_at_bar_from_candles,
    find_latest_bottom,
    next_closed_bar,
)
from src.zigzag_journal import append_zigzag_entry, load_zigzag_rows, update_zigzag_exit

EVENT_RETENTION_DAYS = 120
MAX_CATCHUP_HOURS = 24  # أقصى فترة استدراك لشموع الدقيقة عند توقف البوت (منع إشارات قديمة)

# مسارات النظام المستقل — ثابتة بغض النظر عن متغيرات الأنظمة الأخرى
ZZ_STATE_FILE = os.getenv("ZZ_STATE_FILE", "data/state_zigzag.json")
ZZ_HALAL_DIR = os.getenv("ZZ_HALAL_DIR", "data/zigzag")
# قائمة استبعاد يدوية (رموز أسهم/ETF مرمّزة، ذهب مرمّز، رموز غير مرغوبة).
# ملف نصي بسيط: رمز في كل سطر، و«#» للتعليقات. الملف غير موجود = لا استبعاد.
ZZ_EXCLUDE_FILE = os.getenv("ZZ_EXCLUDE_FILE", "config/zz_exclude.txt")
# ── استبعاد الأسهم/الصناديق المرمّزة آليًا (2026-10-30) ──
# Binance يسمّي الأصل المرمّز للأسهم/الصناديق بـ<التيكر>+B (SPYB, NVDAB, JPMB,
# INTCB…). القائمة اليدوية وحدها لا تكفي: ظهرت رموز جديدة (JPMB/INTCB) بعد ساعة
# من إضافة الملف. لذلك: أي أصل تنتهي قاعدته بحرف B يُستبعد افتراضيًا، ما لم يكن
# في قائمة الإبقاء أدناه (عملات رقمية حقيقية) أو في ملف الاستبعاد بسطر «!SYMBOL».
ZZ_EXCLUDE_B_SUFFIX = os.getenv("ZZ_EXCLUDE_B_SUFFIX", "1").strip().lower() not in (
    "0", "false", "no", "off"
)
CRYPTO_B_BASES = frozenset({
    "ARB", "BB", "BBB", "BNB", "BOTB", "CKB", "DGB", "MUB", "PENGB", "QNTB",
    "SHIB", "TRB", "USDEB", "WENB", "YB",
})

# ── تجميع الرسائل (2026-10-30) ──
# بدل رسالة لكل عملة، تُجمع كل إشارات الدورة في رسالة واحدة (أو عدة رسائل
# مقسّمة عند تجاوز الحد). التوقيت لم يتغيّر: الرسالة تُرسل في نفس الدورة التي
# حدث فيها اللمس/الإغلاق (كل 5 دقائق)، أي «بالوقت الفعلي» بلا انتظار إغلاق شمعة.
MSG_BATCH_MAX_ITEMS = int(os.getenv("ZZ_MSG_BATCH_MAX_ITEMS", "12"))
MSG_BATCH_MAX_CHARS = int(os.getenv("ZZ_MSG_BATCH_MAX_CHARS", "3500"))

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


def load_excluded_symbols(path: str) -> set[str]:
    """رموز مستبعدة من ملف نصي (سطر لكل رمز، «#» للتعليقات)."""
    return set(load_exclusion_config(path)[0])


def load_exclusion_config(path: str) -> tuple[set[str], set[str]]:
    """يقرأ ملف الاستبعاد: يعيد (المستبعدة، المُبقاة إجباريًا).

    الصيغة: رمز في كل سطر. السطر الذي يبدأ بـ«!» يعني «أبقِ هذا الرمز دائمًا»
    (يُستخدم لاستثناء عملة رقمية من قاعدة استبعاد ما ينتهي بـ B).
    """
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return set(), set()
    excluded: set[str] = set()
    forced_keep: set[str] = set()
    for line in lines:
        token = line.split("#", 1)[0].strip().upper()
        if not token:
            continue
        if token.startswith("!"):
            forced_keep.add(token[1:].strip())
        else:
            excluded.add(token)
    return excluded, forced_keep


def is_tokenized_stock_symbol(symbol: str, quote_asset: str = "USDT") -> bool:
    """هل الزوج لأصل تنتهي قاعدته بـ B (نمط أسهم/صناديق Binance المرمّزة)؟

    المثال: SPYBUSDT / NVDABUSDT / JPMBUSDT ⇒ True
            ARBUSDT / CKBUSDT / SOLUSDT   ⇒ False (ARB/CKB عملات رقمية)
    """
    symbol = (symbol or "").upper()
    quote = (quote_asset or "USDT").upper()
    if not symbol.endswith(quote) or len(symbol) <= len(quote):
        return False
    base = symbol[: -len(quote)]
    if base in CRYPTO_B_BASES:
        return False
    return base.endswith("B")


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
        pegged_excluded = len(all_symbols) - len(self.symbols)

        # استبعاد الأسهم/الصناديق المرمّزة آليًا (نمط <تيكر>B) — بلا حاجة لتحديث الملف
        blocklist, forced_keep = load_exclusion_config(ZZ_EXCLUDE_FILE)
        if ZZ_EXCLUDE_B_SUFFIX:
            before = len(self.symbols)
            self.symbols = [
                s for s in self.symbols
                if s.upper() in forced_keep
                or not is_tokenized_stock_symbol(s, self.config.quote_asset)
            ]
            self.logger.info(
                "ZigZag: استُثني %s رمزًا بنمط الأسهم المرمّزة (قاعدة تنتهي بـ B)",
                before - len(self.symbols),
            )

        # استبعاد يدوي إضافي من الملف (رموز أخرى غير مرغوبة)
        if blocklist:
            before = len(self.symbols)
            self.symbols = [s for s in self.symbols if s.upper() not in blocklist]
            self.logger.info(
                "ZigZag: استُثني %s رمزًا من قائمة الاستبعاد (%s)",
                before - len(self.symbols), ZZ_EXCLUDE_FILE,
            )

        self.last_symbols_refresh = now
        excluded = pegged_excluded
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

    def _send_trade_message(self, signal: dict, text: str) -> None:
        if signal.get("silent"):
            self.logger.info("ZigZag: صفقة صامتة %s — رسالة لم تُرسل (فُتحت أثناء الإسكات)", signal.get("symbol", "?"))
            return
        self._safe_send(text)

    # ─────────────────────────────── تجميع الرسائل (2026-10-30)
    # المنطق: كل إشارة تُسجَّل فورًا (حدث + دفتر + تحديث الحالة) كما كان،
    # لكن نص الرسالة يُوضع في صندوق الدورة، ويُرسَل مرة واحدة عند نهاية الدورة.
    # التوقيت الفعلي محفوظ: الرسالة تُرسل في نفس الدورة التي حدث فيها الحدث
    # (كل 5 دقائق كحد أقصى) ولا تنتظر إغلاق أي شمعة.
    def _outbox(self) -> dict[str, list]:
        box = getattr(self, "_outbox_buffer", None)
        if box is None:
            box = {"entry": [], "win": [], "loss": []}
            self._outbox_buffer = box
        return box

    def _queue(self, kind: str, payload) -> None:
        self._outbox()[kind].append(payload)

    def _halal_tag(self, signal: dict) -> str:
        """رمز الحكم الشرعي المختصر (إيموجي فقط) لسطر الرسالة المجمّعة."""
        verdict = signal.get("halal_verdict") or ""
        return verdict.split(" ")[0] if verdict else ""

    def _chunks(self, items: list) -> list[list]:
        """تقسيم القائمة إلى رسائل بحجم آمن (عدد عناصر + عدد أحرف)."""
        chunks: list[list] = []
        current: list = []
        chars = 0
        for item in items:
            length = len(getattr(item, "__str__", lambda: "")()) or 0
            if current and (len(current) >= MSG_BATCH_MAX_ITEMS or chars + length > MSG_BATCH_MAX_CHARS):
                chunks.append(current)
                current, chars = [], 0
            current.append(item)
        if current:
            chunks.append(current)
        return chunks

    def _entry_line(self, signal: dict) -> str:
        entry = float(signal["entry_price"])
        stop = float(signal["stop_price"])
        stop_pct = (stop / entry - 1.0) * 100.0 if entry else 0.0
        tag = self._halal_tag(signal)
        return (
            f"• {signal['symbol']} — دخول {self._fmt(signal, entry)} | "
            f"هدف {self._fmt(signal, signal['target_price'])} (+{self.settings.target_pct:g}%) | "
            f"وقف {self._fmt(signal, stop)} ({pct_label(stop_pct)})"
            + (f" {tag}" if tag else "")
        )

    def _exit_line(self, signal: dict, event: dict) -> str:
        entry = float(signal["entry_price"])
        fill = float(event["fill_price"])
        gross = (fill / entry - 1.0) * 100.0 if entry else 0.0
        duration = humanize_duration_ar(int(signal["entry_time"]), int(event["event_time"]))
        tag = self._halal_tag(signal)
        return (
            f"• {signal['symbol']} — {pct_label(gross)} | {duration}"
            + (f" {tag}" if tag else "")
        )

    def _entry_text(self, signal: dict) -> str:
        verdict = signal.get("halal_verdict") or self.get_halal_verdict(signal["symbol"])
        pivot_line = ""
        if signal.get("pivot_label"):
            pivot_line = f"🧩 القاع: {signal['pivot_label']} محوري (ZigZag++ [LD])\n"
        ema_line = ""
        ema_value = signal.get("ema_value")
        if ema_value and float(ema_value) > 0:
            ema_line = f"📈 فوق EMA{self.settings.ema_len}: {self._fmt(signal, float(ema_value))}\n"
        return (
            f"📥 إشارة دخول — {signal['symbol']}\n"
            f"{STRATEGY_LABEL} • فريم 1H\n"
            "\n"
            f"الدخول: {self._fmt(signal, signal['entry_price'])}\n"
            f"الهدف: {self._fmt(signal, signal['target_price'])} (+{self.settings.target_pct:g}%)\n"
            f"الوقف: {self._fmt(signal, signal['stop_price'])} — إغلاق تحت قاع الشمعة\n"
            f"شمعة القاع: {hhmm_local(int(signal['bottom_bar_open_time']), self.config.timezone_name)}"
            f" | شمعة الدخول: {hhmm_local(int(signal['entry_bar_open_time']), self.config.timezone_name)}\n"
            f"{pivot_line}"
            f"{ema_line}"
            "\n"
            f"⏱ {stamp_ms(int(signal['entry_time']), self.config.timezone_name)}\n"
            f"⚖️ الحكم الشرعي: {verdict}"
        )

    def _win_text(self, signal: dict, event: dict) -> str:
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
        return (
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

    def _loss_text(self, signal: dict, event: dict) -> str:
        verdict = signal.get("halal_verdict") or self.get_halal_verdict(signal["symbol"])
        entry = float(signal["entry_price"])
        fill = float(event["fill_price"])
        gross = (fill / entry - 1.0) * 100.0
        duration = humanize_duration_ar(int(signal["entry_time"]), int(event["event_time"]))
        return (
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

    def _batch_text(self, kind: str, items: list, part: int, parts: int, total: int) -> str:
        """نص رسالة مجمّعة واحدة تضم عدة عملات (بدل رسالة لكل عملة)."""
        if kind == "entry":
            head = f"📥 إشارات دخول ({total}) — {STRATEGY_LABEL}"
            stamp = stamp_ms(int(items[-1]["entry_time"]), self.config.timezone_name)
            lines = [self._entry_line(item) for item in items]
            footer = "الوقف: إغلاق شمعة تحت قاع الإشارة • الهدف يُحتسب باللمس"
        elif kind == "win":
            head = f"🎯 أهداف تحققت ({total}) — {STRATEGY_LABEL}"
            stamp = stamp_ms(int(items[-1]["event"]["event_time"]), self.config.timezone_name)
            lines = [self._exit_line(item["signal"], item["event"]) for item in items]
            footer = "الهدف +" + f"{self.settings.target_pct:g}% تم لمسه — الربح قبل العمولة"
        else:
            head = f"🛑 إغلاقات خاسرة ({total}) — {STRATEGY_LABEL}"
            stamp = stamp_ms(int(items[-1]["event"]["event_time"]), self.config.timezone_name)
            lines = [self._exit_line(item["signal"], item["event"]) for item in items]
            footer = "سبب الخروج: إغلاق شمعة ساعة تحت قاع الإشارة"
        part_note = f" • رسالة {part}/{parts}" if parts > 1 else ""
        return (
            f"{head}{part_note}\n"
            f"⏱ {stamp}\n"
            "\n"
            + "\n".join(lines)
            + "\n\n"
            + footer
        )

    def flush_messages(self) -> None:
        """يُرسل ما تجمّع في صندوق الدورة: رسالة واحدة لكل نوع (أو تقسيم عند الحد).

        إشارة واحدة وحدها ⇒ تُرسل بتفصيلها الكامل كما قبل (لا تتغيّر تجربة القارئ)،
        وأكثر من واحدة ⇒ رسالة مجمّعة واحدة تحوي سطرًا لكل عملة.
        """
        box = self._outbox()
        for kind in ("entry", "win", "loss"):
            items = box.get(kind) or []
            if not items:
                continue
            box[kind] = []
            if len(items) == 1:
                try:
                    if kind == "entry":
                        self._send_trade_message(items[0], self._entry_text(items[0]))
                    elif kind == "win":
                        self._send_trade_message(items[0]["signal"], self._win_text(items[0]["signal"], items[0]["event"]))
                    else:
                        self._send_trade_message(items[0]["signal"], self._loss_text(items[0]["signal"], items[0]["event"]))
                except Exception:
                    self.logger.error("ZigZag: فشل تجهيز رسالة %s", kind, exc_info=True)
                continue

            chunks = self._chunks(items)
            for index, chunk in enumerate(chunks, start=1):
                try:
                    text = self._batch_text(kind, chunk, index, len(chunks), len(items))
                except Exception:
                    self.logger.error("ZigZag: فشل تجهيز رسالة %s مجمّعة", kind, exc_info=True)
                    continue
                self._safe_send(text)
            self.logger.info("ZigZag: رسائل %s مجمّعة: %s رسالة تحوي %s عنصرًا", kind, len(chunks), len(items))

    def send_entry_message(self, signal: dict) -> None:
        """يسجّل الدخول (حدث + دفتر) ويضع رسالته في صندوق الدورة."""
        if not signal.get("halal_verdict"):
            signal["halal_verdict"] = self.get_halal_verdict(signal["symbol"])
        self.append_event("entry", signal["symbol"], int(signal["entry_time"]))
        append_zigzag_entry(self.data_dir, self._build_trade_log_record(signal))
        self._queue("entry", signal)

    def send_success_message(self, signal: dict, event: dict) -> None:
        """يسجّل النجاح (حدث + دفتر + إغلاق) ويضع رسالته في صندوق الدورة."""
        if not signal.get("halal_verdict"):
            signal["halal_verdict"] = self.get_halal_verdict(signal["symbol"])
        self.append_event("exit_win", signal["symbol"], int(event["event_time"]))
        self._record_trade_exit(signal, event)
        self.close_signal(signal["symbol"])
        self._queue("win", {"signal": dict(signal), "event": dict(event)})

    def send_loss_message(self, signal: dict, event: dict) -> None:
        """يسجّل الخسارة (حدث + دفتر + إغلاق) ويضع رسالتها في صندوق الدورة."""
        if not signal.get("halal_verdict"):
            signal["halal_verdict"] = self.get_halal_verdict(signal["symbol"])
        self.append_event("exit_loss", signal["symbol"], int(event["event_time"]))
        self._record_trade_exit(signal, event)
        self.close_signal(signal["symbol"])
        self._queue("loss", {"signal": dict(signal), "event": dict(event)})

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
        # إرسال رسائل الإغلاقات مجمّعة (رسالة واحدة تحوي كل إغلاقات هذه الدورة)
        self.flush_messages()

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

            # Deviation في MT4 بوحدات النقطة = tickSize للزوج
            point = 0.01
            try:
                point = float(self.price_ticks.get(symbol) or 0.01)
            except (TypeError, ValueError):
                point = 0.01
            bottom = find_latest_bottom(closed_df, self.settings, point=point)
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

            if self.settings.pivot_mode and bottom.get("entry_open_time") is not None:
                # قاعدة المستخدم: شمعة القرار = الشمعة التالية للقاع، ولا تُقيَّم إلا إذا
                # كانت آخر شمعة مغلقة (وإلا فالإشارة فاتت — لا ندخل بسعر قديم)
                if int(closed_df["open_time"].iloc[-1]) != int(bottom["entry_open_time"]):
                    continue
                next_row = closed_df.iloc[-1]
            elif self.settings.pivot_mode and self.settings.pivot_source == "confirmed":
                # المحور المؤكد: الدخول بعد تحقق الـ Backstep
                next_row = self._pivot_entry_candle(closed_df, bottom)
            else:
                # قاعدة المستخدم: الدخول على **الشمعة التالية للقاع** إن أغلقت خضراء،
                # وإلا تُلغى الإشارة (next_closed_bar = الشمعة التالية مباشرة)
                next_row = next_closed_bar(closed_df, bottom["bar_open_time"])
            if next_row is None:
                # لم تتأكد الإشارة/لم تُغلق شمعة الدخول بعد — ننتظر الدورة القادمة
                continue

            # منع الإشارات المتأخرة (توقف البوت أو بيانات قديمة)
            age_ms = server_time - int(next_row["close_time"])
            if age_ms > int(self.settings.max_entry_age_hours * HOUR_MS):
                self.state["last_bottom_bar_time"][symbol] = bottom["bar_open_time"]
                self.store.save(self.state)
                self.logger.info("ZigZag: تخطّي قاع قديم لـ %s (عمر الإشارة %s دقيقة)", symbol, age_ms // MINUTE_MS)
                continue

            # قيمة EMA عند شمعة الدخول نفسها (بلا نظر للمستقبل) — لفلتر المتوسط 200
            ema_value = None
            if self.settings.ema_enabled:
                if self.settings.ema_interval == "1h":
                    ema_value = ema_at_bar(closed_df, self.settings.ema_len, int(next_row["open_time"]))
                else:
                    ema_value = self._ema_higher_tf(symbol, int(next_row["close_time"]))

            rejected: dict = {}
            candidate = evaluate_entry_candle(
                bottom, next_row, self.settings, ema_value=ema_value, rejected=rejected
            )
            self.state["last_bottom_bar_time"][symbol] = bottom["bar_open_time"]

            if candidate is None:
                # حمراء أو دوجي أو إغلاق تحت القاع أو تحت EMA200 ⇒ يُلغى القاع
                reason = rejected.get("reason")
                if reason:
                    self._count_rejection(reason, server_time)
                self.store.save(self.state)
                if reason == "below_ema":
                    self.logger.info(
                        "ZigZag: %s تحت EMA%s — إشارة مرفوضة", symbol, self.settings.ema_len
                    )
                else:
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
                "ema_value": candidate.get("ema_value"),
                "pivot_label": bottom.get("pivot_label"),
                "silent": is_muted(),
            }
            open_signals[symbol] = signal
            self.store.save(self.state)
            self.send_entry_message(signal)
            self.logger.info(
                "ZigZag: New signal for %s @ %s (target %s, stop %s)",
                symbol, candidate["entry_price"], candidate["target_price"], candidate["stop_price"],
            )
        # إرسال رسائل الدخول مجمّعة (رسالة واحدة تحوي كل إشارات هذه الدورة)
        self.flush_messages()

    def _ema_higher_tf(self, symbol: str, as_of_close_ms: int) -> float | None:
        """EMA على فريم أعلى (4h) — يُجلب فقط عند وجود إشارة مرشّحة (تكلفة مهملة)."""
        try:
            candles = self.binance.get_klines(
                symbol=symbol, interval=self.settings.ema_interval, limit=500
            )
        except Exception:
            self.logger.warning("ZigZag: تعذّر جلب شموع %s لـ %s", self.settings.ema_interval, symbol)
            return None
        return ema_at_bar_from_candles(candles, self.settings.ema_len, as_of_close_ms)

    def _pivot_entry_candle(self, closed_df, bottom: dict):
        """وضع المحاور: آخر شمعة مغلقة، بشرط أن تكون بعد تأكيد المحور (بلا نظر للمستقبل)."""
        if closed_df is None or closed_df.empty:
            return None
        last = closed_df.iloc[-1]
        if int(last["open_time"]) <= int(bottom.get("confirm_open_time", 0)):
            return None
        return last

    def _count_rejection(self, reason: str, now_ms: int) -> None:
        """عدّاد أسباب رفض الإشارات (يظهر في التقرير اليومي) — لا يغيّر أي منطق."""
        day = local_date_key_from_ms(int(now_ms), self.config.timezone_name)
        stats = self.state.setdefault("rejection_stats", {})
        if stats.get("date") != day:
            stats.clear()
            stats["date"] = day
            stats["counts"] = {}
        counts = stats.setdefault("counts", {})
        counts[reason] = int(counts.get(reason, 0)) + 1

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

    # ─────────────────────────────────────────── التقرير اليومي
    def send_daily_report_if_due(self, now_ms: int) -> None:
        """تقرير يومي عند 00:00 بتوقيت الرياض يغطي اليوم المنتهي.

        الأرقام من حدثيات الحالة (دخول/نجاح/خسارة) ومن دفتر ZigZag
        (`net_return_pct`) — لا يقرأ أي دفتر آخر.
        """
        now_local = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(
            ZoneInfo(self.config.timezone_name)
        )
        if now_local.hour != 0:
            return

        report_day = (now_local.date() - timedelta(days=1)).isoformat()
        report_state = self.state.setdefault("daily_report", {})
        if report_state.get("last_reported_for_date") == report_day:
            return

        entries = wins = losses = 0
        for event in self.state.get("event_log", []):
            event_ms = int(event.get("time_ms", 0))
            if local_date_key_from_ms(event_ms, self.config.timezone_name) != report_day:
                continue
            event_type = event.get("type")
            if event_type == "entry":
                entries += 1
            elif event_type == "exit_win":
                wins += 1
            elif event_type == "exit_loss":
                losses += 1

        closed_rows = []
        for row in load_zigzag_rows(self.data_dir):
            try:
                exit_ms = int(row.get("exit_time_ms") or 0)
            except (TypeError, ValueError):
                continue
            if exit_ms and local_date_key_from_ms(exit_ms, self.config.timezone_name) == report_day:
                closed_rows.append(row)

        nets: list[tuple[str, float]] = []
        for row in closed_rows:
            try:
                nets.append((str(row.get("symbol", "")), float(row.get("net_return_pct"))))
            except (TypeError, ValueError):
                continue

        closed_count = wins + losses
        success_rate = (wins / closed_count * 100.0) if closed_count else 0.0
        avg_net = (sum(net for _sym, net in nets) / len(nets)) if nets else None
        best = max(nets, key=lambda item: item[1]) if nets else None
        worst = min(nets, key=lambda item: item[1]) if nets else None
        weekday_name = weekday_ar_from_ms(now_ms - 1000, self.config.timezone_name)
        open_count = len(self.state.get("open_signals", {}))

        best_line = (
            f"🏆 أفضل عملة: {best[0]} {pct_label(best[1])}" if best
            else "🏆 أفضل عملة: لا توجد صفقات مغلقة اليوم"
        )
        worst_line = (
            f"⚠️ أسوأ عملة: {worst[0]} {pct_label(worst[1])}" if worst
            else "⚠️ أسوأ عملة: لا توجد صفقات مغلقة اليوم"
        )
        avg_net_text = pct_label(avg_net) if avg_net is not None else "—"

        # سطر فلتر EMA200: كم إشارة رُفضت اليوم (شفافية أثر الفلتر)
        stats = self.state.get("rejection_stats") or {}
        filter_line = ""
        if self.settings.ema_enabled and stats.get("date") == report_day:
            counts = stats.get("counts") or {}
            rejected_count = int(counts.get("below_ema", 0)) + int(counts.get("ema_unknown", 0))
            if rejected_count:
                filter_line = f"🚫 مرفوضة بفلتر EMA{self.settings.ema_len}: {rejected_count}\n"
        text = (
            f"📊 التقرير اليومي — {STRATEGY_LABEL}\n"
            f"🗓️ اليوم المشمول: {weekday_name} {report_day}\n"
            f"🕛 وقت التقرير: {stamp_ms(now_ms, self.config.timezone_name)}\n"
            "\n"
            f"📥 إشارات دخلت: {entries}\n"
            f"✅ نجاح: {wins} • 🛑 خسارة: {losses}\n"
            f"📈 نسبة النجاح: {success_rate:.1f}%\n"
            f"💰 متوسط النتيجة (بعد العمولة): {avg_net_text}\n"
            f"📌 مفتوحة حاليًا: {open_count}\n"
            f"{filter_line}"
            f"{best_line}\n"
            f"{worst_line}"
        )
        self._safe_send(text)
        report_state["last_reported_for_date"] = report_day
        self.store.save(self.state)
        self.logger.info("ZigZag: Daily report sent for %s", report_day)

    # ─────────────────────────────────────────── التشغيل
    def run_cycle(self) -> None:
        self.refresh_symbols(force=not self.symbols)
        self.refresh_halal_verdicts()
        self.purge_pegged_stable_signals()
        self.sync_open_signals_to_journal()
        try:
            server_time = self.binance.get_server_time()
            klines_map = self.binance.get_klines_for_symbols(
                self.symbols, self.interval, self.config.kline_limit
            )
            self.monitor_open_signals(server_time, klines_map)
            self.process_new_bottoms(klines_map, server_time)
            self.send_daily_report_if_due(server_time)
        finally:
            # ضمان عدم بقاء أي رسالة في الصندوق حتى لو فشل جزء من الدورة
            self.flush_messages()

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
