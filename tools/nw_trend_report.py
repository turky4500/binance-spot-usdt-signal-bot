"""التقرير الختامي اليومي لتجربة تتبّع الاتجاه (23:59 بتوقيت الرياض).

يقرأ ``data/nw_trend/*`` فقط، ويرسل رسالة واحدة (مُجزّأة إن طالت) ثم يحفظ
نسخة Markdown في ``data/nw_trend/reports/YYYY-MM-DD.md``.

بقرار المستخدم 2026-11-16: **لا إيقاف تلقائي** ⇒ التقرير يتكرر كل يوم
حتى يطلب المستخدم الإيقاف. مفتاح ``day_reports`` يمنع تكرار تقرير اليوم نفسه.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.binance_client import BinanceClient  # noqa: E402  (قراءة فقط)
from src.nw_trend import RIYADH, fmt_date, fmt_price, fmt_time, metrics  # noqa: E402
from src.telegram_client import TelegramClient, install_redaction  # noqa: E402

STATE_FILE = Path(os.getenv("NW_TREND_STATE_FILE", "data/nw_trend/state.json"))
TRADE_LOG = Path(os.getenv("NW_TREND_TRADE_LOG", "data/nw_trend/trades.csv"))
REPORT_DIR = Path(os.getenv("NW_TREND_REPORT_DIR", "data/nw_trend/reports"))
MAX_CHARS = 3500


def _load_trades() -> list[dict]:
    if not TRADE_LOG.exists():
        return []
    with TRADE_LOG.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        try:
            out.append({
                "symbol": r["symbol"],
                "entry_bar_ms": int(r["entry_bar_ms"]),
                "exit_bar_ms": int(r["exit_bar_ms"]),
                "net_pct": float(r["net_pct"]),
                "holding_bars": float(r["holding_bars"] or 0),
                "exit_time_riyadh": r.get("exit_time_riyadh", ""),
                "entry_time_riyadh": r.get("entry_time_riyadh", ""),
            })
        except Exception:
            continue
    return out


def _pct(x) -> str:
    return f"{x:+.3f}%"


def _metrics_block(m: dict) -> str:
    return (
        f"صفقات مغلقة: {m['trades']} | رابحة {m['wins']} | خاسرة {m['losses']} | "
        f"نسبة الفوز {m['win_rate_pct']:.2f}%\n"
        f"متوسط الرابح {_pct(m['avg_win_net'])} | متوسط الخاسر {_pct(m['avg_loss_net'])} | "
        f"Payoff {m['payoff']}\n"
        f"إجمالي صافي (مجموعي) {_pct(m['total_additive_net_pct'])} | "
        f"مركّب {_pct(m['compounded_return_pct'])} | PF {m['profit_factor']}\n"
        f"أفضل {_pct(m['best_net_pct'])} | أسوأ {_pct(m['worst_net_pct'])} | "
        f"أقصى تراجع {m['max_drawdown_pct']:.2f}% | متوسط المدة {m['avg_holding_bars']} ساعة"
    )


def build_report(state: dict, trades: list[dict], date: str,
                 open_rows: list[tuple[str, dict, float | None]]) -> tuple[str, str]:
    today = [r for r in trades if str(r["exit_time_riyadh"]).startswith(date)]
    cum = metrics(trades)
    day = metrics(today)
    run = state.get("last_run") or {}
    counters = state.get("counters") or {}

    lines = [
        f"🧪 تقرير تجربة TREND · 1H — يوم {date} (الرياض)",
        "",
        "■ التشغيل",
        f"الكون: {run.get('symbols', '—')} زوج USDT | دورات مكتملة منذ البداية: "
        f"{state.get('runs', 0)} | آخر شمعة مُقيَّمة: {run.get('time_riyadh', '—')}",
        f"الدورة الأخيرة: evaluated={run.get('evaluated', 0)} · "
        f"دخول={run.get('entries', 0)} · خروج={run.get('exits', 0)} · "
        f"رسائل={run.get('messages', 0)} · أخطاء جلب={run.get('fetch_errors', 0)} · "
        f"إرسال فاشل={run.get('failed', 0)}",
        f"تراكمي: دخول {counters.get('entries', 0)} · خروج {counters.get('exits', 0)} · "
        f"رسائل {counters.get('messages', 0)} · أخطاء إرسال {counters.get('send_errors', 0)}",
        "",
        "■ صفقات اليوم",
        _metrics_block(day),
        "",
        "■ تراكمي منذ بداية التجربة",
        _metrics_block(cum),
        "",
        "■ المراكز المفتوحة الآن",
    ]
    if open_rows:
        for sym, pos, last_price in open_rows:
            pnl = ""
            if last_price:
                pnl = f" | تقريبي الآن {_pct((last_price / pos['entry'] - 1) * 100 - 0.2)}"
            lines.append(
                f"• {sym}: دخول {fmt_price(pos['entry'])} @ {fmt_time(pos['entry_bar_ms'])}"
                f" | trail {fmt_price(pos['trail'])}{pnl}"
            )
    else:
        lines.append("• لا توجد مراكز مفتوحة.")

    if today:
        lines += ["", "■ صفقات اليوم تفصيلًا"]
        for r in sorted(today, key=lambda x: x["exit_bar_ms"]):
            lines.append(f"• {r['symbol']}: {_pct(r['net_pct'])} | "
                         f"{r['entry_time_riyadh']} → {r['exit_time_riyadh']} | "
                         f"{int(r['holding_bars'])} ساعة")

    lines += [
        "",
        "■ الجدولة",
        "كل ساعة :03 بتوقيت الرياض (cron '3 * * * *' UTC) · التقرير 23:59 الرياض"
        " (cron '59 20 * * *' UTC).",
        "النظام مستمر بلا إيقاف تلقائي — الإيقاف عبر تعطيل الوركفلو أو "
        "ضبط المتغيّر NW_TREND_ENABLED=false.",
        "",
        "⚠️ توصيات/محاكاة تجريبية فقط: لا تنفيذ فعلي، ولا نصيحة استثمارية. "
        "النتائج بعد خصم 0.2% عمولة، والخروج مُملأ عند خط التتبّع (تفاؤل محاكاة).",
    ]
    body = "\n".join(lines)
    md = f"# تقرير تجربة TREND · 1H — {date}\n\n```text\n{body}\n```\n"
    return body, md


def send_chunks(telegram: TelegramClient, text: str) -> None:
    if len(text) <= MAX_CHARS:
        telegram.send_message(text)
        return
    parts: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > MAX_CHARS and buf:
            parts.append(buf)
            buf = ""
        buf += line
    if buf:
        parts.append(buf)
    for i, part in enumerate(parts, 1):
        telegram.send_message(f"{part}\n\n(جزء {i}/{len(parts)})")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    install_redaction()
    log = logging.getLogger("nw-trend-report")

    date = os.getenv("NW_TREND_REPORT_DATE") or datetime.now(RIYADH).strftime("%Y-%m-%d")
    force = os.getenv("FORCE_REPORT", "0").strip() in ("1", "true", "yes", "on")

    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    already = date in (state.get("day_reports") or [])
    if already and not force:
        log.info("تقرير %s أُرسل بالفعل — تخطٍّ (FORCE_REPORT=1 لإعادة الإرسال)", date)
        return 0

    trades = _load_trades()
    prices: dict[str, float] = {}
    try:
        client = BinanceClient(
            timeout=int(os.getenv("REQUEST_TIMEOUT", "20")),
            base_url=os.getenv("BINANCE_BASE_URL", "https://data-api.binance.vision"),
        )
        prices = {k: float(v) for k, v in client.get_all_prices().items()}
    except Exception as exc:
        log.warning("تعذّر جلب الأسعار الحالية (التقرير يكمل بدونها): %s", exc)

    positions = state.get("positions") or {}
    open_rows = [(sym, pos, prices.get(sym)) for sym, pos in sorted(positions.items())]
    body, md = build_report(state, trades, date, open_rows)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"{date}.md").write_text(md, encoding="utf-8")

    telegram = TelegramClient(
        token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    )
    send_chunks(telegram, body)
    log.info("أُرسل تقرير %s (%s حرفًا، %s صفقة اليوم، %s مركز مفتوح)",
             date, len(body), sum(1 for r in trades
                                  if str(r["exit_time_riyadh"]).startswith(date)),
             len(open_rows))

    state.setdefault("day_reports", [])
    if date not in state["day_reports"]:
        state["day_reports"].append(date)
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
