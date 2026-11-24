"""نظام NW 1H — نظام سادس **مستقل تمامًا** عن الأنظمة الخمسة القائمة.

المصدر: Nadaraya-Watson Smoothers [LuxAlgo] (CC BY-NC-SA 4.0) على فريم الساعة.
القواعد: ▲ + شمعة تالية خضراء ⇒ توصية دخول (شرطان فقط؛ **لون الخط مُلغي**)؛
         ▼ ⇒ إشارة خروج (مع سعر الدخول/الخروج والنتيجة ربحًا أو خسارةً)؛
         تكرار ▲ ⇒ «إشارة دخول ثانية». بلا أهداف ولا وقف.
         لا بوابة BTC؛ قبل كل توصية دخول أو تكرار يُشترط أن يصنّف ملف
         الأكاديمية الأساسي العملة مباحة. الخروج للمراكز المفتوحة لا يُحجب.

العزل:
  * الحالة: data/state_nw.json فقط · الدفتر: data/trades_log_nw.csv فقط
  * الوركفلو: .github/workflows/nw-bot.yml فقط (concurrency خاص به)
  * لا يقرأ ولا يكتب state_*.json / trades_log_*.csv لأي نظام آخر
  * لا يستورد منطق أي نظام آخر (يستخدم binance_client / telegram_client / halal للقراءة فقط)
"""

from __future__ import annotations

import csv
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from src.nw_halal_gate import AcademyPrimaryGate, Decision
from src.nw_journal import append_trade
from src.nw_messages import entry_message, exit_message, repeat_message
from src.nw_signals import NWSignalConfig, candles_closed, evaluate_symbol
from src.nw_state import NWStateStore, _cap, symbol_state
from src.nw_strategy import NW_WINDOW

_LOG = logging.getLogger("nw_bot")

# نسخة محلية من قواعد استبعاد الأسهم المرمّزة (عزل تام: لا استيراد من zigzag_bot)
_CRYPTO_B_BASES = frozenset({
    "ARB", "BB", "BBB", "BNB", "BOTB", "CKB", "DGB", "MUB", "PENGB", "QNTB",
    "SHIB", "TRB", "USDEB", "WENB", "YB",
})
_PEGGED_STABLE_BASES = frozenset({
    "USDC", "FDUSD", "TUSD", "RLUSD", "XUSD", "USD1", "USDE", "USDS", "BFUSD", "U",
    "EUR", "EURI", "BUSD", "USDP", "DAI", "PYUSD", "GUSD", "LUSD", "AEUR", "EURC",
    "USDY", "USDD", "USDF",
})


def _is_tokenized_stock(sym: str, forced_keep: set[str]) -> bool:
    if sym in forced_keep or not sym.endswith("USDT"):
        return False
    base = sym[:-4]
    return base.endswith("B") and base not in _CRYPTO_B_BASES


def _is_pegged_stable(sym: str) -> bool:
    return sym.endswith("USDT") and sym[:-4] in _PEGGED_STABLE_BASES


