"""Seventh system: isolated, confirmed L/H on 1m Binance Spot; signals only.

No order API, funds, stop or target. Daily P/L is an estimate from confirmation
closes. A durable outbox is saved BEFORE Telegram; on an ambiguous send it is
quarantined rather than risking duplicate recommendations.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import lh1m_messages as messages
from .lh1m_indicator import (LEFT_BARS, RIGHT_BARS, MIN_CLOSED_BARS, MINUTE_MS,
                             confirmed_labels, normalize_klines)
from .lh1m_telegram import SignalDeadlineExpired

LOG = logging.getLogger(__name__)
RIYADH = ZoneInfo("Asia/Riyadh")
CURRENT_SETTINGS = {"left": LEFT_BARS, "right": RIGHT_BARS}
LEGACY_SETTINGS = {"left": 30, "right": 30}


class DurableStoreError(RuntimeError):
    """Fail closed; the worker must exit instead of sending with unsafe state."""


def now_ms() -> int:
    return int(time.time() * 1000)


def day_of(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, RIYADH).date().isoformat()


def empty_state(start_ms: int) -> dict:
    return {"schema": 1, "system": "lh-1m", "started_at_ms": start_ms,
            "last_heartbeat_ms": start_ms, "first_day": day_of(start_ms),
            "indicator_settings": dict(CURRENT_SETTINGS),
            # A fresh system keeps the documented one-minute initial grace.
            "indicator_since_ms": start_ms - MINUTE_MS,
            "cursors": {}, "positions": {}, "events": {}, "daily": {}, "reports": {},
            "coverage_misses": {}}


def _daily(state: dict, day: str) -> dict:
    return state["daily"].setdefault(day, {"entries": 0, "closed": 0,
                                          "unmatched_exits": 0, "wins": 0, "losses": 0,
                                          "breakeven": 0, "net_sum": 0.0})


def _healthy(snapshot: Any, when_ms: int, max_halal_age_ms: int) -> bool:
    return bool(snapshot is not None and snapshot.fresh(when_ms, max_halal_age_ms)
                and snapshot.verdicts.get("USDT") == 0
                and "USDT" not in snapshot.excluded)


class Lh1mBot:
    def __init__(self, market: Any, telegram: Any, store: Any, provider: Any,
                 *, clock: Callable[[], int] = now_ms, max_age_ms: int = 120_000,
                 max_halal_age_ms: int = 4_500_000, fee_side_pct: float = 0.1) -> None:
        if max_age_ms < MINUTE_MS or max_halal_age_ms < MINUTE_MS:
            raise ValueError("invalid freshness threshold")
        if not 0 <= fee_side_pct < 5:
            raise ValueError("invalid fee")
        if getattr(telegram, "muted", False):
            raise ValueError("muted Telegram must never be counted as sent")
        if not callable(getattr(telegram, "send_before", None)):
            raise ValueError("Telegram transport must check a signal deadline before every attempt")
        self.market, self.telegram, self.store, self.provider = market, telegram, store, provider
        self.clock, self.max_age_ms = clock, int(max_age_ms)
        self.max_halal_age_ms, self.fee_side_pct = int(max_halal_age_ms), float(fee_side_pct)
        self.state = store.load()
        if self.state is None:
            self.state = empty_state(self.clock())
            self._save()
        if self.state.get("schema") != 1 or self.state.get("system") != "lh-1m":
            raise ValueError("corrupt/other-system state; refusing reset")
        self.state.setdefault("coverage_misses", {})
        for key in ("cursors", "positions", "events", "daily", "reports", "coverage_misses"):
            if not isinstance(self.state.get(key), dict):
                raise ValueError(f"invalid state: {key}")
        changed = self._migrate_settings()
        for e in self.state["events"].values():
            if e.get("status") == "sending":
                e["status"] = "uncertain"
                changed = True
                LOG.error("Ambiguous prior Telegram send %s; manual resolution required", e.get("id"))
        if changed:
            self._save()

    def _migrate_settings(self) -> bool:
        """Switch 30/30 to 90/90 once, preserving open virtual positions.

        A legacy state has no settings field. Pending 30/30 signals are never
        relabelled as 90/90. Uncertain sends are retained for manual review.
        New 90/90 labels must confirm after the migration, not in the past.
        """
        old = self.state.get("indicator_settings", LEGACY_SETTINGS)
        if old == CURRENT_SETTINGS:
            if not isinstance(self.state.get("indicator_since_ms"), int):
                raise ValueError("missing 90/90 activation time")
            return False
        if old != LEGACY_SETTINGS:
            raise ValueError("unknown prior indicator settings; refusing automatic migration")
        activation_ms = self.clock()
        for pos in self.state["positions"].values():
            if not isinstance(pos, dict):
                raise ValueError("invalid legacy position")
            pos.setdefault("entry_settings", dict(LEGACY_SETTINGS))
            # A missed H under 30/30 must not suppress a new warning under 90/90.
            pos.pop("missed_exit_notified", None)
        for event in self.state["events"].values():
            if (event.get("status") == "pending" and
                    event.get("type") in ("signal", "missed_exit")):
                event["status"] = "superseded_settings_change"
                event.pop("message", None)
                event.pop("effect", None)
        today = day_of(activation_ms)
        if self.state["positions"] or today in self.state["daily"]:
            _daily(self.state, today)["mixed_settings"] = True
        self.state["indicator_settings"] = dict(CURRENT_SETTINGS)
        self.state["indicator_since_ms"] = activation_ms
        self.state["cursors"] = {}  # do not mix confirmation cursors across configurations
        eid = f"settings:{LEFT_BARS}:{RIGHT_BARS}"
        self.state["events"][eid] = {"id": eid, "type": "settings_notice",
                                     "status": "pending", "created_ms": activation_ms}
        return True

    def _save(self) -> None:
        try:
            self.store.save(self.state)
        except Exception as exc:
            raise DurableStoreError("could not persist state; worker must stop before more sends") from exc

    def _allowed(self, snapshot: Any, symbol: str, when_ms: int) -> bool:
        return bool(snapshot and snapshot.allows_entry(symbol, when_ms, self.max_halal_age_ms))

    def _queue_reports(self, current_ms: int) -> None:
        first = date.fromisoformat(self.state["first_day"])
        yesterday = datetime.fromtimestamp(current_ms / 1000, RIYADH).date() - timedelta(days=1)
        # Bound catch-up after a long shutdown; old daily aggregates remain in state.
        day = max(first, yesterday - timedelta(days=30))
        changed = False
        while day <= yesterday:
            key = day.isoformat()
            if key not in self.state["reports"]:
                eid = f"report:{key}"
                self.state["reports"][key] = "queued"
                self.state["events"][eid] = {"id": eid, "type": "report", "day": key,
                                             "status": "pending", "created_ms": current_ms}
                changed = True
            day += timedelta(days=1)
        if changed:
            self._save()

    def _queue_ruling_notice(self, snapshot: Any, current_ms: int) -> None:
        if _healthy(snapshot, current_ms, self.max_halal_age_ms):
            return
        checked = getattr(snapshot, "checked_ms", None)
        marker = str(checked or "unavailable")
        if self.state.get("pause_notice_key") == marker:
            return
        eid = f"rulings-paused:{marker}"
        self.state["pause_notice_key"] = marker
        self.state["events"][eid] = {"id": eid, "type": "notice", "checked_ms": checked,
                                     "status": "pending", "created_ms": current_ms}
        self._save()

    def _queue_gap_notice(self, current_ms: int) -> None:
        last = int(self.state.get("last_heartbeat_ms") or self.state["started_at_ms"])
        if current_ms - last <= 8 * MINUTE_MS or self.state.get("gap_notice_marker") == last:
            return
        eid = f"monitoring-gap:{last}"
        self.state["gap_notice_marker"] = last
        self.state["events"][eid] = {"id": eid, "type": "gap_notice", "last_ms": last,
                                     "status": "pending", "created_ms": current_ms}
        self._save()

    def _queue_blocked_notice(self, snapshot: Any, active: list[str], current_ms: int) -> None:
        if not _healthy(snapshot, current_ms, self.max_halal_age_ms):
            return  # the general stale-file notice already explains the pause
        active_set = set(active)
        blocked = sorted(sym for sym in self.state["positions"]
                         if sym not in active_set or not self._allowed(snapshot, sym, current_ms))
        marker = ",".join(blocked)
        if not marker:
            if self.state.get("blocked_marker"):
                self.state["blocked_marker"] = ""
                self._save()
            return
        if marker == self.state.get("blocked_marker"):
            return
        digest = hashlib.sha256(marker.encode()).hexdigest()[:12]
        eid = f"positions-blocked:{snapshot.checked_ms}:{digest}"
        self.state["blocked_marker"] = marker
        self.state["events"][eid] = {"id": eid, "type": "blocked_notice", "symbols": blocked,
                                     "status": "pending", "created_ms": current_ms}
        self._save()

    def _add_missed_exit(self, sym: str, confirmation_ms: int, current_ms: int) -> bool:
        pos = self.state["positions"].get(sym)
        if (not isinstance(pos, dict) or pos.get("missed_exit_notified")
                or confirmation_ms <= int(pos.get("entry_time_ms", 0))):
            return False
        entry_id = pos.get("entry_id")
        if any(e.get("type") == "missed_exit" and e.get("entry_id") == entry_id
               and e.get("indicator_settings", LEGACY_SETTINGS) == CURRENT_SETTINGS
               for e in self.state["events"].values()):
            return False
        eid = f"missed-H:{LEFT_BARS}x{RIGHT_BARS}:{sym}:{confirmation_ms}:{entry_id}"
        self.state["events"][eid] = {"id": eid, "type": "missed_exit", "symbol": sym,
                                     "entry_id": entry_id, "confirmation_time_ms": confirmation_ms,
                                     "indicator_settings": dict(CURRENT_SETTINGS),
                                     "status": "pending", "created_ms": current_ms}
        return True

    def _mark_missing_coverage(self, sym: str, current_ms: int) -> bool:
        pos = self.state["positions"].get(sym)
        if not isinstance(pos, dict):
            return False
        misses = self.state["coverage_misses"]
        previous = int(misses.get(sym, 0))
        misses[sym] = min(previous + 1, 2)
        changed = misses[sym] != previous
        if misses[sym] < 2 or pos.get("coverage_warned"):
            return changed
        pos["coverage_warned"] = True
        eid = f"coverage-gap:{sym}:{current_ms}:{pos['entry_id']}"
        self.state["events"][eid] = {"id": eid, "type": "coverage_notice", "symbol": sym,
                                     "entry_id": pos["entry_id"], "status": "pending",
                                     "created_ms": current_ms}
        return True

    def _clear_missing_coverage(self, sym: str) -> bool:
        changed = sym in self.state["coverage_misses"]
        self.state["coverage_misses"].pop(sym, None)
        pos = self.state["positions"].get(sym)
        if isinstance(pos, dict) and pos.pop("coverage_warned", False):
            changed = True
        return changed

    def _ingest(self, frames: dict[str, list], symbols: list[str], server_ms: int) -> dict:
        changed, signals, invalid, missed = False, 0, 0, 0
        existing = self.state["events"]
        for sym in sorted(symbols):
            raw = frames.get(sym)
            if raw is None:
                invalid += 1
                changed = self._mark_missing_coverage(sym, server_ms) or changed
                continue
            try:
                bars = normalize_klines(raw, server_ms)
                if len(bars) < MIN_CLOSED_BARS:
                    raise ValueError("not enough closed 1m candles for 90/90")
                if server_ms - (bars[-1].open_ms + MINUTE_MS) > self.max_age_ms:
                    raise ValueError("latest 1m candle is stale")
                last = int(self.state["cursors"].get(sym, -1))
                if last > bars[-1].open_ms:
                    raise ValueError("cursor newer than latest closed bar")
                changed = self._clear_missing_coverage(sym) or changed
                earliest = server_ms - self.max_age_ms - MINUTE_MS
                if sym in self.state["positions"]:
                    # Search the available recent history for a missed H. It is
                    # a warning only, never a backdated or simulated sell.
                    earliest = min(earliest, server_ms - 35 * MINUTE_MS)
                after = max(last, earliest - 1)
                labels = confirmed_labels(bars, left=LEFT_BARS, right=RIGHT_BARS,
                                          after_open_ms=after)
                for label in labels:
                    if label.confirmation_time_ms < self.state["indicator_since_ms"]:
                        continue
                    if server_ms - label.confirmation_time_ms > self.max_age_ms:
                        if label.kind == "H" and self._add_missed_exit(sym, label.confirmation_time_ms, server_ms):
                            missed += 1
                            changed = True
                        continue
                    eid = f"{sym}:{label.kind}:{label.confirmation_open_ms}:{LEFT_BARS}x{RIGHT_BARS}"
                    if eid in existing:
                        continue
                    existing[eid] = {
                        "id": eid, "type": "signal", "symbol": sym, "kind": label.kind,
                        "indicator_settings": dict(CURRENT_SETTINGS),
                        "pivot_open_ms": label.pivot_open_ms,
                        "confirmation_open_ms": label.confirmation_open_ms,
                        "confirmation_time_ms": label.confirmation_time_ms,
                        "confirmation_close": label.confirmation_close,
                        "confirmation_trades": label.confirmation_trades,
                        "confirmation_volume": label.confirmation_volume,
                        "status": "pending", "created_ms": self.clock(),
                    }
                    signals += 1
                    changed = True
                self.state["cursors"][sym] = bars[-1].open_ms
            except (ValueError, TypeError, OverflowError, IndexError) as exc:
                invalid += 1
                changed = self._mark_missing_coverage(sym, server_ms) or changed
                LOG.warning("Skipping invalid 1m candles for %s: %s", sym, exc)
        # Cursors alone need not commit every minute: persisted event IDs + the
        # freshness bound prevent replay after restart without 1,440 commits/day.
        if changed:
            self._save()
        return {"new_labels": signals, "invalid": invalid, "missed_exit_warnings": missed}

    def _plan(self, e: dict, snapshot: Any, when_ms: int) -> tuple[str, dict] | None:
        if e["type"] == "settings_notice":
            return messages.settings_changed(sorted(self.state["positions"])), {"action": "notice"}
        if e["type"] == "gap_notice":
            return messages.gap_notice(int(e["last_ms"]), when_ms), {"action": "notice"}
        if e["type"] == "blocked_notice":
            return messages.blocked_positions(e["symbols"]), {"action": "notice"}
        if e["type"] == "coverage_notice":
            pos = self.state["positions"].get(e["symbol"])
            if not isinstance(pos, dict) or pos.get("entry_id") != e["entry_id"]:
                e["status"] = "superseded"
                return None
            return messages.market_data_missing(e["symbol"]), {"action": "notice"}
        if e["type"] == "missed_exit":
            pos = self.state["positions"].get(e["symbol"])
            if not isinstance(pos, dict) or pos.get("entry_id") != e["entry_id"]:
                e["status"] = "superseded"
                return None
            return (messages.missed_exit(e["symbol"], int(e["confirmation_time_ms"]), when_ms),
                    {"action": "missed_exit", "symbol": e["symbol"], "entry_id": e["entry_id"]})
        if e["type"] == "notice":
            # If the source recovered before delivery, do not send a stale warning.
            if _healthy(snapshot, when_ms, self.max_halal_age_ms):
                e["status"] = "superseded"
                return None
            return messages.rulings_paused(e.get("checked_ms")), {"action": "notice"}
        if e["type"] == "report":
            day = e["day"]
            positions = self.state["positions"]
            blocked = sum(not self._allowed(snapshot, sym, when_ms) for sym in positions)
            text = messages.report(day, self.state["daily"].get(day, {}), len(positions),
                                   blocked, _healthy(snapshot, when_ms, self.max_halal_age_ms),
                                   self.fee_side_pct)
            return text, {"action": "report", "day": day}

        sym, kind = e["symbol"], e["kind"]
        if e.get("indicator_settings", LEGACY_SETTINGS) != CURRENT_SETTINGS:
            e["status"] = "superseded_settings_change"
            return None
        if when_ms - int(e["confirmation_time_ms"]) > self.max_age_ms:
            e["status"] = "expired"
            if kind == "H":
                self._add_missed_exit(sym, int(e["confirmation_time_ms"]), when_ms)
            return None
        if not self._allowed(snapshot, sym, when_ms):
            e["status"] = "blocked_ruling"
            return None
        blockers = {item.get("symbol") for item in self.state["events"].values()
                    if item.get("type") == "signal" and item.get("status") in ("sending", "uncertain")}
        if sym in blockers:
            e["status"] = "blocked_uncertain"
            return None
        pos = self.state["positions"].get(sym)
        if kind == "H":
            if not isinstance(pos, dict):
                # The user asked for every H marker, not only H after a
                # recorded L. Inform without inventing a position or P/L.
                return messages.exit_without_entry(sym, e, when_ms), {
                    "action": "unmatched_exit", "symbol": sym}
            gross = (float(e["confirmation_close"]) / float(pos["entry_price"]) - 1.0) * 100.0
            net = gross - 2 * self.fee_side_pct  # same convention as other systems
            text = messages.exit(sym, e, pos, net, self.fee_side_pct, when_ms)
            return text, {"action": "exit", "symbol": sym, "entry_id": pos["entry_id"],
                          "gross_pct": gross, "net_pct": net}
        if kind != "L":
            raise ValueError("unknown signal kind")
        count = int(pos.get("entry_count", 1)) + 1 if isinstance(pos, dict) else 1
        text = messages.entry(sym, e, when_ms, count)
        new_pos = dict(pos) if isinstance(pos, dict) else {
            "entry_id": e["id"], "entry_price": float(e["confirmation_close"]),
            "entry_time_ms": int(e["confirmation_time_ms"]),
            "entry_settings": dict(CURRENT_SETTINGS),
        }
        new_pos["entry_count"] = count
        return text, {"action": "entry" if count == 1 else "repeat",
                      "symbol": sym, "position_after": new_pos}

    def _apply(self, e: dict, sent_ms: int) -> None:
        effect = e["effect"]
        action = effect["action"]
        if action == "notice":
            return
        if action == "missed_exit":
            pos = self.state["positions"].get(effect["symbol"])
            if not isinstance(pos, dict) or pos.get("entry_id") != effect["entry_id"]:
                raise ValueError("missed exit no longer matches the open position")
            pos["missed_exit_notified"] = True
            return
        if action == "report":
            self.state["reports"][effect["day"]] = "sent"
            return
        daily = _daily(self.state, day_of(sent_ms))
        sym = effect["symbol"]
        if action in ("entry", "repeat"):
            position = dict(effect["position_after"])
            # A manually resolved old send can still create an old-settings
            # position after migration; preserve its true entry provenance.
            position.setdefault("entry_settings", dict(e.get("indicator_settings", LEGACY_SETTINGS)))
            self.state["positions"][sym] = position
            daily["entries"] += 1
            if position["entry_settings"] != CURRENT_SETTINGS:
                daily["mixed_settings"] = True
        elif action == "unmatched_exit":
            daily["unmatched_exits"] = int(daily.get("unmatched_exits", 0)) + 1
        elif action == "exit":
            pos = self.state["positions"].get(sym)
            if not isinstance(pos, dict) or pos.get("entry_id") != effect["entry_id"]:
                raise ValueError("exit no longer matches the original position")
            if pos.get("entry_settings", LEGACY_SETTINGS) != e.get("indicator_settings", LEGACY_SETTINGS):
                daily["mixed_settings"] = True
            del self.state["positions"][sym]
            self.state["coverage_misses"].pop(sym, None)
            net = float(effect["net_pct"])
            daily["closed"] += 1
            daily["wins" if net > 0 else "losses" if net < 0 else "breakeven"] += 1
            daily["net_sum"] = round(float(daily["net_sum"]) + net, 8)
        else:
            raise ValueError("unknown prepared effect")

    def _abandon_signal_before_send(self, e: dict, snapshot: Any) -> None:
        now = self.clock()
        e["status"] = ("blocked_ruling" if not self._allowed(snapshot, e["symbol"], now)
                       else "expired")
        if e.get("kind") == "H":
            self._add_missed_exit(e["symbol"], int(e["confirmation_time_ms"]), now)
        e.pop("message", None)
        e.pop("effect", None)
        self._save()

    def _dispatch(self, snapshot: Any, *, active_symbols: set[str] | None = None,
                  defer_signals: bool = False) -> dict:
        counts = {"sent": 0, "ignored": 0, "uncertain": 0}
        unresolved = [str(e.get("id", "unknown")) for e in self.state["events"].values()
                      if e.get("status") in ("uncertain", "sending")]
        if unresolved:
            # Event IDs contain symbols/timestamps, never credentials. Make
            # recovery possible from a failed workflow log after a restart.
            LOG.error("Telegram delivery unresolved; manual review required for %s",
                      ", ".join(unresolved))
            counts["uncertain"] = len(unresolved)
            return counts
        pending = [e for e in self.state["events"].values() if e.get("status") == "pending"]
        # H before L at the same confirmation time. Reports keep their own time.
        pending.sort(key=lambda e: (int(e.get("confirmation_time_ms", e.get("created_ms", 0))),
                                    0 if e.get("kind") == "H" else 1,
                                    str(e.get("symbol", ""))))
        dirty = False
        for e in pending:
            if e.get("type") == "signal" and defer_signals:
                continue  # first deliver notices; check current Spot universe next
            if (e.get("type") == "signal" and active_symbols is not None
                    and e.get("symbol") not in active_symbols):
                e["status"] = "blocked_market"
                counts["ignored"] += 1
                dirty = True
                continue
            t = self.clock()
            plan = self._plan(e, snapshot, t)
            if plan is None:
                counts["ignored"] += 1
                dirty = True
                continue
            text, effect = plan
            e.update({"status": "sending", "effect": effect,
                      "attempted_ms": t, "message": text})
            if e["type"] == "report":
                self.state["reports"][e["day"]] = "sending"
            self._save()  # durable BEFORE Telegram, so a crash cannot silently duplicate
            dirty = False  # previous non-sends are included in this state commit
            # Persistence itself may have taken long enough for a signal or
            # its halal snapshot to age. Never send after a known expiry.
            if (e["type"] == "signal" and
                    (self.clock() - int(e["confirmation_time_ms"]) > self.max_age_ms or
                     not self._allowed(snapshot, e["symbol"], self.clock()))):
                self._abandon_signal_before_send(e, snapshot)
                counts["ignored"] += 1
                continue
            try:
                if e["type"] == "signal":
                    deadline = min(int(e["confirmation_time_ms"]) + self.max_age_ms,
                                   int(snapshot.checked_ms) + self.max_halal_age_ms)
                    self.telegram.send_before(text, deadline)
                else:
                    self.telegram.send_message(text)
            except SignalDeadlineExpired:
                # The deadline guard raises before the request; an earlier
                # attempt, if any, was explicitly rejected with HTTP 429.
                self._abandon_signal_before_send(e, snapshot)
                counts["ignored"] += 1
                continue
            except Exception as exc:
                # A timeout can mean Telegram accepted the message even though
                # we didn't receive a response; never blindly retry it.
                e["status"] = "uncertain"
                e["error_type"] = type(exc).__name__  # no token/URL in logs or state
                if e["type"] == "report":
                    self.state["reports"][e["day"]] = "uncertain"
                self._save()
                counts["uncertain"] += 1
                LOG.error("Ambiguous Telegram send for %s; inspect chat then resolve", e["id"])
                break
            sent_ms = self.clock()
            try:
                self._apply(e, sent_ms)
                e["status"], e["sent_ms"] = "sent", sent_ms
                e.pop("message", None)  # keep text only for unresolved delivery review
                self._save()  # failure leaves remote 'sending' => review on restart
            except Exception as exc:
                raise DurableStoreError("message may have been delivered; stop for review") from exc
            counts["sent"] += 1
        if dirty:
            self._save()
        return counts

    def resolve(self, event_id: str, was_sent: bool) -> None:
        e = self.state["events"].get(event_id)
        if not e or e.get("status") not in ("uncertain", "sending") or not isinstance(e.get("effect"), dict):
            raise ValueError("event is not an unresolved send")
        if was_sent:
            self._apply(e, int(e["attempted_ms"]))
            e["status"], e["sent_ms"] = "sent", int(e["attempted_ms"])
            e["manually_resolved"] = "sent"
        else:
            e["manually_resolved"] = "not_sent"
            if (e.get("type") in ("signal", "missed_exit") and
                    e.get("indicator_settings", LEGACY_SETTINGS) != CURRENT_SETTINGS):
                e["status"] = "superseded_settings_change"
                e.pop("message", None)
                e.pop("effect", None)
            else:
                e["status"] = "pending"
                if e["type"] == "report":
                    self.state["reports"][e["day"]] = "queued"
        self._save()

    def _prune(self, current_ms: int) -> None:
        # Outstanding/uncertain events stay indefinitely. Completed events are
        # kept for 6h (far longer than the 2m replay window); Git branch history
        # retains previous snapshots. Daily aggregates remain for 120 days.
        old = current_ms - 6 * 3_600_000
        events = self.state["events"]
        for eid, e in list(events.items()):
            if e.get("status") not in ("pending", "sending", "uncertain") and int(e.get("created_ms", 0)) < old:
                del events[eid]
        cutoff = date.fromisoformat(day_of(current_ms)) - timedelta(days=120)
        for d in list(self.state["daily"]):
            if date.fromisoformat(d) < cutoff:
                del self.state["daily"][d]

    def cycle(self) -> dict:
        server_ms = int(self.market.server_time_ms())
        if abs(server_ms - self.clock()) > 30_000:
            raise RuntimeError("local clock differs from Binance server by over 30s")
        snapshot = self.provider.get()
        self._prune(server_ms)
        self._queue_gap_notice(server_ms)
        self._queue_reports(server_ms)
        self._queue_ruling_notice(snapshot, server_ms)
        first = self._dispatch(snapshot, defer_signals=True)
        if first["uncertain"]:
            return {"paused_uncertain": True, "sent": first["sent"],
                    "uncertain": first["uncertain"]}
        active = self.market.active_spot_usdt_symbols()
        self._queue_blocked_notice(snapshot, active, server_ms)
        allowed = [sym for sym in active if self._allowed(snapshot, sym, server_ms)]
        # Only currently verified halal pairs are allowed to emit ANY H/L.
        frames = self.market.candles_for_symbols(allowed) if allowed else {}
        scan = self._ingest(frames, allowed, server_ms)
        second = self._dispatch(snapshot, active_symbols=set(active))
        self._prune(server_ms)
        if not second["uncertain"] and server_ms - int(self.state.get("last_heartbeat_ms") or 0) >= 5 * MINUTE_MS:
            self.state["last_heartbeat_ms"] = server_ms
            self._save()
        return {"indicator_settings": dict(CURRENT_SETTINGS),
                "active_spot_usdt": len(active), "eligible_halal": len(allowed),
                "rulings_fresh": _healthy(snapshot, server_ms, self.max_halal_age_ms),
                "positions": len(self.state["positions"]), **scan,
                "sent": first["sent"] + second["sent"],
                "uncertain": first["uncertain"] + second["uncertain"]}
