#!/usr/bin/env python3
"""Run the isolated L/H 1m recommendation worker. Never places orders."""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import sys
import time
from pathlib import Path

from src.lh1m_bot import DurableStoreError, Lh1mBot
from src.lh1m_github import GitHubFiles, GitHubStateStore, parse_snapshot, HalalProvider
from src.lh1m_indicator import LEFT_BARS, RIGHT_BARS, confirmed_labels, normalize_klines
from src.lh1m_market import SpotMarket
from src.lh1m_telegram import SignalTelegramClient
from src.telegram_client import install_redaction, is_muted

LOG = logging.getLogger("lh1m")


class MemoryStore:
    def __init__(self) -> None:
        self.state = None
    def load(self):
        return copy.deepcopy(self.state)
    def save(self, value):
        self.state = copy.deepcopy(value)


class LocalProvider:
    def get(self):
        try:
            primary = json.loads(Path("data/daily_trend/halal_academy.json").read_text(encoding="utf-8"))
            meta = json.loads(Path("data/daily_trend/halal_academy_meta.json").read_text(encoding="utf-8"))
            return parse_snapshot(primary, meta)
        except Exception as exc:
            LOG.warning("Local halal snapshot unavailable: %s", type(exc).__name__)
            return None


class DryTelegram:
    muted = False
    def send_message(self, text: str) -> None:
        # Deliberately never contacts Telegram. No message body in logs.
        LOG.info("DRY RUN: would send one message (%d characters)", len(text))
    def send_before(self, text: str, deadline_ms: int) -> None:
        self.send_message(text)


def watch(bot: Lh1mBot, seconds: int) -> int:
    """Exit nonzero for persistent failures or an ambiguous Telegram send.

    Stale rulings and an all-pairs market-data outage count as failures. A
    partial outage remains visible in cycle counts/logs but need not stop the
    other pairs. Exiting on ambiguity frees the slot for manual recovery.
    A clean return means the final cycle succeeded, not a real-time SLA.
    """
    seconds = max(0, min(int(seconds), 3_000))
    end = time.monotonic() + seconds
    consecutive_failures = 0
    while True:
        try:
            result = bot.cycle()
            LOG.info("L/H 1m cycle: %s", result)
            if result.get("uncertain") or result.get("paused_uncertain"):
                LOG.error("Unresolved Telegram delivery; exiting for manual review")
                return 2
            eligible = int(result.get("eligible_halal", 0))
            invalid = int(result.get("invalid", 0))
            if result.get("rulings_fresh") is False:
                consecutive_failures += 1
                LOG.warning("Rulings unavailable or stale (%d consecutive cycles)", consecutive_failures)
            elif eligible > 0 and invalid >= eligible:
                consecutive_failures += 1
                LOG.warning("No eligible pair could be evaluated (%d consecutive cycles)", consecutive_failures)
            else:
                consecutive_failures = 0
        except DurableStoreError:
            LOG.error("Durable state failed; exiting immediately to prevent duplicates")
            return 2
        except Exception as exc:
            consecutive_failures += 1
            LOG.warning("Cycle failed safely (%d consecutive): %s",
                        consecutive_failures, type(exc).__name__)
        if consecutive_failures >= 3 or (not seconds and consecutive_failures):
            LOG.error("Monitoring failed; exiting so Actions reports a failure")
            return 2
        if not seconds or time.monotonic() >= end:
            return 2 if consecutive_failures else 0
        # Work after a 1m candle closes. GitHub/network may still delay it.
        wait = max(1.0, (60.0 - time.time() % 60.0) + 2.0)
        time.sleep(min(wait, max(0.0, end - time.monotonic())))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=int, default=0,
                    help="watch duration; 0 performs one cycle")
    ap.add_argument("--dry-run", action="store_true", help="memory-only, no Telegram/GitHub writes")
    ap.add_argument("--inspect", metavar="PAIR", help="read-only indicator inspection on one active Spot pair")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    install_redaction()
    market = SpotMarket(workers=int(os.getenv("LH1_MAX_WORKERS", "10")),
                        timeout=int(os.getenv("REQUEST_TIMEOUT", "8")))
    try:
        if args.inspect:
            symbol = args.inspect.upper().strip()
            if symbol not in market.active_spot_usdt_symbols():
                raise ValueError("not an active Binance Spot USDT pair")
            t = market.server_time_ms()
            raw = market.candles_for_symbols([symbol]).get(symbol, [])
            bars = normalize_klines(raw, t)
            found = confirmed_labels(bars)
            print(json.dumps({"pair": symbol, "closed_1m_candles": len(bars),
                              "indicator_settings": {"left": LEFT_BARS, "right": RIGHT_BARS},
                              "confirmed_labels_in_retrieved_window": len(found),
                              "latest": vars(found[-1]) if found else None,
                              "mode": "read-only inspection; no recommendation or message"},
                             ensure_ascii=False))
            return 0
        if args.dry_run:
            bot = Lh1mBot(market, DryTelegram(), MemoryStore(), LocalProvider())
        else:
            if is_muted():
                raise ValueError("TELEGRAM_MUTE is enabled: refusing to claim live messages were sent")
            token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
            github_token = os.getenv("GITHUB_TOKEN", "").strip()
            repo = os.getenv("GITHUB_REPOSITORY", "").strip()
            if not all((token, chat, github_token, repo)):
                raise ValueError("missing GitHub or Telegram configuration")
            github = GitHubFiles(repo, github_token)
            bot = Lh1mBot(market, SignalTelegramClient(token, chat), GitHubStateStore(github),
                          HalalProvider(github))
            event = os.getenv("LH1_RESOLVE_EVENT", "").strip()
            decision = os.getenv("LH1_RESOLVE_DECISION", "none").strip()
            if event or decision != "none":
                if not event or decision not in ("sent", "not_sent"):
                    raise ValueError("resolution requires event ID and sent/not_sent decision")
                bot.resolve(event, was_sent=(decision == "sent"))
                LOG.info("Resolved delivery status for %s as %s", event, decision)
        return watch(bot, args.seconds)
    finally:
        market.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Don't print a traceback: Telegram exceptions may contain secret URLs.
        print(f"L/H worker stopped: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(2)