class NWBot:
    def __init__(
        self,
        client,
        telegram,
        state_store: NWStateStore,
        cfg: NWSignalConfig | None = None,
        *,
        state_path: str = "data/state_nw.json",
        trade_log: str = "data/trades_log_nw.csv",
        halal_primary: str = "data/daily_trend/halal_academy.json",
        halal_extra: str = "data/daily_trend/halal_extra.json",
        halal_cache: str = "data/zigzag/halal_verdicts.json",
        exclude_file: str = "config/zz_exclude.txt",
        kline_limit: int = NW_WINDOW + 1,
        commission_side_pct: float = 0.1,
        show_repaint_warning: bool = False,  # القالب المبسّط بلا سطر تحذير
        interval: str = "1h",
        include_all: bool = True,             # توسيع المتابعة، لا تعطيل بوابة الدخول
        exclude_pegged_stables: bool = True,  # ليست عملات متقلبة ⇒ إشارات ضجيج
        exclude_tokenized_stocks: bool = False,
        use_exclude_file: bool = False,       # قائمة الاستبعاد اليدوية معطّلة افتراضيًا
        entry_check: Callable[[str], Decision] | None = None,
    ) -> None:
        self.client = client
        self.tg = telegram
        self.store = state_store
        self.cfg = cfg or NWSignalConfig()
        self.state_path = state_path
        self.trade_log = trade_log
        self.halal_primary = halal_primary
        self.halal_extra = halal_extra
        self.halal_cache = halal_cache
        self.exclude_file = exclude_file
        self.kline_limit = kline_limit
        self.commission_side_pct = commission_side_pct
        self.show_repaint_warning = show_repaint_warning
        self.interval = interval
        self.include_all = include_all
        self.exclude_pegged_stables = exclude_pegged_stables
        self.exclude_tokenized_stocks = exclude_tokenized_stocks
        self.use_exclude_file = use_exclude_file
        self._entry_check = entry_check if entry_check is not None else AcademyPrimaryGate.from_environment(halal_primary)
        self.symbols: list[str] = []
        self.verdicts: dict[str, int] = {}      # أحكام العرض؛ بوابة الدخول تقرأ المصدر الأساسي مباشرة
        self.last_summary: dict[str, Any] = {}

    # ── الأحكام الشرعية: تُقرأ للعرض في الرسائل فقط، لا لتصفية الكون ─────────
    def _load_verdicts(self) -> dict[str, int]:
        from src.halal import load_primary_verdicts, merge_with_primary

        def _numeric(raw) -> dict[str, int]:
            # ملف الكاش يحمل بيانات وصفية + قاموس judgements؛ نتجاهل غير الرقمي بأمان
            payload = raw.get("judgements") if isinstance(raw, dict) and isinstance(raw.get("judgements"), dict) else raw
            out: dict[str, int] = {}
            if isinstance(payload, dict):
                for k, v in payload.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        out[str(k).upper()] = int(v)
            return out

        verdicts: dict[str, int] = {}
        for path in (self.halal_cache, self.halal_extra):
            p = Path(path)
            if not p.is_absolute():
                p = Path.cwd() / p
            if p.exists():
                try:
                    with p.open(encoding="utf-8") as f:
                        verdicts.update(_numeric(json.load(f)))
                except (OSError, ValueError, TypeError, OverflowError):
                    pass
        # أحكام العرض ليست بوابة الدخول؛ تلف الملف هنا لا يجوز أن يوقف
        # معالجة خروج المراكز. البوابة المستقلة تقرأ المصدر الأساسي بأمان.
        try:
            primary = load_primary_verdicts(str(Path.cwd() / self.halal_primary))
        except (OSError, ValueError, TypeError, AttributeError, OverflowError):
            primary = {}
        verdicts = merge_with_primary(verdicts, primary)
        if not primary:
            _LOG.warning("⚠️ ملف المصدر الأساسي للأحكام غير موجود: %s", self.halal_primary)
        self.verdicts = verdicts
        return verdicts

    # ── الكون: متابعة أزواج USDT؛ لا يغني عن بوابة إرسال الدخول ────────────
    def refresh_universe(self) -> list[str]:
        self._load_verdicts()
        excluded: set[str] = set()
        forced_keep: set[str] = set()
        if self.use_exclude_file:
            p = Path(self.exclude_file)
            if not p.is_absolute():
                p = Path.cwd() / p
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip().upper()
                    if not line or line.startswith("#"):
                        continue
                    (forced_keep if line.startswith("!") else excluded).add(line.lstrip("!"))

        # حتى إن استُخدم فلتر الكون القديم، لا نحذف مركزًا مفتوحًا من
        # المتابعة لمجرد تغيّر حكمه؛ بوابة الدخول الجديدة مستقلة عنه.
        open_symbols: set[str] = set()
        if not self.include_all:
            items = self.store.load().get("symbols", {})
            if isinstance(items, dict):
                open_symbols = {sym for sym, item in items.items()
                                if isinstance(item, dict) and item.get("position") is not None}
        symbols = []
        for sym in self.client.get_spot_usdt_symbols():
            if self.use_exclude_file and sym in excluded and sym not in forced_keep:
                continue
            if self.exclude_pegged_stables and _is_pegged_stable(sym):
                continue
            if self.exclude_tokenized_stocks and _is_tokenized_stock(sym, forced_keep):
                continue
            # فلتر الكون الاختياري لا يمنع متابعة المراكز المفتوحة؛ أما إرسال
            # الدخول فيُفحص دائمًا من ملف الأكاديمية الأساسي داخل _apply.
            if not self.include_all and sym not in open_symbols:
                base = sym[:-4] if sym.endswith("USDT") else sym
                if self.verdicts.get(base) != 0:
                    continue
            # لا توجد بوابة مرتبطة بـ BTC؛ شروط المؤشر نفسها لم تتغير.
            symbols.append(sym)
        self.symbols = sorted(symbols)
        _LOG.info(
            "كون NW: %d زوجًا (كل الأحكام؛ مباح %d | غير مباح %d | مشبوه %d | بلا حكم %d) على فريم %s",
            len(self.symbols),
            sum(1 for s in self.symbols if self.verdicts.get(s[:-4] if s.endswith("USDT") else s) == 0),
            sum(1 for s in self.symbols if self.verdicts.get(s[:-4] if s.endswith("USDT") else s) == 1),
            sum(1 for s in self.symbols if self.verdicts.get(s[:-4] if s.endswith("USDT") else s) == 2),
            sum(1 for s in self.symbols if (s[:-4] if s.endswith("USDT") else s) not in self.verdicts),
            self.interval,
        )
        return self.symbols

    # ── الرسائل: مفتاح فريد يمنع التكرار عند تكرار تشغيل الوركفلو ──────────
    def _send_once(self, state: dict, key: str, text: str) -> bool:
        if key in state["sent"]:
            return False
        self.tg.send_message(text)
        state["sent"] = _cap(state["sent"] + [key], limit=400)
        state["messages_sent"] = int(state["messages_sent"]) + 1
        return True

    # ── تطبيق شروط المستخدم على زوج واحد ────────────────────────────────────
    def _apply(self, sym: str, res: dict, state: dict, *, allow_entries: bool = True) -> dict:
        from src.halal import verdict_label

        ruling = verdict_label(self.verdicts, sym)      # الحكم الفعلي، لا «مباح» ثابت
        item = symbol_state(state, sym)
        prev_buy = set(state["prev_buy_bars"].get(sym, []))
        prev_sell = set(state["prev_sell_bars"].get(sym, []))
        hist = set(state["buy_history"].get(sym, []))
        cancelled = set(state["cancelled_buy_bars"].get(sym, []))
        events = {"entry": 0, "repeat": 0, "exit": 0, "skipped_same_label": 0,
                  "cancelled": 0, "ruling_filtered": 0, "ruling_unavailable": 0}

        # ① الإلغاء النهائي: شمعة التأكيد أُغلقت حمراء ⇒ تُسجَّل ويُنتظر ▲ جديدة.
        for rec in res.get("cancelled_buy", []):
            B = rec["bar_time"]
            if B not in cancelled:
                cancelled.add(B)
                events["cancelled"] += 1

        for rec in res["buy"]:
            if not allow_entries:
                continue                      # مركز أُضيف للخروج فقط، خارج الكون المختار
            B = rec["bar_time"]
            if B in cancelled:
                events["skipped_same_label"] += 1
                continue
            if B in prev_buy:
                events["skipped_same_label"] += 1
                continue
            pos = item["position"]
            if B in hist and pos is None:
                continue                      # عودة تسمية قديمة بعد إغلاق التوصية
            # فحص حيّ للمصدر الأساسي قبل أي تعديل للمركز أو إرسال دخول/تكرار.
            # الفشل أو الحكم غير المعروف يُحجب؛ الخروج أدناه يبقى مستقلًا.
            try:
                decision = self._entry_check(sym)
                if not isinstance(decision, Decision):
                    decision = Decision(False, "unavailable")
            except Exception:
                decision = Decision(False, "unavailable")
            if decision.allowed is not True or decision.reason != "halal":
                events["ruling_filtered"] += 1
                if decision.reason == "unavailable":
                    events["ruling_unavailable"] += 1
                # إن وُجد خروج في الدورة نفسها فلا نعرض حكمًا أقدم متعارضًا.
                ruling = {"haram": "⛔ غير مباح", "doubtful": "⚠️ مشبوه"}.get(
                    decision.reason, "ℹ️ لا يوجد حكم")
                _LOG.info("توصية NW محجوبة: %s (%s)", sym, decision.reason)
                continue
            # نعرض الحكم الذي تحقّقنا منه الآن، لا نسخة العرض القديمة.
            ruling = "✅ مباح"
            count = int(item["entry_count"]) + 1
            if pos is None:
                text = entry_message(sym, rec, ruling, self.show_repaint_warning)
                tag = "entry"
                item["position"] = {
                    "entry_price": rec["confirm_close"],
                    "entry_time": rec["confirm_bar_time"],
                    "signal_bar_time": rec["bar_time"],
                    "signal_price": rec["signal_price"],
                    "entry_count": 1,
                }
            else:
                text = repeat_message(sym, rec, count, ruling, self.show_repaint_warning)
                tag = "repeat"
                pos["entry_count"] = count
            if self._send_once(state, f"buy:{sym}:{B}:{count}", text):
                events[tag] += 1
            item["entry_count"] = count
            item["last_buy_bar"] = B
            item["last_entry_price"] = rec["confirm_close"]
            hist.add(B)

        for rec in res["sell"]:
            B = rec["bar_time"]
            if B in prev_sell:
                events["skipped_same_label"] += 1
                continue
            pos = item["position"]
            if pos is None:
                continue                      # لا توصية مفتوحة ⇒ لا خروج
            gross = (rec["confirm_close"] / float(pos["entry_price"]) - 1.0) * 100.0
            net = gross - 2.0 * self.commission_side_pct
            append_trade(self.trade_log, {
                "symbol": sym,
                "entry_time": pos["entry_time"],
                "entry_price": pos["entry_price"],
                "exit_time": rec["confirm_bar_time"],
                "exit_price": rec["confirm_close"],
                "gross_pct": round(gross, 4),
                "net_pct": round(net, 4),
                "entries_count": pos["entry_count"],
                "exit_reason": "red_label",
                "signal_bar_time": rec["bar_time"],
                "confirm_bar_time": rec["confirm_bar_time"],
            })
            if self._send_once(state, f"exit:{sym}:{B}", exit_message(
                sym, pos, rec, gross, net, ruling, self.show_repaint_warning
            )):
                events["exit"] += 1
            item["position"] = None
            item["entry_count"] = 0

        state["prev_buy_bars"][sym] = _cap(
            [r["bar_time"] for r in res["buy"]]
            + [r["bar_time"] for r in res.get("cancelled_buy", [])], 10
        )
        state["prev_sell_bars"][sym] = _cap([r["bar_time"] for r in res["sell"]], 10)
        state["buy_history"][sym] = _cap(sorted(hist), 60)
        state["cancelled_buy_bars"][sym] = _cap(sorted(cancelled), 200)
        return events

    def send_daily_report_if_due(self, now_ms: int) -> None:
        """تقرير يومي عند 12:00 AM بتوقيت الرياض عن **اليوم السابق**.

        المنطق في ``src/nw_daily_report.py``: مفتوحة + مغلقة لم يُبلَّغ عنها
        سابقًا + إجمالي الربح/الخسارة، بلا دورات وتنبيهات، وبلا تكرار للصفقات.
        """
        from src.nw_daily_report import send_daily_report_if_due as _send
        _send(self, now_ms)

    # ── دورة واحدة ─────────────────────────────────────────────────────────
    def run_once(self, now_ms: int | None = None, universe: list[str] | None = None) -> dict:
        now_ms = int(now_ms if now_ms is not None else self.client.get_server_time())
        begin_cycle = getattr(self._entry_check, "begin_cycle", None)
        if callable(begin_cycle):
            try:
                begin_cycle()
            except Exception:
                _LOG.warning("تعذرت إعادة تهيئة بوابة الأحكام؛ متابعة الخروج بأمان")
        if universe is not None:
            self._load_verdicts()          # للأحكام حتى عند تمرير كون مخصّص (الاختبارات/التجربة)
        state = self.store.load()
        symbols = universe if universe is not None else self.refresh_universe()
        entry_symbols = set(symbols)
        # أي مركز مفتوح يبقى في المتابعة حتى لو غاب عن الكون المخصّص أو
        # استُبعد بفلتر الكون. إن تعذّر جلب شموعه يُعامل كغير جاهز كالمعتاد.
        items = state.get("symbols", {})
        open_symbols = {
            sym for sym, item in items.items()
            if isinstance(sym, str) and sym.endswith("USDT") and isinstance(item, dict)
            and item.get("position") is not None
        } if isinstance(items, dict) else set()
        symbols = sorted(set(symbols) | open_symbols)
        frames = self.client.get_klines_for_symbols(symbols, self.interval, self.kline_limit)
        summary = {
            "now_ms": now_ms, "symbols": len(symbols), "evaluated": 0,
            "not_ready": 0, "ignored_retro": 0, "ignored_shallow": 0,
            "entry": 0, "repeat": 0, "exit": 0, "skipped_same_label": 0, "cancelled": 0,
            "ruling_filtered": 0, "ruling_unavailable": 0,
        }
        for sym in sorted(symbols):
            o, c, t = candles_closed(frames.get(sym), now_ms)
            res = evaluate_symbol(o, c, t, self.cfg)
            if not res["ok"]:
                summary["not_ready"] += 1
                continue
            summary["evaluated"] += 1
            summary["ignored_retro"] += res["ignored_retro"]
            summary["ignored_shallow"] += res["ignored_shallow"]
            ev = self._apply(sym, res, state, allow_entries=sym in entry_symbols)
            for key in ("entry", "repeat", "exit", "skipped_same_label", "cancelled",
                        "ruling_filtered", "ruling_unavailable"):
                summary[key] += ev[key]

        state["cycles"] = int(state["cycles"]) + 1
        state["last_cycle_ms"] = now_ms
        self.store.save(state)
        self.last_summary = summary
        _LOG.info("دورة NW: %s", summary)
        try:
            self.send_daily_report_if_due(now_ms)
        except Exception as e:
            _LOG.error("خطأ أثناء محاولة إرسال التقرير اليومي: %s", e)
        return summary


