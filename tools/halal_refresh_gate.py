#!/usr/bin/env python3
"""Decide whether an automatic Academy refresh is due, using local files only.

The refresh workflow has a native 30-minute schedule and a backup heartbeat.
This gate prevents each heartbeat from fetching the site when the last checked
snapshot is fresh and internally consistent. Manual dispatch always proceeds.
Unparseable, missing, inconsistent or future-dated metadata fails open *to an
update attempt*; it never edits or invents a ruling itself.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PRIMARY = Path("data/daily_trend/halal_academy.json")
METADATA = Path("data/daily_trend/halal_academy_meta.json")
REFRESH_INTERVAL_SECONDS = 30 * 60


@dataclass(frozen=True)
class Decision:
    refresh: bool
    reason: str
    age_seconds: float | None = None


def primary_digest(primary: dict[str, int]) -> str:
    """Match the updater's canonical semantic digest, not the file byte hash."""
    raw = json.dumps(
        primary, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def decide(
    primary: Any,
    metadata: Any,
    *,
    event_name: str,
    now: datetime,
) -> Decision:
    """Return a refresh decision without changing the supplied data."""
    if event_name == "workflow_dispatch":
        return Decision(True, "manual_dispatch")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not isinstance(primary, dict) or not primary or any(
        not isinstance(k, str) or not k
        or isinstance(v, bool) or not isinstance(v, int) or v not in (0, 1, 2)
        for k, v in primary.items()
    ):
        return Decision(True, "primary_missing_or_invalid")
    if not isinstance(metadata, dict):
        return Decision(True, "metadata_missing_or_invalid")
    recorded = metadata.get("primary_sha256")
    try:
        expected = primary_digest(primary)
    except (TypeError, ValueError):
        return Decision(True, "primary_missing_or_invalid")
    if not isinstance(recorded, str) or recorded != expected:
        return Decision(True, "digest_missing_or_mismatch")
    raw_time = metadata.get("last_check_utc")
    if not isinstance(raw_time, str) or not raw_time.strip():
        return Decision(True, "last_check_missing_or_invalid")
    try:
        last_check = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        if last_check.tzinfo is None or last_check.utcoffset() is None:
            return Decision(True, "last_check_missing_or_invalid")
        age = (now.astimezone(timezone.utc) - last_check.astimezone(timezone.utc)).total_seconds()
    except (ValueError, OverflowError):
        return Decision(True, "last_check_missing_or_invalid")
    if age < 0:
        return Decision(True, "last_check_in_future", age)
    if age >= REFRESH_INTERVAL_SECONDS:
        return Decision(True, "refresh_due", age)
    return Decision(False, "snapshot_fresh", age)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary", type=Path, default=PRIMARY)
    parser.add_argument("--metadata", type=Path, default=METADATA)
    args = parser.parse_args()
    result = decide(
        load_json(args.primary), load_json(args.metadata),
        event_name=os.environ.get("GITHUB_EVENT_NAME", ""),
        now=datetime.now(timezone.utc),
    )
    value = "true" if result.refresh else "false"
    age = "unknown" if result.age_seconds is None else f"{result.age_seconds:.0f}"
    print(f"Academy refresh: {value}; reason={result.reason}; age_seconds={age}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"refresh={value}\n")
            stream.write(f"reason={result.reason}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
