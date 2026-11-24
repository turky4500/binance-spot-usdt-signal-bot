#!/usr/bin/env python3
"""Report changes to the Academy file, once per Riyadh day, via Telegram.

The only input is data/daily_trend/halal_academy.json. The baseline is the
snapshot of the last fully acknowledged report (or the activation snapshot
for the first report). A separate GitHub state file is the durable send
journal. No trading state, Binance data, or Telegram secrets are written.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

PRIMARY = 'data/daily_trend/halal_academy.json'
META = 'data/daily_trend/halal_academy_meta.json'
STATE = 'data/daily_trend/halal_academy_report_state.json'
RIYADH = ZoneInfo('Asia/Riyadh')
LABELS = {0: 'حلال', 1: 'حرام', 2: 'مشبوه', None: 'بدون حكم'}
SCHEMA = 1
MAX_CHUNK_BYTES = 3200
MAX_HTTP_BYTES = 2_000_000
TOKEN_PATH_RE = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
SHA_RE = re.compile(r'^[0-9a-f]{40}$')
SYMBOL_RE = re.compile(r'^[A-Z0-9][A-Z0-9._-]{0,39}$')


class ReportError(Exception):
    def __init__(self, stage: str, code: int | str):
        super().__init__(stage)
        self.stage, self.code = stage, code


class DeliveryRejected(Exception):
    """Telegram positively rejected the request; a later retry is safe."""
    def __init__(self, code: int, retry_seconds: int = 3600):
        super().__init__('rejected')
        self.code = code
        self.retry_seconds = max(60, min(int(retry_seconds), 3600))


class DeliveryUncertain(Exception):
    """Request might have reached Telegram; do not retry the same part blindly."""
    def __init__(self, code: str):
        super().__init__('uncertain')
        self.code = code


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def parse_json(raw: bytes, stage: str) -> Any:
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, TypeError):
        raise ReportError(stage, 'invalid_json') from None


def validate_primary(value: Any) -> dict[str, int | None]:
    if not isinstance(value, dict) or not value:
        raise ReportError('primary', 'empty_or_invalid')
    for symbol, verdict in value.items():
        if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
            raise ReportError('primary', 'invalid_symbol')
        if verdict is not None and (type(verdict) is not int or verdict not in (0, 1, 2)):
            raise ReportError('primary', 'unsupported_verdict')
    return dict(value)


def snapshot_digest(snapshot: dict[str, int | None]) -> str:
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _day(value: Any, stage: str = 'state') -> date:
    try:
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise ReportError(stage, 'invalid_date') from None


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def initial_state(snapshot: dict[str, int | None], today: date) -> dict:
    return {
        'schema': SCHEMA,
        'last_covered_day': today.isoformat(),
        'baseline_kind': 'activation',
        'snapshot': copy.deepcopy(snapshot),
        'snapshot_sha256': snapshot_digest(snapshot),
        'pending': None,
    }


def validate_state(value: Any) -> dict:
    if not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != SCHEMA:
        raise ReportError('state', 'invalid_schema')
    covered = _day(value.get('last_covered_day'))
    if value.get('baseline_kind') not in ('activation', 'report'):
        raise ReportError('state', 'invalid_baseline_kind')
    snapshot = validate_primary(value.get('snapshot'))
    if value.get('snapshot_sha256') != snapshot_digest(snapshot):
        raise ReportError('state', 'baseline_digest_mismatch')
    pending = value.get('pending')
    if pending is not None:
        if not isinstance(pending, dict) or _day(pending.get('day')) <= covered:
            raise ReportError('state', 'invalid_pending_day')
        target = validate_primary(pending.get('snapshot'))
        if pending.get('snapshot_sha256') != snapshot_digest(target):
            raise ReportError('state', 'pending_digest_mismatch')
        if pending.get('phase') not in ('ready', 'sending', 'uncertain'):
            raise ReportError('state', 'invalid_pending_phase')
        chunks, index = pending.get('chunks'), pending.get('next_chunk')
        if (not isinstance(chunks, list) or not 1 <= len(chunks) <= 1000
                or any(not isinstance(x, str) or not x or len(x.encode('utf-8')) > 4096 for x in chunks)
                or type(index) is not int or not 0 <= index < len(chunks)):
            raise ReportError('state', 'invalid_pending_chunks')
        ids = pending.get('message_ids')
        if not isinstance(ids, list) or len(ids) != index or any(type(x) is not int or x <= 0 for x in ids):
            raise ReportError('state', 'invalid_pending_progress')
        if pending.get('retry_after_utc') is not None and _utc(pending['retry_after_utc']) is None:
            raise ReportError('state', 'invalid_retry_time')
    return copy.deepcopy(value)


def diff_snapshots(old: dict[str, int | None], new: dict[str, int | None]) -> tuple[list, list, list]:
    added = [(s, new[s]) for s in sorted(new.keys() - old.keys())]
    removed = [(s, old[s]) for s in sorted(old.keys() - new.keys())]
    changed = [(s, old[s], new[s]) for s in sorted(old.keys() & new.keys()) if old[s] != new[s]]
    return added, removed, changed


def _source_line(meta: Any, snapshot: dict[str, int | None], now: datetime) -> str:
    if not isinstance(meta, dict) or meta.get('primary_sha256') != snapshot_digest(snapshot):
        return '⚠️ آخر فحص للمصدر: غير مؤكد (البيانات الوصفية غير متطابقة أو مفقودة).'
    checked = _utc(meta.get('last_check_utc'))
    if checked is None:
        return '⚠️ آخر فحص للمصدر: غير متاح.'
    if checked > now.astimezone(timezone.utc) + timedelta(minutes=2):
        return '⚠️ آخر فحص للمصدر: التاريخ المسجل مستقبلي؛ يلزم التحقق.'
    return '🕒 آخر فحص للمصدر: ' + checked.astimezone(RIYADH).strftime('%Y-%m-%d %H:%M:%S') + ' الرياض'


def _split_lines(lines: list[str]) -> list[str]:
    groups: list[list[str]] = [[]]
    size = 0
    for line in lines:
        encoded = (line + '\n').encode('utf-8')
        if len(encoded) > MAX_CHUNK_BYTES:
            raise ReportError('format', 'line_too_long')
        if groups[-1] and size + len(encoded) > MAX_CHUNK_BYTES:
            groups.append([])
            size = 0
        groups[-1].append(line)
        size += len(encoded)
    count = len(groups)
    out = []
    for index, group in enumerate(groups, 1):
        prefix = f'📊 تقرير أحكام الأكاديمية — الجزء {index}/{count}\n' if count > 1 else ''
        text = prefix + '\n'.join(group).strip()
        if not text or len(text.encode('utf-8')) > 4096:
            raise ReportError('format', 'chunk_too_long')
        out.append(text)
    return out


def format_report(snapshot: dict[str, int | None], old: dict[str, int | None],
                  baseline_day: str, baseline_kind: str, report_day: date,
                  meta: Any, now: datetime, uncertain_day: str | None = None) -> list[str]:
    added, removed, changed = diff_snapshots(old, snapshot)
    counts = {key: sum(1 for v in snapshot.values() if v == key) for key in (0, 1, 2, None)}
    label = 'النسخة عند تفعيل التقرير' if baseline_kind == 'activation' else 'آخر تقرير'
    lines = [
        '📊 تقرير ملف أحكام الأكاديمية',
        f'📅 التاريخ: {report_day.isoformat()} — توقيت الرياض',
        f'📁 إجمالي العملات في الملف: {len(snapshot)}',
        f'✅ حلال: {counts[0]}',
        f'⛔ حرام: {counts[1]}',
        f'ℹ️ بدون حكم: {counts[None]}',
        f'⚠️ مشبوه: {counts[2]}',
        _source_line(meta, snapshot, now),
        '',
        f'🔄 الفروق الصافية مقارنةً مع {label} ({baseline_day}):',
    ]
    if uncertain_day:
        lines.append(f'⚠️ لم يتأكد اكتمال تقرير {uncertain_day}؛ المقارنة مع آخر نسخة محفوظة مؤكدة.')
    if not (added or removed or changed):
        lines.append('لا توجد تغييرات في الملف.')
    else:
        lines.append(f'➕ الإضافات: {len(added)}')
        lines.extend(f'• {symbol}: {LABELS[verdict]}' for symbol, verdict in added)
        lines.append(f'➖ المحذوفات: {len(removed)}')
        lines.extend(f'• {symbol}: {LABELS[verdict]}' for symbol, verdict in removed)
        lines.append(f'🔄 تغيّر الحكم: {len(changed)}')
        lines.extend(f'• {symbol}: من {LABELS[before]} إلى {LABELS[after]}'
                     for symbol, before, after in changed)
    return _split_lines(lines)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http(method: str, url: str, headers: dict, payload: bytes | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=payload, method=method, headers=headers)
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            body = response.read(MAX_HTTP_BYTES + 1)
            status = response.status
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read(MAX_HTTP_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise OSError('network_failure') from None
    if len(body) > MAX_HTTP_BYTES:
        raise OSError('response_too_large')
    return int(status), body


def _blob_sha(raw: bytes) -> str:
    return hashlib.sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()


class GitHubRepo:
    def __init__(self, repo: str, token: str):
        if not TOKEN_PATH_RE.fullmatch(repo or '') or not token:
            raise ReportError('github_config', 'missing_or_invalid')
        self.base = 'https://api.github.com/repos/' + repo
        self.headers = {
            'Authorization': 'Bearer ' + token,
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'halal-academy-daily-report',
            'Content-Type': 'application/json',
        }

    def _request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, bytes]:
        try:
            raw = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
            return _http(method, self.base + path, self.headers, raw)
        except OSError:
            raise ReportError('github_network', 'unavailable') from None

    def head(self) -> str:
        status, body = self._request('GET', '/git/ref/heads/main')
        if status != 200:
            raise ReportError('github_head', status)
        obj = parse_json(body, 'github_head')
        sha = (obj.get('object') or {}).get('sha') if isinstance(obj, dict) else None
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            raise ReportError('github_head', 'invalid_sha')
        return sha

    def read_file(self, path: str, ref: str) -> tuple[bytes | None, str | None]:
        if path not in (PRIMARY, META, STATE) or not SHA_RE.fullmatch(ref):
            raise ReportError('github_read', 'invalid_path_or_ref')
        encoded_path = '/'.join(urllib.parse.quote(part, safe='') for part in path.split('/'))
        status, body = self._request('GET', f'/contents/{encoded_path}?ref={ref}')
        if status == 404:
            return None, None
        if status != 200:
            raise ReportError('github_read', status)
        obj = parse_json(body, 'github_read')
        try:
            if obj.get('type') != 'file' or obj.get('encoding') != 'base64':
                raise ValueError
            sha = obj['sha']
            if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
                raise ValueError
            compact = ''.join(obj['content'].split())
            raw = base64.b64decode(compact, validate=True)
            if _blob_sha(raw) != sha:
                raise ValueError
            return raw, sha
        except (AttributeError, KeyError, TypeError, ValueError, binascii.Error):
            raise ReportError('github_read', 'invalid_file') from None

    def save_state(self, state: dict, old_sha: str | None) -> str:
        raw = (json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
        body = {
            'message': 'chore: update halal daily report state [skip ci]',
            'content': base64.b64encode(raw).decode('ascii'),
            'branch': 'main',
        }
        if old_sha is not None:
            if not SHA_RE.fullmatch(old_sha):
                raise ReportError('github_write', 'invalid_previous_sha')
            body['sha'] = old_sha
        path = '/'.join(urllib.parse.quote(part, safe='') for part in STATE.split('/'))
        status, output = self._request('PUT', '/contents/' + path, body)
        if status not in (200, 201):
            raise ReportError('github_write', status)
        obj = parse_json(output, 'github_write')
        sha = (obj.get('content') or {}).get('sha') if isinstance(obj, dict) else None
        if sha != _blob_sha(raw):
            raise ReportError('github_write', 'content_sha_mismatch')
        return sha


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token.strip(), chat_id.strip()

    def available(self) -> bool:
        return bool(self.token and self.chat_id and not any(x.isspace() for x in self.token))

    def check_chat(self) -> None:
        """Read-only credential/chat check; deliberately sends no message."""
        payload = json.dumps({'chat_id': self.chat_id}).encode('utf-8')
        headers = {'Content-Type': 'application/json', 'User-Agent': 'halal-academy-daily-report'}
        try:
            status, body = _http('POST', 'https://api.telegram.org/bot' + self.token + '/getChat',
                                 headers, payload)
        except OSError:
            raise ReportError('telegram_chat_check', 'network') from None
        if status != 200:
            raise ReportError('telegram_chat_check', status)
        try:
            obj = json.loads(body.decode('utf-8'))
            result = obj.get('result') if isinstance(obj, dict) else None
            if obj.get('ok') is not True or not isinstance(result, dict) or type(result.get('id')) is not int:
                raise ValueError
        except (ValueError, UnicodeError, AttributeError):
            raise ReportError('telegram_chat_check', 'invalid_response') from None

    def send(self, text: str) -> int:
        payload = json.dumps({'chat_id': self.chat_id, 'text': text,
                              'disable_web_page_preview': True}, ensure_ascii=False).encode('utf-8')
        headers = {'Content-Type': 'application/json', 'User-Agent': 'halal-academy-daily-report'}
        try:
            status, body = _http('POST', 'https://api.telegram.org/bot' + self.token + '/sendMessage',
                                 headers, payload)
        except OSError:
            raise DeliveryUncertain('network') from None
        try:
            obj = json.loads(body.decode('utf-8'))
        except (ValueError, UnicodeError):
            raise DeliveryUncertain('invalid_response') from None
        if status == 200 and isinstance(obj, dict) and obj.get('ok') is True:
            result = obj.get('result') or {}
            message_id = result.get('message_id') if isinstance(result, dict) else None
            if type(message_id) is int and message_id > 0:
                return message_id
            raise DeliveryUncertain('invalid_message_id')
        if 400 <= status < 500 and isinstance(obj, dict) and obj.get('ok') is False:
            params = obj.get('parameters')
            default_retry = 900 if status == 429 else 3600
            retry = params.get('retry_after', default_retry) if isinstance(params, dict) else default_retry
            try:
                retry = int(retry)
            except (ValueError, TypeError):
                retry = default_retry
            raise DeliveryRejected(status, retry)
        raise DeliveryUncertain('http_' + str(status))


class Reporter:
    def __init__(self, repo, telegram, sleep=time.sleep):
        self.repo, self.telegram, self.sleep = repo, telegram, sleep

    def _read_source(self, head: str) -> tuple[dict[str, int | None], Any]:
        raw, _ = self.repo.read_file(PRIMARY, head)
        if raw is None:
            raise ReportError('primary', 'missing')
        snapshot = validate_primary(parse_json(raw, 'primary'))
        metadata_raw, _ = self.repo.read_file(META, head)
        try:
            metadata = parse_json(metadata_raw, 'metadata') if metadata_raw is not None else None
        except ReportError:
            metadata = None
        return snapshot, metadata

    def run(self, now: datetime, bootstrap_only: bool = False) -> str:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ReportError('clock', 'timezone_missing')
        now = now.astimezone(timezone.utc)
        today = now.astimezone(RIYADH).date()
        head = self.repo.head()
        raw, state_sha = self.repo.read_file(STATE, head)
        if raw is None:
            # Missing state is ambiguous: it may be first activation or an
            # accidental deletion. Never silently erase the comparison baseline.
            if not bootstrap_only:
                raise ReportError('state', 'bootstrap_required')
            snapshot, _ = self._read_source(head)
            self.repo.save_state(initial_state(snapshot, today), None)
            return 'baseline_initialized_no_message'
        state = validate_state(parse_json(raw, 'state'))
        if bootstrap_only:
            return 'baseline_already_exists_no_message'
        covered = _day(state['last_covered_day'])
        if today < covered:
            raise ReportError('clock', 'before_baseline')
        pending = state.get('pending')
        pending_day = _day(pending['day']) if pending else None
        if pending_day and pending_day > today:
            raise ReportError('clock', 'before_pending')
        if pending and pending_day == today:
            if pending['phase'] in ('sending', 'uncertain'):
                return 'delivery_uncertain_no_retry'
            retry_at = _utc(pending.get('retry_after_utc'))
            if retry_at is not None and now < retry_at:
                return 'waiting_before_safe_retry'
            if not self.telegram.available():
                raise ReportError('telegram_config', 'missing')
            pending['phase'] = 'sending'
            pending.pop('retry_after_utc', None)
            state_sha = self.repo.save_state(state, state_sha)
            return self._send_reserved(state, state_sha, now)
        if not pending and today <= covered:
            return 'already_covered_today'
        if not self.telegram.available():
            raise ReportError('telegram_config', 'missing')
        # A prior incomplete day is not blindly resent. Continue from the last
        # fully acknowledged snapshot and mention the uncertainty in today's report.
        uncertain_day = pending['day'] if pending else None
        snapshot, metadata = self._read_source(head)
        chunks = format_report(snapshot, state['snapshot'], state['last_covered_day'],
                               state['baseline_kind'], today, metadata, now, uncertain_day)
        state['pending'] = {
            'day': today.isoformat(),
            'phase': 'sending',
            'next_chunk': 0,
            'message_ids': [],
            'chunks': chunks,
            'snapshot': snapshot,
            'snapshot_sha256': snapshot_digest(snapshot),
            'created_at_utc': _utc_text(now),
        }
        state_sha = self.repo.save_state(state, state_sha)
        return self._send_reserved(state, state_sha, now)

    def _send_reserved(self, state: dict, state_sha: str, now: datetime) -> str:
        while True:
            pending = state['pending']
            index = pending['next_chunk']
            try:
                message_id = self.telegram.send(pending['chunks'][index])
            except DeliveryRejected as exc:
                pending['phase'] = 'ready'
                pending['retry_after_utc'] = _utc_text(now + timedelta(seconds=exc.retry_seconds))
                pending['last_error'] = 'telegram_rejected_' + str(exc.code)
                self.repo.save_state(state, state_sha)
                return 'telegram_rejected_safe_retry_later'
            except DeliveryUncertain as exc:
                pending['phase'] = 'uncertain'
                pending['last_error'] = exc.code
                self.repo.save_state(state, state_sha)
                return 'delivery_attempt_uncertain'
            pending['message_ids'].append(message_id)
            pending['next_chunk'] += 1
            if pending['next_chunk'] == len(pending['chunks']):
                state.update({
                    'last_covered_day': pending['day'],
                    'baseline_kind': 'report',
                    'snapshot': pending['snapshot'],
                    'snapshot_sha256': pending['snapshot_sha256'],
                    'last_message_ids': pending['message_ids'],
                    'last_sent_at_utc': _utc_text(now),
                    'pending': None,
                })
                self.repo.save_state(state, state_sha)
                return 'report_sent_and_baseline_saved'
            pending['phase'] = 'ready'
            state_sha = self.repo.save_state(state, state_sha)
            self.sleep(1.1)
            pending['phase'] = 'sending'
            state_sha = self.repo.save_state(state, state_sha)


def main() -> int:
    try:
        repo = GitHubRepo(os.environ.get('GITHUB_REPOSITORY', ''), os.environ.get('GITHUB_TOKEN', ''))
        telegram = Telegram(os.environ.get('TELEGRAM_BOT_TOKEN', ''), os.environ.get('TELEGRAM_CHAT_ID', ''))
        bootstrap = os.environ.get('REPORT_BOOTSTRAP_ONLY', '').strip().lower() in ('1', 'true', 'yes')
        outcome = Reporter(repo, telegram).run(datetime.now(timezone.utc), bootstrap_only=bootstrap)
        print('Academy daily report:', outcome)
        if bootstrap:
            if not telegram.available():
                raise ReportError('telegram_config', 'missing')
            telegram.check_chat()
            print('Telegram chat check: ok; no message sent')
        if outcome in ('delivery_attempt_uncertain', 'delivery_uncertain_no_retry',
                       'telegram_rejected_safe_retry_later'):
            print('::warning::Daily report delivery incomplete; consult its state before any manual retry.')
        if outcome in ('delivery_attempt_uncertain', 'telegram_rejected_safe_retry_later'):
            return 1
        # A previously recorded uncertainty remains visible as a warning, but
        # does not fail every five-minute heartbeat and flood failure notices.
        return 0
    except ReportError as exc:
        print('::warning::Academy daily report:', exc.stage, exc.code, file=sys.stderr)
        return 1
    except Exception as exc:
        # Never render exception messages: a transport exception may contain a URL with a token.
        print('::warning::Academy daily report unexpected:', type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
