"""دورة واحدة من «تجربة تتبّع الاتجاه» على كل أزواج USDT — فريم 1H.

⋮ يُشغَّل من وركفلو ``nw-trend-experiment.yml`` كل ساعة :03 بتوقيت الرياض.
⋮ نظام **مستقل وموازٍ**: يقرأ/يكتب ``data/nw_trend/*`` فقط.
⋮ لا يلمس أي حالة/دفتر/رسالة للأنظمة الأخرى.
⋮ لا ``push`` trigger، لا توكن داخل الملفات، لا إعادة إرسال عند التكرار.

الصلابة:
  * يعالج الشموع المغلقة الفائتة (حد أقصى 6) إن تأخر كرون GitHub.
  * مفتاح ``symbol|bar_ms|action`` يمنع تكرار الرسائل، ويُحفظ بعد كل إرسال.
  * إن فشل إرسال رسالة لا يُحدَّث ``last_bar_ms`` ⇒ الدورة القادمة تعيد المحاولة.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.binance_client import BinanceClient  # noqa: E402  (قراءة فقط)
from src.nw_trend import (  # noqa: E402
    INTERVAL_MS, MAX_CATCHUP_BARS, WARMUP, entry_message, exit_message,
    fmt_date, fmt_time, indicators, is_excluded, step,
)
from src.telegram_client import TelegramClient, install_redaction  # noqa: E402

STATE_FILE = Path(os.getenv("NW_TREND_STATE_FILE", "data/nw_trend/state.json"))
TRADE_LOG = Path(os.getenv("NW_TREND_TRADE_LOG", "data/nw_trend/trades.csv"))
MESSAGE_LOG = Path(os.getenv("NW_TREND_MESSAGE_LOG", "data/nw_trend/messages.jsonl"))

CSV_FIELDS = [
    "symbol", "entry_bar_ms", "entry_time_riyadh", "entry", "init_stop",
    "ema200", "donchian20", "atr14", "exit_bar_ms", "exit_time_riyadh",
    "exit", "trail", "gross_pct", "net_pct", "holding_bars", "exit_reason",
]


def _blank_state() -> dict:
    return {
        "system": "nw-trend-1h-experiment",
        "version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "updated_utc": None,
        "last_run_utc": None,
        "runs": 0,
        "last_bar_ms": {},
        "sent": [],
        "positions": {},
        "trades": [],
        "day_reports": [],
        "counters": {
            "entries": 0, "exits": 0, "messages": 0, "send_errors": 0,
            "fetch_errors": 0, "insufficient_history": 0,
        },
        "last_run": {},
    }


def load_state(path: Path) -> dict:
    if not path.exists():
        return _blank_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("system") != "nw-trend-1h-experiment":
            raise ValueError("ملف حالة ل نظام آخر")
        blank = _blank_state()
        for key, value in blank.items():
            data.setdefault(key, value)
        return data
    except Exception as exc:  # لا نُتلف حالة سليمة بسبب ملف تالف
        logging.warning("تعذّر قراءة الحالة (%s) — بدء حالة جديدة: %s", path, exc)
        try:
            path.replace(path.with_suffix(path.suffix + ".corrupt"))
        except Exception:
            pass
        return _blank_state()


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    state["updated_utc"] = datetime.now(timezone.utc).isoformat()
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _existing_message_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    if not path.exists():
        return keys
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                keys.add(str(json.loads(line)["key"]))
            except Exception:
                continue
    return keys


def append_message(path: Path, key: str, symbol: str, bar_ms: int,
                   action: str, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "key": key, "symbol": symbol, "bar_ms": int(bar_ms), "action": action,
        "time_riyadh": fmt_time(bar_ms), "text": text,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _trade_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    if not path.exists():
        return keys
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            keys.add(f"{row.get('symbol')}|{row.get('entry_bar_ms')}|{row.get('exit_bar_ms')}")
    return keys


def append_trade(path: Path, row: dict, known: set[str]) -> None:
    key = f"{row['symbol']}|{row['entry_bar_ms']}|{row['exit_bar_ms']}"
    if key in known:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})
    known.add(key)


def send_once(telegram: TelegramClient, state: dict, path: Path, key: str,
              symbol: str, bar_ms: int, action: str, text: str,
              message_keys: set[str]) -> str:
    """يُرجع 'sent' | 'duplicate' | 'failed' — ويحفظ الحالة فورًا بعد النجاح."""
    if key in state["sent"] or key in message_keys:
        return "duplicate"
    try:
        telegram.send_message(text)  # عند TELEGRAM_MUTE=1 يصبح no-op
    except Exception as exc:
        logging.error("فشل إرسال %s: %s", key, exc)
        state["counters"]["send_errors"] += 1
        return "failed"
    state["sent"].append(key)
    if len(state["sent"]) > 400:
        state["sent"] = state["sent"][-400:]
    state["counters"]["messages"] += 1
    append_message(MESSAGE_LOG, key, symbol, bar_ms, action, text)
    message_keys.add(key)
    save_state(path, state)  # حفظ تدريجي: لا تفقد المفتاح عند انقطاع لاحق
    return "sent"


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    install_redaction()
    log = logging.getLogger("nw-trend")

    client = BinanceClient(
        timeout=int(os.getenv("REQUEST_TIMEOUT", "20")),
        max_workers=int(os.getenv("MAX_WORKERS", "10")),
        base_url=os.getenv("BINANCE_BASE_URL", "https://data-api.binance.vision"),
    )
    telegram = TelegramClient(
        token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    )

    server = client.get_server_time()
    last_closed_open = (server // INTERVAL_MS) * INTERVAL_MS - INTERVAL_MS
    log.info("وقت الخادم %s | آخر شمعة 1H مغلقة %s (الرياض)",
             fmt_time(server), fmt_time(last_closed_open))

    all_symbols = client.get_spot_usdt_symbols("USDT")
    symbols = sorted(s for s in all_symbols if not is_excluded(s))
    log.info("الكون: %s زوج USDT (%s مستبعدة: مستقرة مربوطة)",
             len(symbols), len(all_symbols) - len(symbols))
    if not symbols:
        log.error("الكون فارغ — إلغاء الدورة")
        return 1

    frames = client.get_klines_for_symbols(symbols, "1h", limit=260)
    state = load_state(STATE_FILE)
    message_keys = _existing_message_keys(MESSAGE_LOG)
    trade_keys = _trade_keys(TRADE_LOG)

    run_stats = {
        "bar_ms": int(last_closed_open),
        "time_riyadh": fmt_time(last_closed_open),
        "symbols": len(symbols), "evaluated": 0, "skipped": 0,
        "fetch_errors": 0, "insufficient_history": 0,
        "entries": 0, "exits": 0, "messages": 0, "failed": 0,
    }

    for sym in symbols:
        df = frames.get(sym)
        if df is None or df.empty:
            run_stats["fetch_errors"] += 1
            state["counters"]["fetch_errors"] += 1
            continue
        try:
            df = df[df["close_time"] < server]
            if len(df) < WARMUP + 1:
                run_stats["insufficient_history"] += 1
                state["counters"]["insufficient_history"] += 1
                continue
            t = df["open_time"].to_numpy(dtype=np.int64)
            h = df["high"].to_numpy(dtype=float)
            l = df["low"].to_numpy(dtype=float)
            c = df["close"].to_numpy(dtype=float)
            hits = np.where(t == last_closed_open)[0]
            if not len(hits):
                run_stats["fetch_errors"] += 1
                state["counters"]["fetch_errors"] += 1
                continue
            last = int(hits[0])
            ind = indicators(h, l, c)

            prev = state["last_bar_ms"].get(sym)
            if prev is None:
                js = [last]
            else:
                js = [j for j, tj in enumerate(t)
                      if int(prev) < int(tj) <= int(last_closed_open) and j >= WARMUP]
                js = js[-MAX_CATCHUP_BARS:]
            if not js:
                run_stats["skipped"] += 1
                continue

            symbol_failed = False
            for j in js:
                bar_ms = int(t[j])
                action, payload, newpos = step(
                    j, ind, t, c, l, state["positions"].get(sym)
                )
                if action is None:
                    if newpos is not None:
                        state["positions"][sym] = newpos
                    continue

                key = f"{sym}|{bar_ms}|{action}"
                text = (entry_message(sym, bar_ms, payload) if action == "entry"
                        else exit_message(sym, bar_ms, payload))
                status = send_once(telegram, state, STATE_FILE, key, sym,
                                   bar_ms, action, text, message_keys)
                if status == "failed":
                    symbol_failed = True
                    run_stats["failed"] += 1
                    break  # لا تُحدَّث last_bar_ms ⇒ إعادة المحاولة في الدورة القادمة

                run_stats["messages"] += 1
                if action == "entry":
                    state["positions"][sym] = payload
                    state["counters"]["entries"] += 1
                    run_stats["entries"] += 1
                else:
                    open_pos = state["positions"].get(sym) or {}
                    state["positions"].pop(sym, None)
                    row = {
                        "symbol": sym,
                        "entry_bar_ms": payload["entry_bar_ms"],
                        "entry_time_riyadh": fmt_time(payload["entry_bar_ms"]),
                        "entry": payload["entry"],
                        "init_stop": open_pos.get("init_stop", ""),
                        "ema200": open_pos.get("ema200", ""),
                        "donchian20": open_pos.get("donchian20", ""),
                        "atr14": open_pos.get("atr14", ""),
                        "exit_bar_ms": payload["exit_bar_ms"],
                        "exit_time_riyadh": fmt_time(payload["exit_bar_ms"]),
                        "exit": payload["exit"], "trail": payload["trail"],
                        "gross_pct": payload["gross_pct"], "net_pct": payload["net_pct"],
                        "holding_bars": payload["holding_bars"],
                        "exit_reason": payload["exit_reason"],
                    }
                    # تفاصيل الدخول محفوظة في الحالة عند فتح المركز
                    state["trades"].append({**row, "symbol": sym})
                    if len(state["trades"]) > 2000:
                        state["trades"] = state["trades"][-2000:]
                    append_trade(TRADE_LOG, row, trade_keys)
                    state["counters"]["exits"] += 1
                    run_stats["exits"] += 1
                save_state(STATE_FILE, state)

            if symbol_failed:
                continue
            state["last_bar_ms"][sym] = int(last_closed_open)
            run_stats["evaluated"] += 1
        except Exception as exc:  # رمز واحد معطوب لا يُسقط الدورة كلها
            log.exception("خطأ في %s: %s", sym, exc)
            run_stats["fetch_errors"] += 1
            state["counters"]["fetch_errors"] += 1

    state["runs"] += 1
    state["last_run_utc"] = datetime.now(timezone.utc).isoformat()
    state["last_run"] = run_stats
    save_state(STATE_FILE, state)
    log.info("انتهت الدورة: %s", json.dumps(run_stats, ensure_ascii=False))
    log.info("مراكز مفتوحة: %s | رسائل مرسلة تراكميًا: %s",
             len(state["positions"]), state["counters"]["messages"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