def build_nw_bot() -> NWBot:
    from src.binance_client import BinanceClient
    from src.telegram_client import TelegramClient, is_muted

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not is_muted() and (not token or not chat_id):
        raise ValueError("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (or set TELEGRAM_MUTE=1)")
    client = BinanceClient(
        timeout=int(os.getenv("REQUEST_TIMEOUT", "20")),
        max_workers=int(os.getenv("MAX_WORKERS", "10")),
        base_url=os.getenv("BINANCE_BASE_URL", "https://data-api.binance.vision"),
    )
    cfg = NWSignalConfig(
        bandwidth=float(os.getenv("NW_BANDWIDTH", "8")),
        window=int(os.getenv("NW_WINDOW", str(NW_WINDOW))),
        # مصدر الإشارة = مؤشر Nadaraya-Watson Envelope [LuxAlgo] الذي أرسله
        # المستخدم: تسميات ▲/▼ كما تُطبع على الشارت، بلا أي اشتقاق آخر.
        mode=os.getenv("NW_MODE", "envelope").strip().lower(),
        mult=float(os.getenv("NW_MULT", "3")),   # input.float(3.) في الكود المرسل
        max_age_bars=int(os.getenv("NW_SIGNAL_MAX_AGE_BARS", "1")),
        # لون الخط لم يعد شرطًا (قرار المستخدم 2026-11-06)؛ 1 يعيده اختياريًا
        require_newest_green=os.getenv("NW_REQUIRE_NEWEST_GREEN", "0").strip() in ("1", "true", "yes", "on"),
        # الفلتران التاليان خاصان بالنقل البحثي القديم ولا يُستخدمان إطلاقًا في
        # وضع envelope؛ معطّلان افتراضيًا حتى لا يلمسا تسميات المؤشر المرسل.
        boundary_correction=os.getenv("NW_BOUNDARY_CORRECT", "0").strip() in ("1", "true", "yes", "on"),
        min_trough_pct=float(os.getenv("NW_MIN_TROUGH_PCT", "0")),
    )
    state_path = os.getenv("NW_STATE_FILE", "data/state_nw.json")
    return NWBot(
        client=client,
        telegram=TelegramClient(token, chat_id),
        state_store=NWStateStore(state_path),
        cfg=cfg,
        state_path=state_path,
        trade_log=os.getenv("NW_TRADE_LOG", "data/trades_log_nw.csv"),
        halal_primary=os.getenv("NW_HALAL_PRIMARY", "data/daily_trend/halal_academy.json"),
        halal_extra=os.getenv("NW_HALAL_EXTRA", "data/daily_trend/halal_extra.json"),
        halal_cache=os.getenv("NW_HALAL_CACHE", "data/zigzag/halal_verdicts.json"),
        exclude_file=os.getenv("NW_EXCLUDE_FILE", "config/zz_exclude.txt"),
        # الوضع السببي: أول قيمة للخط عند الشمعة 500، فلا يكفي 501 شمعة لكشف
        # أي انقلاب ميل (خلل §23). نجلب 560 = 500 نافذة + 60 قيمة صالحة إضافية.
        # وزن طلب Binance واحد لكلا الحدّين (501–1000 ⇒ weight 5) فلا تكلفة إضافية.
        kline_limit=int(os.getenv("NW_KLINE_LIMIT", str(
            NW_WINDOW + (60 if cfg.mode in {"endpoint", "envelope"} else 1)))),
        commission_side_pct=float(os.getenv("NW_COMMISSION_SIDE_PCT", "0.1")),
        # الرسائل بالقالب المبسّط: بلا سطر تحذير افتراضيًا (1 يعيده اختياريًا)
        show_repaint_warning=os.getenv("NW_SHOW_REPAINT_WARNING", "0").strip() in ("1", "true", "yes", "on"),
        # متابعة كل الأحكام تحافظ على الخروج؛ بوابة الدخول مستقلة وإلزامية.
        include_all=os.getenv("NW_INCLUDE_ALL", "1").strip() not in ("0", "false", "no", "off"),
        exclude_pegged_stables=os.getenv("NW_EXCLUDE_PEGGED_STABLES", "1").strip() not in ("0", "false", "no", "off"),
        exclude_tokenized_stocks=os.getenv("NW_EXCLUDE_TOKENIZED_STOCKS", "0").strip() in ("1", "true", "yes", "on"),
        use_exclude_file=os.getenv("NW_USE_EXCLUDE_FILE", "0").strip() in ("1", "true", "yes", "on"),
    )
