"""بوت «تتبّع الاتجاه اليومي» — بديل نظام ZigZag++.

يرسل على تيليجرام: رسالة دخول (سعر الدخول + الوقف + الحجم + المخاطرة)،
وتحديث الوقف عندما يصبح مضمونًا، ورسالة إغلاق (السعر، السبب، الصافي، الرصيد).
ويعلن فتح/إغلاق بوابة السوق.

⚠️ لا يقرأ ولا يكتب أي ملف يخص ZigZag أو بقية الأنظمة:
    الحالة: data/state_daily_trend.json
    الدفتر: data/trades_log_daily_trend.csv
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from src.binance_client import BinanceClient
from src.daily_trend import (
    DailyTrendSettings,
    breakout_signal,
    market_gate,
    net_pnl_pct,
    position_notional,
    position_pnl_usd,
    update_trailing_stop,
    exit_reason,
)
from src.telegram_client import TelegramClient

STATE_FILE = os.getenv("DT_STATE_FILE", "data/state_daily_trend.json")
TRADE_LOG = os.getenv("DT_TRADE_LOG", "data/trades_log_daily_trend.csv")
HALAL_DIR = os.getenv("DT_HALAL_DIR", "data/zigzag")          # نفس مصدر الأحكام القائم
HALAL_EXTRA = os.getenv("DT_HALAL_EXTRA", "data/daily_trend/halal_extra.json")
EXCLUDE_FILE = os.getenv("DT_EXCLUDE_FILE", "config/zz_exclude.txt")
GATE_SYMBOL = os.getenv("DT_GATE_SYMBOL", "BTCUSDT")
ENTRY_WINDOW_MIN = int(os.getenv("DT_ENTRY_WINDOW_MIN", "90"))   # نافذة التنفيذ بعد الإغلاق اليومي
STOP_NOTICE_PCT = float(os.getenv("DT_STOP_NOTICE_PCT", "1.0"))      # إشعار عند رفع الوقف ≥1% من سعره
STOP_MIN_MINUTES = float(os.getenv("DT_STOP_MIN_MINUTES", "30"))     # فاصل أدنى بين رسالتي وقف لنفس العملة

_LOG = logging.getLogger("daily_trend")
_RIYADH = timezone(timedelta(hours=3))

TRADE_FIELDS = [
    "trade_id", "symbol", "entry_time_ms", "entry_time_local", "entry_price", "stop_price",
    "atr", "stop_pct", "notional", "risk_usd", "halal", "max_price", "exit_time_ms",
    "exit_time_local", "exit_price", "exit_reason", "net_pct", "pnl_usd", "equity_after",
]


# ───────────────────────────── أدوات ─────────────────────────────
def local_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, _RIYADH).strftime("%d %b %Y — %H:%M")


def fmt_price(value: float, tick: str | None = None) -> str:
    if not value or value != value:
        return "—"
    try:
        decimals = 0
        if tick:
            decimals = max(0, -int(round(__import__("math").log10(float(tick))))) if float(tick) < 1 else 0
        else:
            decimals = 8 if value < 1 else 6 if value < 100 else 2
        return f"{value:.{min(decimals, 10)}f}".rstrip("0").rstrip(".") or "0"
    except Exception:
        return f"{value:.6f}"


class DailyTrendBot:
    def __init__(self, token: str, chat_id: str) -> None:
        self.settings = DailyTrendSettings()
        self.client = BinanceClient(
            timeout=int(os.getenv("REQUEST_TIMEOUT", "20")),
            max_workers=int(os.getenv("MAX_WORKERS", "10")),
            base_url=os.getenv("BINANCE_BASE_URL", "https://data-api.binance.vision"),
        )
        self.telegram = TelegramClient(token, chat_id)
        self.state = self._load_state()
        self.symbols: list[str] = []
        self.ticks: dict[str, str] = {}
        self.halal: dict[str, int] = {}

    # ── الحالة ──
    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "equity": None,               # يُهيّأ من DT_CAPITAL أول مرة
            "peak_equity": None,
            "positions": {},              # symbol -> dict
            "last_entry_bar_close": 0,    # آخر شمعة يومية عولجت للدخول
            "gate_open": None,            # آخر حالة معروفة للبوابة
            "sent": [],                   # مفاتيح الرسائل المُرسلة (منع التكرار)
            "last_report_date": "",       # آخر يوم أُرسل فيه التقرير اليومي
            "event_log": [],
            "trades": 0,
            "wins": 0,
        }

    def _load_state(self) -> dict[str, Any]:
        state = self._default_state()
        path = os.path.join(os.getcwd(), STATE_FILE)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    state.update(json.load(f))
            except (OSError, ValueError):
                _LOG.warning("تعذّر قراءة الحالة — البدء من حالة جديدة")
        if state.get("equity") is None:
            state["equity"] = float(self.settings.capital)
            state["peak_equity"] = float(self.settings.capital)
        return state

    def save_state(self) -> None:
        path = os.path.join(os.getcwd(), STATE_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _already_sent(self, key: str) -> bool:
        return key in self.state["sent"]

    def _mark_sent(self, key: str) -> None:
        sent = self.state["sent"]
        sent.append(key)
        del sent[:-400]                      # لا تنمو بلا حد

    def _send_once(self, key: str, text: str) -> None:
        if self._already_sent(key):
            return
        self.telegram.send_message(text)
        self._mark_sent(key)
        _LOG.info("📤 %s", key)

    # ── الكون والأحكام ──
    def refresh_universe(self) -> None:
        from src.halal import load_cached_verdicts, refresh_if_stale

        verdicts = refresh_if_stale(HALAL_DIR, int(os.getenv("HALAL_REFRESH_HOURS", "6"))) or {}
        extra_path = os.path.join(os.getcwd(), HALAL_EXTRA)
        if os.path.exists(extra_path):
            try:
                with open(extra_path, encoding="utf-8") as f:
                    verdicts = {**verdicts, **json.load(f)}
            except (OSError, ValueError):
                pass
        self.halal = verdicts

        excluded: set[str] = set()
        path = os.path.join(os.getcwd(), EXCLUDE_FILE)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith(("#", "!")):
                        excluded.add(line.upper())

        from zigzag_bot import CRYPTO_B_BASES, is_pegged_stable_symbol, is_tokenized_stock_symbol

        symbols = []
        for sym in self.client.get_spot_usdt_symbols():
            if sym in excluded:
                continue
            try:
                if is_tokenized_stock_symbol(sym) and sym[:-4] not in CRYPTO_B_BASES:
                    continue
                if is_pegged_stable_symbol(sym):
                    continue
            except Exception:
                pass
            base = sym[:-4] if sym.endswith("USDT") else sym
            if verdicts.get(base) != 0:          # 0 = مباح فقط
                continue
            symbols.append(sym)
        self.symbols = symbols
        self.ticks = self.client.get_price_ticks()
        _LOG.info("الكون المباح: %s زوجًا", len(symbols))

    # ── إدارة المراكز ──
    def manage_positions(self, now_ms: int, gate: dict, prices: dict[str, float]) -> None:
        for sym in list(self.state["positions"]):
            pos = self.state["positions"][sym]
            price = prices.get(sym)
            df = self.client.get_klines(sym, "1d", self.settings.daily_limit)
            bar_highs = df[df["close_time"] >= int(pos["entry_time_ms"])]["high"]
            high_since = max([float(pos.get("max_price") or pos["entry_price"])]
                             + [float(h) for h in bar_highs] + ([float(price)] if price else []))
            new_stop = update_trailing_stop(
                pos["stop_price"], high_since, pos["atr"], self.settings.atr_mult
            )
            pos["max_price"] = high_since
            if new_stop > float(pos["stop_price"]):
                old_stop = float(pos["stop_price"])
                pos["stop_price"] = new_stop              # الوقف الحقيقي يتحدّث دائمًا (للدخول/الخروج)
                entry = float(pos["entry_price"])
                last_notified = float(pos.get("notified_stop") or old_stop)
                last_ms = int(pos.get("notified_ms") or 0)
                crossed_breakeven = new_stop >= entry > last_notified
                big_enough = new_stop >= last_notified * (1 + STOP_NOTICE_PCT / 100)
                cooled = (now_ms - last_ms) >= STOP_MIN_MINUTES * 60_000
                if crossed_breakeven or (big_enough and cooled):
                    pos["notified_stop"] = new_stop
                    pos["notified_ms"] = now_ms
                    self._announce_stop_raise(sym, pos, last_notified, new_stop, high_since, price, now_ms)

            reason = exit_reason(pos, price, gate["open"])
            if reason is None:
                continue
            if price is None:
                continue
            exit_price = float(price) if reason == "gate" else float(pos["stop_price"])
            self._close_position(sym, pos, exit_price, reason, now_ms)

    def _close_position(self, sym: str, pos: dict, exit_price: float, reason: str, now_ms: int) -> None:
        net = net_pnl_pct(pos["entry_price"], exit_price, self.settings.commission_side_pct)
        pnl = position_pnl_usd(pos, exit_price, self.settings.commission_side_pct)
        equity = float(self.state["equity"]) + pnl
        self.state["equity"] = equity
        self.state["peak_equity"] = max(float(self.state.get("peak_equity") or equity), equity)
        self.state["trades"] = int(self.state.get("trades", 0)) + 1
        if net > 0:
            self.state["wins"] = int(self.state.get("wins", 0)) + 1
        self.state["positions"].pop(sym, None)
        self.state["event_log"].append(
            {"type": "exit", "symbol": sym, "time": now_ms, "reason": reason, "net_pct": net}
        )
        self._append_trade({
            "trade_id": f"{sym}-{pos['entry_time_ms']}",
            "symbol": sym,
            "entry_time_ms": pos["entry_time_ms"],
            "entry_time_local": local_time(int(pos["entry_time_ms"])),
            "entry_price": pos["entry_price"],
            "stop_price": pos["entry_stop"],
            "atr": pos["atr"],
            "stop_pct": pos["stop_pct"],
            "notional": pos["notional"],
            "risk_usd": pos["risk_usd"],
            "halal": pos.get("halal", ""),
            "max_price": pos.get("max_price", pos["entry_price"]),
            "exit_time_ms": now_ms,
            "exit_time_local": local_time(now_ms),
            "exit_price": exit_price,
            "exit_reason": reason,
            "net_pct": round(net, 3),
            "pnl_usd": round(pnl, 2),
            "equity_after": round(equity, 2),
        })
        label = {"stop": "🛑 لمس الوقف المتحرك", "gate": "🌐 إغلاق بوابة السوق"}.get(reason, reason)
        icon = "✅" if net > 0 else "❌"
        self._send_once(
            f"exit:{sym}:{pos['entry_time_ms']}",
            "\n".join([
                f"{icon} أغلق الصفقة الآن — {sym}",
                f"{label}",
                "",
                f"💵 الدخول: {fmt_price(pos['entry_price'], self.ticks.get(sym))}",
                f"🚪 الخروج: {fmt_price(exit_price, self.ticks.get(sym))}",
                f"📊 الصافي: {net:+.2f}%  ({pnl:+.2f}$)",
                f"💼 الرصيد: {equity:.2f}$",
                "",
                f"🕒 {local_time(now_ms)}",
            ]),
        )

    def _announce_stop_raise(self, sym: str, pos: dict, old_stop: float, new_stop: float,
                            high_since: float, price: float | None, now_ms: int) -> None:
        """رسالة «ارفع الوقف» عند كل ارتفاع للوقف المتحرك — المستخدم لا يقرر شيئًا."""
        tick = self.ticks.get(sym)
        entry = float(pos["entry_price"])
        gain = (float(price) / entry - 1.0) * 100.0 if price else 0.0
        distance = (float(price) / new_stop - 1.0) * 100.0 if price else 0.0
        secured = new_stop >= entry
        head = ("🔒 ارفع الوقف (الصفقة أصبحت بلا خسارة)" if secured else "📈 ارفع الوقف")
        lines = [
            f"{head} — {sym}",
            "",
            f"🛑 من: {fmt_price(old_stop, tick)}  ←  إلى: {fmt_price(new_stop, tick)}",
            f"💵 الدخول: {fmt_price(entry, tick)}",
        ]
        if price:
            lines.append(f"📊 السعر الآن: {fmt_price(float(price), tick)}  ({gain:+.2f}%)")
            lines.append(f"📏 المسافة بين السعر والوقف: {distance:.2f}%")
        lines += [
            f"📈 أعلى سعر منذ الدخول: {fmt_price(high_since, tick)}",
            "",
            "👉 حدّث أمر الوقف (Stop) عند المنصة إلى الرقم أعلاه.",
            f"🕒 {local_time(now_ms)}",
        ]
        self._send_once(f"stop:{sym}:{new_stop:.8f}", "\n".join(lines))

    def send_daily_report_if_due(self, now_ms: int, gate: dict, prices: dict[str, float]) -> None:
        """تقرير يومي (مرة واحدة كل يوم) — مثل تقرير الزيجزاج السابق."""
        day = datetime.fromtimestamp(now_ms / 1000, _RIYADH).strftime("%Y-%m-%d")
        if self.state.get("last_report_date") == day:
            return
        equity = float(self.state["equity"])
        start = float(self.settings.capital)
        peak = float(self.state.get("peak_equity") or equity)
        trades = int(self.state.get("trades", 0))
        wins = int(self.state.get("wins", 0))
        rate = (wins / trades * 100.0) if trades else 0.0
        gate_icon = "🟢 مفتوحة" if gate["open"] else "🔴 مغلقة"
        lines = [
            f"📊 التقرير اليومي — {datetime.fromtimestamp(now_ms / 1000, _RIYADH).strftime('%d %b %Y')}",
            "",
            f"💼 الرصيد: {equity:.2f}$  ({(equity / start - 1) * 100:+.1f}% من البداية)  •  أعلى رصيد: {peak:.2f}$",
            f"🎯 الصفقات المغلقة: {trades}  •  ناجحة: {wins}  •  نسبة الفوز: {rate:.0f}%" if trades
            else "🎯 الصفقات المغلقة: 0 (لا صفقات بعد)",
            f"🌐 بوابة السوق: {gate_icon}  •  BTC "
            f"{fmt_price(gate['close'], self.ticks.get(GATE_SYMBOL))} مقابل EMA{self.settings.gate_ema_len}: "
            f"{fmt_price(gate['ema'], self.ticks.get(GATE_SYMBOL))}",
        ]
        positions = self.state["positions"]
        if positions:
            lines += ["", f"📌 المراكز المفتوحة ({len(positions)}):"]
            for sym, pos in positions.items():
                tick = self.ticks.get(sym)
                price = prices.get(sym)
                entry = float(pos["entry_price"])
                gain = (float(price) / entry - 1.0) * 100.0 if price else 0.0
                days = max(0, int((now_ms - int(pos["entry_time_ms"])) / 86_400_000))
                lines.append(
                    f"• {sym} — دخول {fmt_price(entry, tick)} | الآن "
                    f"{fmt_price(float(price), tick) if price else '—'} ({gain:+.2f}%) | "
                    f"الوقف المطلوب {fmt_price(float(pos['stop_price']), tick)} | مدة {days} يومًا"
                )
            lines += ["", "👉 تأكّد أن أوامر الوقف على المنصة مطابقة للوقف المطلوب أعلاه."]
        else:
            lines += ["", "📌 المراكز المفتوحة: لا يوجد" + ("" if gate["open"] else " (السوق النقدي — البوابة مغلقة)")]

        today_events = [e for e in self.state.get("event_log", [])
                        if datetime.fromtimestamp(e["time"] / 1000, _RIYADH).strftime("%Y-%m-%d") == day]
        if today_events:
            lines += ["", "🗓 أحداث اليوم:"]
            for e in today_events[-8:]:
                if e["type"] == "entry":
                    lines.append(f"• دخول {e['symbol']} عند {fmt_price(float(e['entry']), self.ticks.get(e['symbol']))}")
                else:
                    lines.append(f"• إغلاق {e['symbol']} ({e.get('reason', '')}) بصافي {e.get('net_pct', 0):+.2f}%")

        lines += [
            "",
            "📋 لا تنفّذ شيئًا الآن — ستستلم رسالة «ارفع الوقف» أو «أغلق الصفقة» عند الحاجة.",
            f"🕒 {local_time(now_ms)}",
        ]
        self._send_once(f"report:{day}", "\n".join(lines))
        self.state["last_report_date"] = day

    # ── الدخول ──
    def scan_entries(self, now_ms: int, gate: dict) -> None:
        if not gate["open"]:
            return
        free = self.settings.max_positions - len(self.state["positions"])
        if free <= 0:
            return
        # آخر شمعة يومية مغلقة (من البيتكوين — نفس توقيت كل الأزواج)
        ref = self.client.get_klines(GATE_SYMBOL, "1d", 10)
        ref = ref[ref["close_time"] < now_ms]
        if ref.empty:
            return
        bar_close = int(ref["close_time"].iloc[-1])
        if self.state.get("last_entry_bar_close") == bar_close:
            return                                    # عولجت هذه الشمعة سابقًا
        if now_ms - bar_close > ENTRY_WINDOW_MIN * 60_000:
            self.state["last_entry_bar_close"] = bar_close
            _LOG.info("فاتت نافذة التنفيذ للشمعة %s — تُتجاوز", local_time(bar_close))
            return

        candidates: list[dict] = []
        for sym in self.symbols:
            if sym == GATE_SYMBOL:
                continue
            try:
                df = self.client.get_klines(sym, "1d", self.settings.daily_limit)
            except Exception as exc:                  # لا نُسقط الكون بسبب زوج واحد
                _LOG.debug("تخطي %s: %s", sym, exc)
                continue
            sig = breakout_signal(df, now_ms, self.settings)
            if not sig:
                continue
            if int(sig["bar_close_time"]) != bar_close:
                continue
            sig["symbol"] = sym
            try:
                sig["quote_volume"] = float(df["quote_asset_volume"].iloc[-1])
            except Exception:
                sig["quote_volume"] = 0.0
            candidates.append(sig)

        if not candidates:
            self.state["last_entry_bar_close"] = bar_close
            return
        candidates.sort(key=lambda s: s["quote_volume"], reverse=True)
        for sig in candidates[:free]:
            self._open_position(sig, bar_close, now_ms)
        self.state["last_entry_bar_close"] = bar_close

    def _open_position(self, sig: dict, bar_close: int, now_ms: int) -> None:
        sym = sig["symbol"]
        equity = float(self.state["equity"])
        notional = position_notional(
            equity, self.settings.risk_pct, sig["entry_price"], sig["stop_price"],
            self.settings.max_positions,
        )
        if notional <= 0:
            return
        risk_usd = notional * sig["stop_pct"] / 100.0
        self.state["positions"][sym] = {
            "symbol": sym,
            "entry_time_ms": bar_close,
            "entry_price": sig["entry_price"],
            "stop_price": sig["stop_price"],
            "entry_stop": sig["stop_price"],
            "atr": sig["atr"],
            "stop_pct": sig["stop_pct"],
            "notional": notional,
            "risk_usd": risk_usd,
            "max_price": sig["entry_price"],
            "notified_stop": sig["stop_price"],
            "notified_ms": 0,
            "halal": "مباح",
        }
        self.state["event_log"].append(
            {"type": "entry", "symbol": sym, "time": now_ms, "entry": sig["entry_price"]}
        )
        tick = self.ticks.get(sym)
        self._send_once(
            f"entry:{sym}:{bar_close}",
            "\n".join([
                f"🚀 دخول جديد — {sym}",
                "📈 نظام تتبّع الاتجاه اليومي",
                "",
                f"💵 سعر الدخول: {fmt_price(sig['entry_price'], tick)}",
                f"🛑 الوقف: {fmt_price(sig['stop_price'], tick)}  (−{sig['stop_pct']:.2f}%)",
                f"📦 حجم المركز: {notional:.2f}$  (مخاطرة {risk_usd:.2f}$ = {self.settings.risk_pct:g}%)",
                f"📊 اختراق أعلى {self.settings.lookback} يومًا "
                f"({fmt_price(sig['breakout_high'], tick)}) بـ +{sig['breakout_pct']:.2f}%",
                "",
                f"🌐 السوق: BTC فوق EMA{self.settings.gate_ema_len}(4h) ✅",
                "⏳ الخروج: لمس الوقف المتحرك أو إغلاق بوابة السوق",
                f"🕒 {local_time(now_ms)}",
            ]),
        )

    # ── البوابة ──
    def announce_gate(self, gate: dict, now_ms: int) -> None:
        previous = self.state.get("gate_open")
        if previous is None:
            self.state["gate_open"] = gate["open"]
            return
        if previous == gate["open"]:
            return
        self.state["gate_open"] = gate["open"]
        if gate["open"]:
            text = "\n".join([
                "🟢 فتح بوابة السوق",
                f"BTC فوق EMA{self.settings.gate_ema_len}(4h) — يُسمح بالدخول",
                f"BTC: {fmt_price(gate['close'], self.ticks.get(GATE_SYMBOL))} • "
                f"المتوسط: {fmt_price(gate['ema'], self.ticks.get(GATE_SYMBOL))}",
                f"🕒 {local_time(now_ms)}",
            ])
        else:
            text = "\n".join([
                "🔴 إغلاق بوابة السوق",
                f"BTC تحت EMA{self.settings.gate_ema_len}(4h) — إيقاف الدخول وإغلاق المراكز",
                f"BTC: {fmt_price(gate['close'], self.ticks.get(GATE_SYMBOL))} • "
                f"المتوسط: {fmt_price(gate['ema'], self.ticks.get(GATE_SYMBOL))}",
                f"🕒 {local_time(now_ms)}",
            ])
        self._send_once(f"gate:{int(gate['open'])}:{now_ms // 86400000}", text)

    # ── دفتر الصفقات ──
    def _append_trade(self, row: dict) -> None:
        path = os.path.join(os.getcwd(), TRADE_LOG)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        exists = os.path.exists(path)
        df = pd.DataFrame([row], columns=TRADE_FIELDS)
        df.to_csv(path, mode="a", header=not exists, index=False, encoding="utf-8-sig")

    # ── دورة ──
    def run_cycle(self) -> None:
        now_ms = self.client.get_server_time()
        btc_4h = self.client.get_klines(GATE_SYMBOL, "4h", self.settings.gate_limit)
        gate = market_gate(btc_4h, now_ms, self.settings.gate_ema_len)
        prices = self.client.get_all_prices()
        self.announce_gate(gate, now_ms)
        self.manage_positions(now_ms, gate, prices)
        self.scan_entries(now_ms, gate)
        self.send_daily_report_if_due(now_ms, gate, prices)
        self.save_state()

    def run(self) -> None:
        poll = int(os.getenv("POLL_SECONDS", "60"))
        while True:
            try:
                self.run_cycle()
            except Exception:
                _LOG.exception("فشلت الدورة — نتابع")
            time.sleep(poll)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID مطلوبان")
    bot = DailyTrendBot(token, chat_id)
    _LOG.info("الإعدادات الفعلية: %s", bot.settings.describe())
    bot.refresh_universe()
    if os.getenv("DT_ONCE", "0").strip().lower() in ("1", "true", "yes"):
        bot.run_cycle()
        return
    bot.run()


if __name__ == "__main__":
    main()
