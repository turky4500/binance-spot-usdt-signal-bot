"""Isolated durable state and read-only ruling snapshots via GitHub Contents API.

Only the dedicated state branch is written by this module. Main and the other
systems' state/ledgers are never modified. The workflow's concurrency group
must ensure a single writer; optimistic SHA checks detect external writers.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests

LOG = logging.getLogger(__name__)
PRIMARY_PATH = "data/daily_trend/halal_academy.json"
META_PATH = "data/daily_trend/halal_academy_meta.json"
STATE_BRANCH = "indicator-lh-1m-state"
STATE_PATH = "data/indicator_lh_1m/state.json"


class StateConflict(RuntimeError):
    pass


def _serialized(state: dict) -> bytes:
    return (json.dumps(state, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


class GitHubFiles:
    def __init__(self, repository: str, token: str, timeout: int = 25,
                 session: requests.Session | None = None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("invalid GITHUB_REPOSITORY")
        if not token:
            raise ValueError("missing GITHUB_TOKEN")
        self.base = f"https://api.github.com/repos/{repository}"
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}",
                                     "Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28",
                                     "User-Agent": "isolated-lh1m-signal-bot"})

    def ref(self, branch: str, missing_ok: bool = False) -> str | None:
        r = self.session.get(f"{self.base}/git/ref/heads/{quote(branch, safe='')}",
                             timeout=self.timeout)
        if r.status_code == 404 and missing_ok:
            return None
        r.raise_for_status()
        return str(r.json()["object"]["sha"])

    def create_state_branch(self) -> None:
        if self.ref(STATE_BRANCH, missing_ok=True):
            return
        sha = self.ref("main")
        r = self.session.post(f"{self.base}/git/refs",
                              json={"ref": f"refs/heads/{STATE_BRANCH}", "sha": sha},
                              timeout=self.timeout)
        # A second concurrent initializer may have created it; the workflow
        # concurrency group is still required to prevent two active writers.
        if r.status_code == 422 and self.ref(STATE_BRANCH, missing_ok=True):
            return
        r.raise_for_status()

    def get(self, path: str, ref: str, missing_ok: bool = False) -> tuple[bytes, str] | None:
        url = f"{self.base}/contents/{quote(path, safe='/')}"
        r = self.session.get(url, params={"ref": ref}, timeout=self.timeout)
        if r.status_code == 404 and missing_ok:
            return None
        r.raise_for_status()
        payload = r.json()
        if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
            raise RuntimeError(f"unexpected GitHub content response: {path}")
        return base64.b64decode(payload["content"], validate=False), str(payload["sha"])

    def put(self, path: str, branch: str, content: bytes, sha: str | None) -> requests.Response:
        doc: dict[str, str] = {
            "message": "chore(lh1m): durable signal state [skip ci]",
            "content": base64.b64encode(content).decode("ascii"),
            "branch": branch,
        }
        if sha:
            doc["sha"] = sha
        return self.session.put(f"{self.base}/contents/{quote(path, safe='/')}",
                                json=doc, timeout=self.timeout)


class GitHubStateStore:
    """Compare-and-swap store. `save` must succeed before any Telegram send."""
    def __init__(self, github: GitHubFiles) -> None:
        self.github = github
        self.sha: str | None = None
        self.last_content: bytes | None = None

    def load(self) -> dict | None:
        self.github.create_state_branch()
        got = self.github.get(STATE_PATH, STATE_BRANCH, missing_ok=True)
        if got is None:
            return None
        content, self.sha = got
        self.last_content = content
        state = json.loads(content)
        if not isinstance(state, dict) or state.get("schema") != 1 or state.get("system") != "lh-1m":
            raise RuntimeError("unrecognised/corrupt LH state; refusing to reset")
        return state

    def save(self, state: dict) -> None:
        content = _serialized(state)
        if len(content) > 900_000:
            raise RuntimeError("isolated state exceeds safe Contents API size; stop and review")
        if self.last_content == content:
            return
        for attempt in range(3):
            try:
                r = self.github.put(STATE_PATH, STATE_BRANCH, content, self.sha)
                if r.status_code in (200, 201):
                    self.sha = str(r.json()["content"]["sha"])
                    self.last_content = content
                    return
                if r.status_code == 409:
                    # A definite rejected CAS cannot be treated as our write,
                    # even if another writer stored identical bytes. Otherwise
                    # both workers could believe they own an outbox event.
                    raise StateConflict("state CAS rejected; another writer may be active")
                if r.status_code not in (500, 502, 503, 504):
                    r.raise_for_status()
            except (requests.Timeout, requests.ConnectionError):
                pass
            # A timeout might have happened *after* GitHub accepted the write.
            # Read back before retrying. Never overwrite a competing change.
            latest = self.github.get(STATE_PATH, STATE_BRANCH, missing_ok=True)
            latest_content, latest_sha = latest if latest else (None, None)
            if latest_content == content:
                self.sha, self.last_content = latest_sha, content
                return
            if latest_sha != self.sha:
                raise StateConflict("state changed by another writer; stopping before send")
            time.sleep(1 + attempt)
        raise RuntimeError("GitHub state write failed; no further messages will be sent")


@dataclass(frozen=True)
class HalalSnapshot:
    verdicts: dict[str, int]
    excluded: frozenset[str]
    checked_ms: int

    def fresh(self, now_ms: int, max_age_ms: int = 4_500_000) -> bool:
        age = int(now_ms) - self.checked_ms
        return -300_000 <= age <= max_age_ms

    def allows_entry(self, symbol: str, now_ms: int, max_age_ms: int = 4_500_000) -> bool:
        if (not self.fresh(now_ms, max_age_ms) or self.verdicts.get("USDT") != 0
                or "USDT" in self.excluded):
            return False
        if not symbol.endswith("USDT") or symbol == "USDT":
            return False
        base = symbol[:-4]
        return base not in self.excluded and self.verdicts.get(base) == 0


def primary_digest(verdicts: dict[str, int]) -> str:
    data = json.dumps(verdicts, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def parse_snapshot(primary: Any, meta: Any, *, minimum_symbols: int = 700) -> HalalSnapshot:
    if not isinstance(primary, dict) or len(primary) < minimum_symbols or not isinstance(meta, dict):
        raise ValueError("incomplete halal primary or metadata")
    if meta.get("source") != "btcacademy.online":
        raise ValueError("unexpected halal source")
    if any(not isinstance(k, str) or type(v) is not int or v not in (0, 1, 2)
           for k, v in primary.items()):
        raise ValueError("invalid halal verdicts")
    raw_time = str(meta.get("last_check_utc") or "")
    parsed = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("missing timezone in last_check_utc")
    checked_ms = int(parsed.astimezone(timezone.utc).timestamp() * 1000)
    if "unverified_symbols" not in meta:
        raise ValueError("halal metadata lacks per-symbol verification; waiting for updated refresh")
    if meta.get("primary_sha256") != primary_digest(primary):
        raise ValueError("halal primary/metadata digest mismatch; possible partial update")
    excluded: set[str] = set()
    for key in ("unknown", "ambiguous", "unclear_articles", "unverified_symbols"):
        value = meta.get(key, [])
        if not isinstance(value, list):
            raise ValueError(f"invalid metadata list: {key}")
        excluded.update(str(x).upper() for x in value)
    return HalalSnapshot({k.upper(): v for k, v in primary.items()}, frozenset(excluded), checked_ms)


class HalalProvider:
    """Read both files at ONE main commit; invalidate the snapshot on any read error."""
    def __init__(self, github: GitHubFiles, refresh_seconds: int = 60) -> None:
        self.github = github
        self.refresh_seconds = refresh_seconds
        self.last_attempt = 0.0
        self.snapshot: HalalSnapshot | None = None

    def get(self, force: bool = False) -> HalalSnapshot | None:
        now = time.monotonic()
        if not force and self.snapshot is not None and now - self.last_attempt < self.refresh_seconds:
            return self.snapshot
        self.last_attempt = now
        try:
            sha = self.github.ref("main")
            primary = self.github.get(PRIMARY_PATH, str(sha))
            meta = self.github.get(META_PATH, str(sha))
            if primary is None or meta is None:
                raise RuntimeError("missing primary or metadata")
            self.snapshot = parse_snapshot(json.loads(primary[0]), json.loads(meta[0]))
        except Exception as exc:
            # Even a previously fresh snapshot may now have a changed ruling;
            # do not keep it after a failed or inconsistent refresh.
            self.snapshot = None
            LOG.warning("Cannot refresh halal snapshot; L/H paused: %s", type(exc).__name__)
        return self.snapshot
