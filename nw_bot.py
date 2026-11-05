"""نظام NW 1H — نظام سادس **مستقل تمامًا** عن الأنظمة الخمسة القائمة.

المصدر: Nadaraya-Watson Smoothers [LuxAlgo] (CC BY-NC-SA 4.0) على فريم الساعة.
القواعد: ▲ + شمعة تالية خضراء + أحدث قطعة خضراء ⇒ توصية دخول؛
         ▼ ⇒ إشارة خروج (مع سعر الدخول/الخروج والنتيجة ربحًا أو خسارةً)؛
         تكرار ▲ ⇒ «إشارة دخول ثانية». بلا أهداف ولا وقف.
         **الدخول من المؤشر وحده** — لا بوابة BTC ولا أي فلتر شرعي؛
         الإرسال يشمل كل أزواج USDT الفورية بجميع أحكامها، ويُعرض الحكم في الرسالة.

العزل:
  * الحالة: data/state_nw.json فقط · الدفتر: data/trades_log_nw.csv فقط
  * الوركفلو: .github/workflows/nw-bot.yml فقط (concurrency خاص به)
  * لا يقرأ ولا يكتب state_*.json / trades_log_*.csv لأي نظام آخر
  * لا يستورد منطق أي نظام آخر (يستخدم binance_client / telegram_client / halal للقراءة فقط)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

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
        show_repaint_warning: bool = True,
        interval: str = "1h",
        include_all: bool = True,             # كل العملات بجميع أحكامها (قرار المستخدم)
        exclude_pegged_stables: bool = True,  # ليست عملات متقلبة ⇒ إشارات ضجيج
        exclude_tokenized_stocks: bool = False,
        use_exclude_file: bool = False,       # قائمة الاستبعاد اليدوية معطّلة افتراضيًا
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
        self.symbols: list[str] = []
        self.verdicts: dict[str, int] = {}      # للتسمية فقط، لا كفلتر
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
                except (OSError, ValueError, TypeError):
                    pass
        primary = load_primary_verdicts(str(Path.cwd() / self.halal_primary))
        verdicts = merge_with_primary(verdicts, primary)
        if not primary:
            _LOG.warning("⚠️ ملف المصدر الأساسي للأحكام غير موجود: %s", self.halal_primary)
        self.verdicts = verdicts
        return verdicts

    # ── الكون: كل أزواج USDT الفورية بجميع أحكامها (قرار المستخدم 2026-11-06) ──
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

        symbols = []
        for sym in self.client.get_spot_usdt_symbols():
            if self.use_exclude_file and sym in excluded and sym not in forced_keep:
                continue
            if self.exclude_pegged_stables and _is_pegged_stable(sym):
                continue
            if self.exclude_tokenized_stocks and _is_tokenized_stock(sym, forced_keep):
                continue
            # الافتراضي: لا فلتر شرعي إطلاقًا. إن عُطّل NW_INCLUDE_ALL يعود فلتر «المباح فقط».
            if not self.include_all:
                base = sym[:-4] if sym.endswith("USDT") else sym
                if self.verdicts.get(base) != 0:
                    continue
            # لا توجد أي بوابة مرتبطة بـ BTC: الدخول من المؤشر وحده (▲ + شمعة خضراء + خط أخضر)
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
    def _apply(self, sym: str, res: dict, state: dict) -> dict:
        from src.halal import verdict_label

        ruling = verdict_label(self.verdicts, sym)      # الحكم الفعلي، لا «مباح» ثابت
        item = symbol_state(state, sym)
        prev_buy = set(state["prev_buy_bars"].get(sym, []))
        prev_sell = set(state["prev_sell_bars"].get(sym, []))
        hist = set(state["buy_history"].get(sym, []))
        events = {"entry": 0, "repeat": 0, "exit": 0, "skipped_same_label": 0}

        for rec in res["buy"]:
            B = rec["bar_time"]
            if B in prev_buy:
                events["skipped_same_label"] += 1
                continue
            pos = item["position"]
            if B in hist and pos is None:
                continue                      # عودة تسمية قديمة بعد إغلاق التوصية
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

        state["prev_buy_bars"][sym] = _cap([r["bar_time"] for r in res["buy"]], 10)
        state["prev_sell_bars"][sym] = _cap([r["bar_time"] for r in res["sell"]], 10)
        state["buy_history"][sym] = _cap(sorted(hist), 60)
        return events

    # ── دورة واحدة ─────────────────────────────────────────────────────────
    def run_once(self, now_ms: int | None = None, universe: list[str] | None = None) -> dict:
        now_ms = int(now_ms if now_ms is not None else self.client.get_server_time())
        if universe is not None:
            self._load_verdicts()          # للأحكام حتى عند تمرير كون مخصّص (الاختبارات/التجربة)
        symbols = universe if universe is not None else self.refresh_universe()
        frames = self.client.get_klines_for_symbols(symbols, self.interval, self.kline_limit)
        state = self.store.load()
        summary = {
            "now_ms": now_ms, "symbols": len(symbols), "evaluated": 0,
            "not_ready": 0, "ignored_retro": 0,
            "entry": 0, "repeat": 0, "exit": 0, "skipped_same_label": 0,
        }
        for sym in sorted(symbols):
            o, c, t = candles_closed(frames.get(sym), now_ms)
            res = evaluate_symbol(o, c, t, self.cfg)
            if not res["ok"]:
                summary["not_ready"] += 1
                continue
            summary["evaluated"] += 1
            summary["ignored_retro"] += res["ignored_retro"]
            ev = self._apply(sym, res, state)
            for key in ("entry", "repeat", "exit", "skipped_same_label"):
                summary[key] += ev[key]

        state["cycles"] = int(state["cycles"]) + 1
        state["last_cycle_ms"] = now_ms
        self.store.save(state)
        self.last_summary = summary
        _LOG.info("دورة NW: %s", summary)
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
        max_age_bars=int(os.getenv("NW_SIGNAL_MAX_AGE_BARS", "1")),
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
        kline_limit=int(os.getenv("NW_KLINE_LIMIT", str(NW_WINDOW + 1))),
        commission_side_pct=float(os.getenv("NW_COMMISSION_SIDE_PCT", "0.1")),
        show_repaint_warning=os.getenv("NW_SHOW_REPAINT_WARNING", "1").strip() not in ("0", "false", "no", "off"),
        # «الإرسال يشمل كل العملات بجميع أحكامها» — لا فلتر شرعي ولا بوابة BTC
        include_all=os.getenv("NW_INCLUDE_ALL", "1").strip() not in ("0", "false", "no", "off"),
        exclude_pegged_stables=os.getenv("NW_EXCLUDE_PEGGED_STABLES", "1").strip() not in ("0", "false", "no", "off"),
        exclude_tokenized_stocks=os.getenv("NW_EXCLUDE_TOKENIZED_STOCKS", "0").strip() in ("1", "true", "yes", "on"),
        use_exclude_file=os.getenv("NW_USE_EXCLUDE_FILE", "0").strip() in ("1", "true", "yes", "on"),
    )
