"""No-network state CAS and halal-snapshot safety tests."""
import base64
import json
from datetime import datetime, timezone

import pytest
import requests

from src.lh1m_bot import empty_state
from src.lh1m_github import (GitHubStateStore, HalalProvider, StateConflict,
                            STATE_BRANCH, STATE_PATH, parse_snapshot, primary_digest)

T = int(datetime(2026, 11, 5, 12, tzinfo=timezone.utc).timestamp() * 1000)


class Response:
    def __init__(self, status, sha=None): self.status_code, self.sha = status, sha
    def json(self): return {'content': {'sha': self.sha}}
    def raise_for_status(self):
        if self.status_code >= 400: raise requests.HTTPError(str(self.status_code))


class FakeGitHub:
    def __init__(self):
        self.content, self.sha, self.n = None, None, 0
        self.mode = None
        self.branches, self.paths = [], []
    def create_state_branch(self): self.branches.append(STATE_BRANCH)
    def get(self, path, ref, missing_ok=False):
        assert path == STATE_PATH and ref == STATE_BRANCH
        return (self.content, self.sha) if self.content is not None else None
    def put(self, path, branch, content, sha):
        self.paths.append((path, branch))
        self.n += 1
        if self.mode == 'after':
            self.mode = None
            self.content, self.sha = content, f'sha{self.n}'
            raise requests.Timeout('write accepted but response lost')
        if self.mode == 'before':
            self.mode = None
            raise requests.Timeout('write never reached server')
        if self.mode == 'conflict':
            self.content, self.sha = b'{"different":true}', 'other-writer'
            return Response(409)
        if self.mode == 'identical-conflict':
            self.content, self.sha = content, 'other-writer-same-content'
            return Response(409)
        assert sha == self.sha
        self.content, self.sha = content, f'sha{self.n}'
        return Response(201 if self.n == 1 else 200, self.sha)


def test_state_only_uses_isolated_branch_and_roundtrips():
    gh = FakeGitHub()
    store = GitHubStateStore(gh)
    assert store.load() is None
    state = empty_state(T)
    store.save(state)
    assert gh.paths == [(STATE_PATH, STATE_BRANCH)]
    assert GitHubStateStore(gh).load() == state
    store.save(state)
    assert len(gh.paths) == 1  # no needless commit for identical data


def test_timeout_after_accepted_write_is_reconciled_not_repeated(monkeypatch):
    gh, store = FakeGitHub(), None
    store = GitHubStateStore(gh); store.load()
    gh.mode = 'after'
    monkeypatch.setattr('src.lh1m_github.time.sleep', lambda _: None)
    state = empty_state(T)
    store.save(state)
    assert gh.n == 1 and store.sha == gh.sha and json.loads(gh.content) == state


def test_timeout_before_write_is_safely_retried(monkeypatch):
    gh = FakeGitHub(); store = GitHubStateStore(gh); store.load()
    gh.mode = 'before'
    monkeypatch.setattr('src.lh1m_github.time.sleep', lambda _: None)
    state = empty_state(T)
    store.save(state)
    assert gh.n == 2 and json.loads(gh.content) == state


def test_competing_state_write_stops_instead_of_overwriting(monkeypatch):
    gh = FakeGitHub(); store = GitHubStateStore(gh); store.load()
    state = empty_state(T); store.save(state)
    state['positions']['AAAUSDT'] = {'entry_price': 1}
    gh.mode = 'conflict'
    monkeypatch.setattr('src.lh1m_github.time.sleep', lambda _: None)
    with pytest.raises(StateConflict): store.save(state)
    assert gh.sha == 'other-writer'


def test_identical_competing_write_is_not_mistaken_for_our_own_success():
    gh = FakeGitHub(); store = GitHubStateStore(gh); store.load()
    state = empty_state(T); store.save(state)
    state['positions']['AAAUSDT'] = {'entry_price': 1}
    gh.mode = 'identical-conflict'
    with pytest.raises(StateConflict, match='CAS rejected'):
        store.save(state)
    assert gh.sha == 'other-writer-same-content'


