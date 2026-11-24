"""End-to-end API simulation; no GitHub or Telegram network calls."""
from __future__ import annotations

import base64
import copy
import json
import urllib.parse
from datetime import datetime, timezone

from tools import halal_daily_report as r


class SimulatedServices:
    def __init__(self):
        self.counter = 1
        self.current = f'{self.counter:040x}'
        primary = {'BTC': 0, 'ETH': 1, 'XRP': 2}
        meta = {'primary_sha256': r.snapshot_digest(primary),
                'last_check_utc': '2026-11-05T20:40:00Z'}
        self.files = {r.PRIMARY: self.encode(primary), r.META: self.encode(meta),
                      'data/state_unrelated.json': b'{"position":"untouched"}'}
        self.history = {self.current: copy.deepcopy(self.files)}
        self.telegram_texts = []
        self.before_put = None
        self.api_paths = []

    @staticmethod
    def encode(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')

    def commit_external(self, changes):
        self.files.update(changes)
        self.counter += 1
        self.current = f'{self.counter:040x}'
        self.history[self.current] = copy.deepcopy(self.files)

    def __call__(self, method, url, headers, payload=None):
        parsed = urllib.parse.urlparse(url)
        if parsed.netloc == 'api.telegram.org':
            assert method == 'POST' and parsed.path.endswith('/sendMessage')
            state = json.loads(self.files[r.STATE])
            assert state['pending']['phase'] == 'sending'
            body = json.loads(payload)
            assert body['chat_id'] == 'fake-chat'
            self.telegram_texts.append(body['text'])
            result = {'ok': True, 'result': {'message_id': len(self.telegram_texts)}}
            return 200, self.encode(result)
        assert parsed.netloc == 'api.github.com'
        assert headers['Authorization'] == 'Bearer fake-github-token'
        prefix = '/repos/example/repo'
        assert parsed.path.startswith(prefix + '/')
        path = parsed.path[len(prefix):]
        self.api_paths.append((method, path))
        if method == 'GET' and path == '/git/ref/heads/main':
            return 200, self.encode({'object': {'sha': self.current}})
        assert path.startswith('/contents/')
        filename = path[len('/contents/'):]
        if method == 'GET':
            ref = urllib.parse.parse_qs(parsed.query)['ref'][0]
            contents = self.history[ref].get(filename)
            if contents is None:
                return 404, b'{}'
            obj = {'type': 'file', 'encoding': 'base64',
                   'sha': r._blob_sha(contents),
                   'content': base64.b64encode(contents).decode()}
            return 200, self.encode(obj)
        assert method == 'PUT' and filename == r.STATE
        if self.before_put:
            hook, self.before_put = self.before_put, None
            hook()
        body = json.loads(payload)
        assert body['branch'] == 'main' and '[skip ci]' in body['message']
        existing = self.files.get(filename)
        expected_sha = r._blob_sha(existing) if existing is not None else None
        if body.get('sha') != expected_sha:
            return 409, b'{}'
        new_value = base64.b64decode(body['content'])
        self.commit_external({filename: new_value})
        return 200, self.encode({'content': {'sha': r._blob_sha(new_value)}})


def test_full_bootstrap_and_midnight_report_with_concurrent_unrelated_commit(monkeypatch):
    services = SimulatedServices()
    monkeypatch.setattr(r, '_http', services)
    github = r.GitHubRepo('example/repo', 'fake-github-token')
    telegram = r.Telegram('fake-telegram-token', 'fake-chat')
    reporter = r.Reporter(github, telegram, sleep=lambda _: None)
    before = datetime(2026, 11, 5, 20, 59, tzinfo=timezone.utc)
    midnight = datetime(2026, 11, 5, 21, 0, tzinfo=timezone.utc)

    assert reporter.run(before, bootstrap_only=True) == 'baseline_initialized_no_message'
    assert services.telegram_texts == []
    assert reporter.run(before) == 'already_covered_today'

    current = {'BTC': 2, 'ETH': 1, 'NEW': 0}
    meta = {'primary_sha256': r.snapshot_digest(current),
            'last_check_utc': '2026-11-05T20:55:00Z'}
    services.commit_external({r.PRIMARY: services.encode(current), r.META: services.encode(meta)})
    # Another workflow moves main between the reporter's read and state PUT.
    services.before_put = lambda: services.commit_external({'data/state_unrelated.json': b'{"position":"newer"}'})
    assert reporter.run(midnight) == 'report_sent_and_baseline_saved'
    assert len(services.telegram_texts) == 1
    report = services.telegram_texts[0]
    assert 'إجمالي العملات في الملف: 3' in report
    assert 'BTC: من حلال إلى مشبوه' in report
    assert 'NEW: حلال' in report and 'XRP: مشبوه' in report
    assert services.files['data/state_unrelated.json'] == b'{"position":"newer"}'
    saved = json.loads(services.files[r.STATE])
    assert saved['snapshot'] == current and saved['pending'] is None
    raw_state = services.files[r.STATE].decode()
    assert 'fake-github-token' not in raw_state
    assert 'fake-telegram-token' not in raw_state
    assert 'fake-chat' not in raw_state
    assert reporter.run(midnight) == 'already_covered_today'
    assert len(services.telegram_texts) == 1
    assert all(path == '/contents/' + r.STATE for method, path in services.api_paths if method == 'PUT')
