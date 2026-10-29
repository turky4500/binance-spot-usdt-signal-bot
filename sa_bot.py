"""بوت نظام القاع — نسخة السعودية (تداول): نظام موازٍ مستقل تمامًا.

لا يقرأ ولا يكتب أي ملف يخص نظام آخر:
    الحالة:  data/state_sa.json     (النظام الآخر: data/state_qaa.json / data/state.json)
    الدفتر:  data/trades_log_sa.csv (النظام الآخر: trades_log_qaa.csv / trades_log.csv)
    القائمة: config/sa_universe.txt (رموز تداول) + config/sa_halal.json (حكم يدوي اختياري)

الرسائل (كلها باسم المؤشر «نظام القاع (سهم يومي)» + سطر الحكم الشرعي):
    📥 دخول — سعر/وقف القاع/هدف عرض 2R/حجم مقترح
    📈 تحديث وقف متحرك — عند ارتفاع الوقف ≥ عتبة (1%) ليعدّل المستخدم يدويًا
    🎉/🛑 إغلاق — نسب فعلية وصافية، سبب الإغلاق (وقف/مهلة)، وأعلى سعر تحقق
    ⚠️ رسائل فردية فقط لكل سهم — لا قائمة جامعة أبدًا (حد تيليجرام 4096).

تكييفات جوهرية عن نسخة الكريبتو:
    • الفريم يومي حصراً — لكن إغلاق الشمعة = 15:00 الرياض (12:00 UTC)، وأيام
      التداول غير متصلة (عطلة جمعة/سبت + عطلات رسمية) → المهلة والمهلة تُحسب
      بعدّ الشموع الفعلية (فهرس) لا بفروق الزمن، وجدولة المعالجة بالتاريخ.
    • البيانات من Yahoo Finance بأسعار معدّلة بالكامل (توزيعات/انقسامات).
    • لا توجد بيانات لحظية مجانية → متابعة شموع 1 دقيقة معطّلة (المسار محفوظ
      للاستخدام لاحقًا عند توفر مصدر لحظي)، وإدارة الوقف على إغلاق الشمعة اليومية.
    • لا حد لعدد المراكز المفتوحة: أي سهم تنطبق شرطه يُرسل فورًا.
    • فشل رسالة واحدة لا يوقف الدورة أبدًا (safe_send) — درس حادثة 2026-10-20.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from src.config import AppConfig
from src.saudi_data import (
    DAY_MS,
    TadawulClient,
    date_close_ms,
    format_sar_price,
    is_bar_closed,
    now_ms,
    riyadh_today_str,
)
from src.saudi_halal import ensure_verdict, refresh_if_stale
from src.saudi_journal import append_sa_entry, update_sa_exit
from src.saudi_strategy import (
    SaSettings,
    bars_between,
    evaluate_entry,
    is_timeout,
    next_stop,
    should_raise_alert,
    stop_fill,
)
from src.state import StateStore
from src.telegram_client import TelegramClient, is_muted
from src.utils import (
    humanize_duration_ar,
    local_date_key_from_ms,
    ms_to_local_text,
)

EVENT_RETENTION_DAYS = 120
STRATEGY_LABEL = "نظام القاع (سهم يومي)"

# ───────────────────────────────────────────── مسارات النظام المنفصلة
SA_STATE_FILE = os.getenv("SA_STATE_FILE", "data/state_sa.json")
SA_HALAL_DIR = os.getenv("SA_HALAL_DIR", "data/sa")
SA_UNIVERSE_FILE = os.getenv("SA_UNIVERSE_FILE", "config/sa_universe.txt")

# نافذة البيانات لكل رمز (شموع يومية) — ~2y يكفي تمهيد EMA200 مع هوامش
SA_DATA_RANGE = os.getenv("SA_DATA_RANGE", "2y")
# حد مطاردة الأيام الفائتة عند تعطّل الدورات (لا يتجاوزه كي لا تُعاد عشرات الأيام دفعة واحدة)
SA_MAX_CATCHUP_DAYS = int(os.getenv("SA_MAX_CATCHUP_DAYS", "10"))
# رمز المسبار الرخيص: يُجلب فقط لحسم «هل أغلقت شمعة جديدة؟» قبل جلب 383 رمزًا
SA_PROBE_CODE = os.getenv("SA_PROBE_CODE", "2222")
# متابعة شموع 1 دقيقة (معطّل افتراضيًا — لا بيانات لحظية مجانية لتداول)
SA_INTRABAR_ENABLED = os.getenv("SA_INTRABAR_ENABLED", "0").strip().lower() not in ("0", "false", "no", "off")


def load_universe(path: str | None = None) -> tuple[list[str], dict[str, str]]:
    """يقرأ قائمة رموز تداول: (أكواد مرتّبة، {رمز: اسم الشركة}).

    ملف سطر واحد: «الرمز  الاسم» — الاسم اختياري للقراءة فقط، تُقرأ الأعمدة الأولى.
    """
    file_path = Path(path or SA_UNIVERSE_FILE)
    codes: list[str] = []
    names: dict[str, str] = {}
    if file_path.exists():
        for raw in file_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            code = parts[0].upper().removesuffix(".SR")
            if not code or not code.isdigit():
                continue
            if code not in names:
                codes.append(code)
            if len(parts) > 1 and parts[1].strip():
                names[code] = parts[1].strip()
    return sorted(set(codes)), names


class SaSignalBot:
    """بوت إشارات نظام القاع — مسح رموز تداول من قائمة مُعرَّفة مسبقًا."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = logging.getLogger("sa-signal-bot")
        self.market = TadawulClient(
            timeout=config.request_timeout,
            max_workers=config.max_workers,
        )
        self.telegram = TelegramClient(
            token=config.telegram_bot_token,
            chat_id=config.telegram_chat_id,
            timeout=config.request_timeout,
        )
        self.settings = SaSettings()
        self.store = StateStore(SA_STATE_FILE)
        self.state = self.store.load()
        self.data_dir = str(Path(SA_STATE_FILE).parent)
        self.halal_dir = SA_HALAL_DIR
        self.halal_verdicts: dict[str, str] = {}
        self.codes: list[str] = []
        self.company_names: dict[str, str] = {}
        self.last_universe_refresh = 0.0

    # ─────────────────────────────────────────── البيانات الأساسية
    def refresh_universe(self, force: bool = False) -> None:
        now = time.time()
        if not force and self.codes and (now - self.last_universe_refresh) < 6 * 60 * 60:
            return
        self.codes, self.company_names = load_universe()
        self.last_universe_refresh = now
        self.logger.info("SA: Loaded %d Tadawul symbols from %s", len(self.codes), SA_UNIVERSE_FILE)

    def refresh_halal_verdicts(self) -> None:
        self.halal_verdicts = refresh_if_stale(self.halal_dir)

    def get_halal_verdict(self, code: str) -> str:
        return ensure_verdict(self.halal_dir, code, self.halal_verdicts)

    def append_event(self, event_type: str, code: str, event_time_ms: int) -> None:
        events = self.state.setdefault("event_log", [])
        events.append({"type": event_type, "symbol": code, "time_ms": int(event_time_ms)})
        cutoff_ms = int(event_time_ms) - (EVENT_RETENTION_DAYS * 24 * 60 * 60 * 1000)
        self.state["event_log"] = [e for e in events if int(e.get("time_ms", 0)) >= cutoff_ms]
        self.store.save(self.state)

    # ─────────────────────────────────────────── دفتر الصفقات (الملف المنفصل)
    def _build_trade_log_record(self, trade: dict) -> dict[str, object]:
        entry_time_ms = int(trade["entry_time"])
        return {
            "trade_id": trade["trade_id"],
            "code": trade["code"],
            "company": trade.get("company", ""),
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
        update_sa_exit(
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
        for _code, trade in self.state.get("open_trades", {}).items():
            append_sa_entry(self.data_dir, self._build_trade_log_record(trade))

    # ─────────────────────────────────────────── رسائل تيليجرام (فردية — بلا قائمة جامعة)
    def _fmt_price(self, value: float | str) -> str:
        return format_sar_price(value)

    def _label_with_company(self, trade: dict) -> str:
        code = str(trade.get("code", ""))
        company = str(trade.get("company", "") or "")
        return f"{code} ({company})" if company else code

    def _safe_send(self, text: str) -> None:
        """إرسال آمن: فشل رسالة واحدة (تجاوز 4096، شبكة...) لا يوقف دورة المراقبة.

        الدرس: يوم 2026-10-20 — تقرير بـ15 صفقة تجاوز حد تيليجرام (HTTP400)
        قتل الدورة قبل حفظ الحالة → أربع دورات متعاقبة فشلت ودفعت النظام18 ساعة.
        """
        try:
            self.telegram.send_message(text)
        except Exception:
            self.logger.error("SA: فشل إرسال رسالة تيليجرام — تُجتاز وتُسجَّل وتُكمل الدورة", exc_info=True)

    def _send_trade_message(self, trade: dict, text: str) -> None:
        if trade.get("silent"):
            self.logger.info(
                "SA: صفقة صامتة %s — رسالة لم تُرسل (فُتحت أثناء الإسكات)",
                trade.get("code", "?"),
            )
            return
        self._safe_send(text)

    def send_entry_message(self, trade: dict) -> None:
        verdict = trade.get("halal_verdict") or self.get_halal_verdict(trade["code"])
        entry = float(trade["entry_price"])
        stop = float(trade["pivot_stop"])
        target = float(trade["target_price"])
        dist = float(trade["stop_dist_pct"])
        size = float(trade["size_suggest_pct"])
        text = (
            f"📥 إشارة دخول — {STRATEGY_LABEL}\n"
            f"السهم: {self._label_with_company(trade)}\n"
            f"الفريم: 1D (يومي)\n"
            f"سعر الدخول: {self._fmt_price(entry)} ريال\n"
            f"وقف القاع (الأصلي): {self._fmt_price(stop)} ريال (-{dist:.2f}%)\n"
            f"🎯 الهدف المقترح: {self._fmt_price(target)} ريال "
            f"(+{self.settings.target_r * dist:.1f}% — عرض فقط لا يُنفَّذ)\n"
            f"📏 مسافة الوقف: {dist:.2f}% → 💼 الحجم المقترح: {size:.1f}% من رأس المال "
            f"(مخاطرة {self.settings.risk_pct:.1f}%)\n"
            f"🟢 الوقف المتحرك: {self.settings.trail_pct:g}% من القمة — يرتفع فقط "
            f"(أول رسالة تحديث عند +{self.settings.stop_alert_step_pct:g}%)\n"
            f"وقت الإشارة: {ms_to_local_text(int(trade['entry_time']), self.config.timezone_name)}\n"
            f"─────────────\n"
            f"الحكم الشرعي: {verdict}"
        )
        self._send_trade_message(trade, text)
        self.append_event("entry", trade["code"], int(trade["entry_time"]))
        append_sa_entry(self.data_dir, self._build_trade_log_record(trade))

    def send_stop_update_message(
        self, trade: dict, old_stop: float, new_stop: float, event_ms: int, current_price: float | None = None
    ) -> None:
        """رسالة ارتفاع الوقف المتحرك — ليعدّل المستخدم وقفه يدويًا على منصته.

        تحوي: سعر الدخول، الوقف السابق والجديد ونسبة الارتفاع، الربح المضمون،
        عدد التحديثات، والسعر الحالي مع نسبته من الدخول ومسافته فوق/دون الوقف الجديد —
        ليقرر المستخدم بنفسه (مسارته/تعديل وقفه).
        """
        verdict = trade.get("halal_verdict") or self.get_halal_verdict(trade["code"])
        entry = float(trade["entry_price"])
        rise_pct = (float(new_stop) / float(old_stop) - 1.0) * 100.0
        lock_pct = (float(new_stop) / entry - 1.0) * 100.0
        lock_line = (
            f"🔒 الربح المضمون عند الخروج عند هذا الوقف: {lock_pct:+.2f}%"
            if lock_pct >= 0
            else f"⚠️ الوقف ما زال دون سعر الدخول ({lock_pct:+.2f}%)"
        )
        price_line = ""
        if current_price:
            now_pct = (float(current_price) / entry - 1.0) * 100.0
            gap = (float(current_price) / float(new_stop) - 1.0) * 100.0
            gap_text = f"{gap:.2f}% فوق الوقف الجديد" if gap >= 0 else f"{-gap:.2f}% دون الوقف الجديد"
            price_line = (
                f"السعر الآن: {self._fmt_price(current_price)} ريال "
                f"({now_pct:+.2f}% من الدخول • {gap_text})\n"
            )
        text = (
            f"📈 تحديث وقف متحرك — {STRATEGY_LABEL}\n"
            f"السهم: {self._label_with_company(trade)}\n"
            f"سعر الدخول: {self._fmt_price(entry)} ريال\n"
            f"{price_line}"
            f"الوقف السابق: {self._fmt_price(old_stop)} ريال\n"
            f"الوقف الجديد: {self._fmt_price(new_stop)} ريال (+{rise_pct:.2f}%)\n"
            f"{lock_line}\n"
            f"عدد تحديثات الوقف: {int(trade.get('stop_updates_count', 0)) + 1}\n"
            f"وقت التحديث: {ms_to_local_text(int(event_ms), self.config.timezone_name)}\n"
            f"─────────────\n"
            f"ℹ️ عدّل وقفك يدويًا على منصتك عند هذا المستوى\n"
            f"─────────────\n"
            f"الحكم الشرعي: {verdict}"
        )
        self._send_trade_message(trade, text)
        self.append_event("stop_alert", trade["code"], int(event_ms))

    def send_close_message(
        self, trade: dict, exit_price: float, event_time_ms: int, reason: str, reason_ar: str
    ) -> None:
        """رسالة انتهاء الصفقة — تفصيلية: نسب فعلية وصافية، سبب الإغلاق، وأعلى سعر."""
        verdict = trade.get("halal_verdict") or self.get_halal_verdict(trade["code"])
        entry = float(trade["entry_price"])
        gross = (float(exit_price) / entry - 1.0) * 100.0
        net = gross - (2.0 * self.settings.commission_side_pct)
        if net > 0:
            outcome, emoji, headline = "win", "🎉", "انتهت الصفقة — رابحة ✅"
            result_line = f"✅ تم نجاح التوصية بنسبة {net:+.2f}%"
        else:
            outcome, emoji, headline = "loss", "🛑", "انتهت الصفقة — خاسرة"
            result_line = f"🛑 الخسارة بنسبة {net:+.2f}%"
        peak = float(trade.get("peak_price", entry))
        peak_gain = (peak / entry - 1.0) * 100.0
        duration = humanize_duration_ar(trade["entry_time"], event_time_ms)
        last_stop = float(trade.get("stop_price", trade.get("pivot_stop", entry)))
        text = (
            f"{emoji} {headline}\n"
            f"{STRATEGY_LABEL}\n"
            f"السهم: {self._label_with_company(trade)}\n"
            f"سعر الدخول: {self._fmt_price(entry)} ريال\n"
            f"سعر الخروج: {self._fmt_price(exit_price)} ريال\n"
            f"النسبة الفعلية: {gross:+.2f}% • الصافية بعد العمولة: {net:+.2f}%\n"
            f"سبب الإغلاق: {reason_ar}\n"
            f"أعلى سعر تحقق: {self._fmt_price(peak)} ريال (+{peak_gain:.2f}%)\n"
            f"الوقف الأخير: {self._fmt_price(last_stop)} ريال\n"
            f"تحديثات الوقف: {int(trade.get('stop_updates_count', 0))} رسالة\n"
            f"وقت الإغلاق: {ms_to_local_text(int(event_time_ms), self.config.timezone_name)}\n"
            f"المدة المستغرقة: {duration}\n"
            f"─────────────\n"
            f"{result_line}\n"
            f"─────────────\n"
            f"الحكم الشرعي: {verdict}"
        )
        self._send_trade_message(trade, text)
        self.append_event("exit_win" if outcome == "win" else "exit_loss", trade["code"], int(event_time_ms))
        self._record_trade_exit(trade, outcome, reason, int(event_time_ms), float(exit_price))

    def close_trade(self, code: str, exit_bar_open_time: int, reason: str) -> None:
        self.state["open_trades"].pop(code, None)
        self.state.setdefault("last_exit_bar_time", {})[code] = exit_bar_open_time
        self.store.save(self.state)
        self.logger.info("SA: Closed %s بسبب %s", code, reason)

    def _stop_reason(self, trade: dict, stop_value: float) -> tuple[str, str]:
        """سبب الإغلاق عند الوقف: متحرك (رُفع فوق القاع) أم وقف القاع نفسه."""
        pivot_stop = float(trade.get("pivot_stop", trade["stop_price"]))
        if float(stop_value) > pivot_stop:
            return "stop_trail", "لمس الوقف المتحرك 🔺"
        return "stop_pivot", "لمس وقف القاع"

    # ─────────────────────────────────────────── متابعة لحظية (معطّلة — لا بيانات لحظية)
    def monitor_open_trades_intrabar(self, now_ms_value: int) -> None:
        """متابعة شموع 1 دقيقة — **معطّلة** في نسخة تداول.

        لا يوجد مصدر بيانات لحظية مجانية شبيه بـBinance لتداول، لذا تُدار الوقفات
        على إغلاق الشمعة اليومية (يكفي — طلب المستخدم). المسار محفوظ حرفًا من نسخة
        الكريبتو ليُفعَّل لاحقًا إن توفّر مصدر لحظي (SA_INTRABAR_ENABLED=1 + عميل يدعم 1m).
        """
        if not SA_INTRABAR_ENABLED:
            return
        self.logger.warning(
            "SA: SA_INTRABAR_ENABLED=1 لكن لا يوجد عميل لحظي لتداول — المسار معطّل فعليًا."
        )

    # ─────────────────────────────────────────── معالجة يوم تداول مغلق (محور بالتاريخ)
    def _latest_closed_date_fast(self) -> str:
        """يحدّد آخر تاريخ تداول مغلق بجلب رمز مسبار واحد فقط (طلب رخيص قبل 383)."""
        probe = self.market.get_daily(SA_PROBE_CODE, range="10d")
        if probe.empty or "date" not in probe.columns:
            return ""
        now = now_ms()
        closed = [d for d in sorted(set(probe["date"].tolist())) if is_bar_closed(d, now)]
        return closed[-1] if closed else ""

    def process_closed_days(self, klines_map: dict, now: int) -> None:
        """يجمع تواريخ التداول المغلقة (غير المتصلة) ويعالجها تصاعدياً حتى حدّ المطاردة."""
        self.purge_delisted_trades()

        all_dates: set[str] = set()
        for df in klines_map.values():
            if df is None or df.empty or "date" not in df.columns:
                continue
            all_dates.update(df["date"].tolist())
        closed_dates = sorted(d for d in all_dates if is_bar_closed(d, now))
        if not closed_dates:
            return
        latest_closed = closed_dates[-1]

        last_processed = str(self.state.get("last_processed_date", "") or "")
        if last_processed and last_processed >= latest_closed:
            return

        if not last_processed:
            # أول تشغيل: آخر يوم مغلق فقط (لا تاريخ خلفي بلا حالة سابقة)
            to_process = [latest_closed]
        else:
            to_process = [d for d in closed_dates if last_processed < d <= latest_closed]
        if len(to_process) > SA_MAX_CATCHUP_DAYS:
            self.logger.warning(
                "SA: مطاردة %d يوم (حدّ %d) — تُقصّر لآخر %d أيام",
                len(to_process), SA_MAX_CATCHUP_DAYS, SA_MAX_CATCHUP_DAYS,
            )
            to_process = to_process[-SA_MAX_CATCHUP_DAYS:]

        self.logger.info("SA: Processing %d closed trading day(s): %s", len(to_process), to_process[0])
        for day in to_process:
            self._process_one_trading_date(day, klines_map)
            self.state["last_processed_date"] = day
            self.store.save(self.state)

    def _process_one_trading_date(self, day: str, klines_map: dict) -> None:
        """يوم تداول واحد مغلق: متابعة المفتوحة (وقف/مهلة/رفع الوقف) ثم فتح الجديد."""
        open_trades = self.state.get("open_trades", {})
        day_close_ms = date_close_ms(day)

        # ── 1) متابعة الصفقات المفتوحة ──
        for code, trade in list(open_trades.items()):
            df = klines_map.get(code)
            if df is None or df.empty or "date" not in df.columns:
                continue
            candle = df[df["date"] == day]
            if candle.empty:
                continue  # الرمز لم يتداول هذا اليوم (إيقاف/إدراج) → لا تقييم
            row = candle.iloc[-1]
            candle_open = float(row["open"])
            candle_high = float(row["high"])
            candle_low = float(row["low"])
            candle_close = float(row["close"])
            bar_open_time = int(row["open_time"])

            # 1a) رفع الوقف أولًا من قمم الشموع السابقة (بلا انتحار نفس الشمعة)
            #     الترتيب المُختبر: تعديل ← فحص ← تحديث القمة — يُطبَّق هنا لأن هذا هو
            #     المسار الأساسي الوحيد (لا متابعة لحظية في تداول).
            cur_stop = float(trade["stop_price"])
            new_stop = next_stop(cur_stop, float(trade["peak_price"]), self.settings)
            raised = new_stop > cur_stop
            if raised:
                trade["stop_price"] = new_stop

            # 1b) فحص الوقف (بالقيمة المرفوعة) — تنفيذ الفجوة عند الفتح إن فاقته
            if candle_low <= new_stop:
                fill = stop_fill(new_stop, candle_open)
                reason_key, reason_ar = self._stop_reason(trade, new_stop)
                self.send_close_message(trade, fill, day_close_ms, reason_key, reason_ar)
                self.close_trade(code, bar_open_time, reason_key)
                continue

            # 1c) المهلة: انتهاء max_hold شمعة تداول (عدّ فهرس) = خروج بسعر الإغلاق
            bars_held = bars_between(df, int(trade["entry_bar_open_time"]), bar_open_time)
            if is_timeout(bars_held, self.settings.max_hold):
                self.send_close_message(
                    trade, candle_close, day_close_ms,
                    "timeout", f"انتهت المهلة ({self.settings.max_hold} شمعة)",
                )
                self.close_trade(code, bar_open_time, "timeout")
                continue

            # 1d) رسالة ارتفاع الوقف عند تجاوز العتبة (فقط إن لم يُغلق أعلاه)
            if raised:
                step = self.settings.stop_alert_step_pct
                if should_raise_alert(new_stop, float(trade.get("last_stop_alert_price", cur_stop)), step):
                    self.send_stop_update_message(trade, cur_stop, new_stop, day_close_ms, candle_close)
                    trade["last_stop_alert_price"] = new_stop
                    trade["stop_updates_count"] = int(trade.get("stop_updates_count", 0)) + 1

            # 1e) قمة الشمعة — الوقف يُرفع من قمة اليوم في اليوم التالي (لا حماية بنفس الشمعة)
            trade["peak_price"] = max(float(trade["peak_price"]), candle_high)

        # ── 2) إشارات دخول جديدة (لا حدّ لعدد المراكز) ──
        for code in self.codes:
            if code in self.state["open_trades"]:
                continue
            df = klines_map.get(code)
            if df is None or df.empty or "date" not in df.columns:
                continue
            closed_df = df[df["date"] <= day].copy()
            if closed_df.empty:
                continue
            if str(closed_df.iloc[-1]["date"]) != day:
                continue  # لم يتداول الرمز هذا اليوم → لا إشارة

            signal = evaluate_entry(
                closed_df,
                self.settings,
                int(self.state.get("last_entry_bar_time", {}).get(code, 0)),
                int(self.state.get("last_exit_bar_time", {}).get(code, 0)),
            )
            if not signal:
                continue

            entry_time_ms = date_close_ms(str(signal.get("bar_date") or day))
            trade = {
                "trade_id": f"{code}-{signal['bar_open_time']}",
                "code": code,
                "company": self.company_names.get(code, ""),
                "entry_price": signal["entry_price"],
                "stop_price": signal["stop_price"],          # وقف القاع — يبدأ هنا
                "pivot_stop": signal["stop_price"],           # الأصلي للعرض والتمييز
                "target_price": signal["target_price"],       # 2R — عرض فقط
                "stop_dist_pct": signal["stop_dist_pct"],
                "size_suggest_pct": signal["size_suggest_pct"],
                "entry_time": entry_time_ms,
                "entry_bar_open_time": signal["bar_open_time"],
                "peak_price": signal["peak_price"],           # قمة شمعة الإشارة
                "last_stop_alert_price": signal["stop_price"],
                "stop_updates_count": 0,
                "halal_verdict": self.get_halal_verdict(code),
                "silent": is_muted(),
            }
            self.state["open_trades"][code] = trade
            self.state.setdefault("last_entry_bar_time", {})[code] = signal["bar_open_time"]
            self.store.save(self.state)
            self.send_entry_message(trade)
            self.logger.info("SA: New trade opened for %s @ %s", code, signal["entry_price"])

    def purge_delisted_trades(self) -> None:
        """يحذف أي صفقة لسهم خرج من القائمة (إدراج ملغى/استبعاد) — لا بقاء لصفقة بلا بيانات."""
        open_trades = self.state.get("open_trades", {})
        if not self.codes:
            return
        doomed = [c for c in list(open_trades) if c not in set(self.codes)]
        for code in doomed:
            trade = open_trades.pop(code, {}) or {}
            self.state.get("last_entry_bar_time", {}).pop(code, None)
            trade_id = str(trade.get("trade_id") or "")
            if trade_id:
                update_sa_exit(self.data_dir, trade_id, {
                    "outcome": "removed",
                    "exit_reason": "removed_from_universe",
                    "exit_time_ms": int(trade.get("entry_time") or 0),
                    "exit_price": trade.get("entry_price"),
                    "duration_minutes": 0,
                    "duration_text": "أُزيلت (خارج قائمة الرموز)",
                })
            self.append_event("removed_delisted", code, int(trade.get("entry_time") or 0))

    # ─────────────────────────────────────────── التشغيل
    def run_cycle(self) -> None:
        self.refresh_universe()
        self.refresh_halal_verdicts()
        self.sync_open_trades_to_journal()
        self.monitor_open_trades_intrabar(now_ms())

        # فحص رخيص أولًا: هل أغلقت شمعة يوم جديدة فعلًا؟ إن لا → تجنّب جلب 383 رمزًا
        latest_closed = self._latest_closed_date_fast()
        last_processed = str(self.state.get("last_processed_date", "") or "")
        if latest_closed and last_processed and latest_closed <= last_processed:
            self.logger.info("SA: No new closed candle (%s <= %s) — skip full fetch", latest_closed, last_processed)
            return

        klines_map = self.market.get_daily_for_symbols(self.codes, range=SA_DATA_RANGE)
        self.process_closed_days(klines_map, now_ms())

    def run(self) -> None:
        self.refresh_universe(force=True)
        self.logger.info("SA bot started. %s", self.settings.describe())
        while True:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                raise
            except Exception:
                self.logger.exception("Unhandled error in main loop")
            time.sleep(self.config.poll_seconds)

    def run_once(self) -> None:
        self.refresh_universe(force=True)
        self.refresh_halal_verdicts()
        self.logger.info("SA bot one-shot run started. %s", self.settings.describe())
        self.run_cycle()
        self.logger.info("SA bot one-shot run completed.")


def build_sa_bot() -> SaSignalBot:
    config = AppConfig.from_env()
    Path(SA_STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    return SaSignalBot(config)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, AppConfig.from_env().log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    bot = build_sa_bot()
    bot.run()


if __name__ == "__main__":
    main()