def test_oversized_state_stops_before_writing():
    gh = FakeGitHub(); store = GitHubStateStore(gh); store.load()
    state = empty_state(T); state['oversized'] = 'x' * 900_001
    with pytest.raises(RuntimeError, match='size'): store.save(state)
    assert gh.n == 0


def meta(primary=None, **updates):
    d = {'source': 'btcacademy.online', 'last_check_utc': '2026-11-05T12:00:00Z',
         'unverified_symbols': ['RETAINED'], 'ambiguous': ['AMB'],
         'unknown': [], 'unclear_articles': []}
    if primary is not None:
        d['primary_sha256'] = primary_digest(primary)
    return {**d, **updates}


def test_halal_snapshot_requires_recent_known_independent_verdicts():
    primary = {'AAA': 0, 'USDT': 0, 'RETAINED': 0, 'AMB': 0, 'HARAM': 1}
    snap = parse_snapshot(primary, meta(primary), minimum_symbols=1)
    assert snap.allows_entry('AAAUSDT', T)
    for sym in ('RETAINEDUSDT', 'AMBUSDT', 'HARAMUSDT', 'UNKNOWNUSDT'):
        assert not snap.allows_entry(sym, T)
    assert not snap.allows_entry('AAAUSDT', T + 4_500_001)
    assert not snap.allows_entry('AAAUSDT', T - 300_001)
    for bad in ({'USDT': False, 'AAA': 0}, {'USDT': 0, 'AAA': '0'}):
        with pytest.raises(ValueError): parse_snapshot(bad, meta(), minimum_symbols=1)
    m = meta(); del m['unverified_symbols']
    with pytest.raises(ValueError, match='verification'): parse_snapshot({'USDT': 0}, m, minimum_symbols=1)


def test_partial_two_commit_update_and_missing_digest_fail_closed():
    old = {'USDT': 0, 'AAA': 0}
    changed = {'USDT': 0, 'AAA': 1}
    with pytest.raises(ValueError, match='digest mismatch'):
        parse_snapshot(changed, meta(old), minimum_symbols=1)
    with pytest.raises(ValueError, match='digest mismatch'):
        parse_snapshot(old, meta(), minimum_symbols=1)


def test_halal_provider_reads_both_files_at_the_same_head():
    primary = {'USDT': 0, 'AAA': 0, **{f'C{i}': 1 for i in range(700)}}
    class Fake:
        def __init__(self): self.refs = []
        def ref(self, branch): return 'immutable-head-sha'
        def get(self, path, ref):
            self.refs.append(ref)
            obj = primary if path.endswith('halal_academy.json') else meta(primary)
            return json.dumps(obj).encode(), 'file-sha'
    fake = Fake(); result = HalalProvider(fake).get(force=True)
    assert fake.refs == ['immutable-head-sha', 'immutable-head-sha']
    assert result.allows_entry('AAAUSDT', T)


def test_halal_provider_invalidates_previous_snapshot_on_partial_update_or_network_error():
    primary = {'USDT': 0, 'AAA': 0, **{f'C{i}': 1 for i in range(700)}}
    class Fake:
        def __init__(self): self.mode = 'valid'
        def ref(self, branch):
            if self.mode == 'network':
                raise requests.ConnectionError('unavailable')
            return 'immutable-head-sha'
        def get(self, path, ref):
            obj = primary if path.endswith('halal_academy.json') else meta(primary)
            if self.mode == 'partial' and path.endswith('halal_academy.json'):
                obj = {**primary, 'AAA': 1}
            return json.dumps(obj).encode(), 'file-sha'
    fake, provider = Fake(), None
    provider = HalalProvider(fake)
    assert provider.get(force=True).allows_entry('AAAUSDT', T)
    fake.mode = 'partial'
    assert provider.get(force=True) is None and provider.snapshot is None
    fake.mode = 'valid'
    assert provider.get(force=True).allows_entry('AAAUSDT', T)
    fake.mode = 'network'
    assert provider.get(force=True) is None and provider.snapshot is None
