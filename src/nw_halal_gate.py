"""Fail-closed Academy-only check for new NW entry recommendations.

On GitHub Actions the primary file is fetched from the repository's current
main immediately before each new/repeat entry. Local runs reread the configured
primary file on each check. No supplemental rulings or cached verdicts can
permit a recommendation. Network or validation failures deny new entries;
exit processing remains independent of this checker.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)
PRIMARY_PATH = 'data/daily_trend/halal_academy.json'
_REPO_RE = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
_ACADEMY_SYMBOL_RE = re.compile(r'^[A-Z0-9][A-Z0-9._-]{0,39}$')
_SYMBOL_RE = re.compile(r'^[A-Z0-9]+USDT$')
_SHA_RE = re.compile(r'^[0-9a-f]{40}$')
_MAX_RESPONSE = 2_000_000


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str  # halal, haram, doubtful, unknown, unavailable, invalid_symbol


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _parse_primary(raw: bytes) -> dict[str, int | None]:
    data: Any = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    if not isinstance(data, dict) or not data:
        raise ValueError('empty or invalid primary')
    for key, value in data.items():
        if (not isinstance(key, str) or not _ACADEMY_SYMBOL_RE.fullmatch(key)
                or (value is not None and (type(value) is not int or value not in (0, 1, 2)))):
            raise ValueError('invalid primary entry')
    return data


def _blob_sha(raw: bytes) -> str:
    return hashlib.sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_current_github_file(repository: str, token: str) -> bytes:
    """Read only the fixed Academy path at main, never logging credentials."""
    if not _REPO_RE.fullmatch(repository or '') or not token or any(c.isspace() for c in token):
        raise ValueError('missing GitHub configuration')
    url = f'https://api.github.com/repos/{repository}/contents/{PRIMARY_PATH}?ref=main'
    headers = {
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'nw-academy-entry-gate',
        'Cache-Control': 'no-cache, no-store',
    }
    request = urllib.request.Request(url, headers=headers, method='GET')
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=8) as response:
            if response.status != 200:
                raise ValueError('unexpected status')
            body = response.read(_MAX_RESPONSE + 1)
    except (urllib.error.URLError, OSError, TimeoutError):
        raise ValueError('GitHub read failed') from None
    if len(body) > _MAX_RESPONSE:
        raise ValueError('response too large')
    try:
        payload = json.loads(body.decode('utf-8'), object_pairs_hook=_unique_object)
        if (payload.get('type') != 'file' or payload.get('encoding') != 'base64'
                or payload.get('path') != PRIMARY_PATH):
            raise ValueError
        sha = payload['sha']
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            raise ValueError
        data = base64.b64decode(''.join(payload['content'].split()), validate=True)
        if _blob_sha(data) != sha:
            raise ValueError
        return data
    except (AttributeError, ValueError, KeyError, TypeError, UnicodeError, binascii.Error):
        raise ValueError('invalid GitHub contents') from None


class AcademyPrimaryGate:
    def __init__(self, local_path: str, *, github_actions: bool = False,
                 repository: str = '', token: str = ''):
        self.local_path = Path(local_path)
        self.github_actions = github_actions
        self.repository = repository
        self._token = token
        self._unavailable_this_cycle = False

    @classmethod
    def from_environment(cls, local_path: str) -> 'AcademyPrimaryGate':
        return cls(
            local_path,
            github_actions=os.getenv('GITHUB_ACTIONS', '').strip().lower() == 'true',
            repository=os.getenv('GITHUB_REPOSITORY', '').strip(),
            token=os.getenv('NW_HALAL_GITHUB_TOKEN', '').strip(),
        )

    def begin_cycle(self) -> None:
        # A new cycle may recover from a transient API or filesystem failure.
        self._unavailable_this_cycle = False

    def __call__(self, symbol: str) -> Decision:
        if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
            return Decision(False, 'invalid_symbol')
        if self._unavailable_this_cycle:
            return Decision(False, 'unavailable')
        try:
            raw = (_read_current_github_file(self.repository, self._token)
                   if self.github_actions else self.local_path.read_bytes())
            rulings = _parse_primary(raw)
        except Exception:
            # لا نسجّل تفاصيل الاستثناء: قد تتضمن ترويسات الاعتماد. التعطّل
            # يُحجب لبقية الدورة حتى لا يؤخر محاولات متكررة تنبيهات الخروج.
            self._unavailable_this_cycle = True
            _LOG.warning('تعذرت قراءة ملف أحكام الأكاديمية؛ تُحجب التوصيات الجديدة لهذه الدورة')
            return Decision(False, 'unavailable')
        value = rulings.get(symbol[:-4])
        if value == 0:
            return Decision(True, 'halal')
        if value == 1:
            return Decision(False, 'haram')
        if value == 2:
            return Decision(False, 'doubtful')
        return Decision(False, 'unknown')
