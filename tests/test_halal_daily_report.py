"""Network-free regression tests for the Academy daily file report."""
from __future__ import annotations

import base64
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from tools import halal_daily_report as r


UTC = timezone.utc
BEFORE_MIDNIGHT = datetime(2026, 11, 5, 20, 59, tzinfo=UTC)  # 23:59 Riyadh
MIDNIGHT = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)         # 00:00 Riyadh


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


class FakeRepo:
    def __init__(self, source=None):
        self.source = source or {'BTC': 0, 'ETH': 1, 'XRP': 2}
        self.metadata = {'primary_sha256': r.snapshot_digest(self.source),
                         'last_check_utc': '2026-11-05T20:45:00Z'}
        self.state = None
        self.sha = None
        self.writes = []
        self.fail_write_number = None
        self.reads = []

    def head(self):
        return 'a' * 40

    def read_file(self, path, ref):
        self.reads.append(path)
        assert ref == 'a' * 40
        if path == r.PRIMARY:
            return encode(self.source), 'b' * 40
        if path == r.META:
            return encode(self.metadata), 'c' * 40
        if path == r.STATE:
            return (encode(self.state), self.sha) if self.state is not None else (None, None)
        raise AssertionError(path)

    def save_state(self, value, old_sha):
        assert old_sha == self.sha
        number = len(self.writes) + 1
        if number == self.fail_write_number:
            raise r.ReportError('github_write', 'simulated_failure')
        self.state = copy.deepcopy(value)
        self.sha = r._blob_sha(encode(value))
        self.writes.append(copy.deepcopy(value))
        return self.sha

    def update_source(self, value):
        self.source = value
        self.metadata = {'primary_sha256': r.snapshot_digest(value),
                         'last_check_utc': '2026-11-05T20:55:00Z'}


class FakeTelegram:
    def __init__(self, repo, outcomes=None, available=True):
        self.repo = repo
        self.outcomes = list(outcomes or [])
        self.sent = []
        self._available = available
        self.next_id = 100
        self.chat_checks = 0

    def check_chat(self):
        self.chat_checks += 1

    def available(self):
        return self._available

    def send(self, text):
        assert self.repo.state['pending']['phase'] == 'sending'
        assert self.repo.state['pending']['chunks'][self.repo.state['pending']['next_chunk']] == text
        self.sent.append(text)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        self.next_id += 1
        return self.next_id


def reporter(repo, telegram=None):
    tg = telegram or FakeTelegram(repo)
    return r.Reporter(repo, tg, sleep=lambda _: None), tg


def initialize(rep, now=BEFORE_MIDNIGHT):
    assert rep.run(now, bootstrap_only=True) == 'baseline_initialized_no_message'


def test_baseline_and_riyadh_midnight_do_not_send_early():
    repo = FakeRepo()
    rep, tg = reporter(repo)
    initialize(rep)
    assert repo.state['baseline_kind'] == 'activation'
    assert repo.state['last_covered_day'] == '2026-11-05'
    assert rep.run(BEFORE_MIDNIGHT + timedelta(seconds=59)) == 'already_covered_today'
    assert tg.sent == []
    assert rep.run(MIDNIGHT) == 'report_sent_and_baseline_saved'
    assert len(tg.sent) == 1
    assert '2026-11-06' in tg.sent[0]
    assert 'النسخة عند تفعيل التقرير' in tg.sent[0]
    assert repo.state['last_covered_day'] == '2026-11-06'
    assert repo.state['baseline_kind'] == 'report'
    assert repo.state['pending'] is None


def test_same_day_repeated_heartbeats_do_not_send_or_write_again():
    repo = FakeRepo()
    rep, tg = reporter(repo)
    initialize(rep)
    rep.run(MIDNIGHT)
    writes = len(repo.writes)
    for minutes in (1, 30, 240, 1000):
        assert rep.run(MIDNIGHT + timedelta(minutes=minutes)) == 'already_covered_today'
    assert len(repo.writes) == writes
    assert len(tg.sent) == 1


def test_diff_counts_added_deleted_and_changed_include_values():
    old = {'BTC': 0, 'ETH': 1, 'XRP': 2, 'NONE': None}
    new = {'BTC': 2, 'ETH': 1, 'NEW': 0, 'NONE': 1}
    added, removed, changed = r.diff_snapshots(old, new)
    assert added == [('NEW', 0)]
    assert removed == [('XRP', 2)]
    assert changed == [('BTC', 0, 2), ('NONE', None, 1)]
    text = '\n'.join(r.format_report(new, old, '2026-11-05', 'report',
                                     MIDNIGHT.astimezone(r.RIYADH).date(), {}, MIDNIGHT))
    assert 'إجمالي العملات في الملف: 4' in text
    assert 'حلال: 1' in text and 'حرام: 2' in text
    assert 'بدون حكم: 0' in text and 'مشبوه: 1' in text
    assert 'NEW: حلال' in text and 'XRP: مشبوه' in text
    assert 'BTC: من حلال إلى مشبوه' in text
    assert 'NONE: من بدون حكم إلى حرام' in text


def test_unknown_count_only_counts_explicit_null_values_inside_file():
    old = {'BTC': 0, 'UNDECIDED': None}
    text = '\n'.join(r.format_report(old, old, '2026-11-05', 'report',
                                     MIDNIGHT.astimezone(r.RIYADH).date(), {}, MIDNIGHT))
    assert 'إجمالي العملات في الملف: 2' in text
    assert 'بدون حكم: 1' in text
    assert 'لا توجد تغييرات في الملف' in text


@pytest.mark.parametrize('bad', [{}, [], None, {'BTC': True}, {'BTC': 3},
                                  {'BTC': '0'}, {'btc': 0}, {'BTC\nX': 0}, {'': 0}])
def test_invalid_source_fails_closed(bad):
    with pytest.raises(r.ReportError):
        r.validate_primary(bad)


def test_duplicate_keys_are_rejected():
    with pytest.raises(r.ReportError):
        r.parse_json(b'{"BTC":0,"BTC":1}', 'primary')


def test_only_file_is_compared_and_new_baseline_is_saved_after_success():
    repo = FakeRepo()
    rep, tg = reporter(repo)
    initialize(rep)
    repo.update_source({'BTC': 2, 'ETH': 1, 'NEW': 0})
    assert rep.run(MIDNIGHT) == 'report_sent_and_baseline_saved'
    text = tg.sent[0]
    assert 'NEW: حلال' in text and 'XRP: مشبوه' in text
    assert 'BTC: من حلال إلى مشبوه' in text
    assert repo.state['snapshot'] == repo.source
    assert set(repo.reads) <= {r.PRIMARY, r.META, r.STATE}
    assert all(write.get('pending') is None or write['pending']['phase'] == 'sending'
               for write in repo.writes)


def test_first_report_baseline_does_not_reset_on_repeat_bootstrap():
    repo = FakeRepo()
    rep, tg = reporter(repo)
    initialize(rep)
    baseline = copy.deepcopy(repo.state)
    repo.update_source({'BTC': 1, 'NEW': 0})
    assert rep.run(BEFORE_MIDNIGHT, bootstrap_only=True) == 'baseline_already_exists_no_message'
    assert repo.state == baseline and tg.sent == []
    assert rep.run(MIDNIGHT) == 'report_sent_and_baseline_saved'
    assert 'BTC: من حلال إلى حرام' in tg.sent[0]
    assert 'NEW: حلال' in tg.sent[0]


def test_missing_state_fails_closed_until_explicit_bootstrap():
    repo = FakeRepo()
    rep, tg = reporter(repo)
    with pytest.raises(r.ReportError, match='state'):
        rep.run(MIDNIGHT)
    assert not tg.sent and repo.state is None and not repo.writes
    assert rep.run(MIDNIGHT, bootstrap_only=True) == 'baseline_initialized_no_message'
    assert repo.state['last_covered_day'] == '2026-11-06'


def test_missing_telegram_config_never_reserves_or_advances_state():
    repo = FakeRepo()
    tg = FakeTelegram(repo, available=False)
    rep, _ = reporter(repo, tg)
    initialize(rep)
    with pytest.raises(r.ReportError, match='telegram_config'):
        rep.run(MIDNIGHT)
    assert repo.state['pending'] is None and not tg.sent
    assert len(repo.writes) == 1


def test_uncertain_delivery_is_not_retried_on_same_day():
    repo = FakeRepo()
    tg = FakeTelegram(repo, [r.DeliveryUncertain('network')])
    rep, _ = reporter(repo, tg)
    initialize(rep)
    repo.update_source({'BTC': 2, 'ETH': 1, 'XRP': 2})
    assert rep.run(MIDNIGHT) == 'delivery_attempt_uncertain'
    assert repo.state['pending']['phase'] == 'uncertain'
    assert repo.state['snapshot']['BTC'] == 0
    assert rep.run(MIDNIGHT + timedelta(hours=1)) == 'delivery_uncertain_no_retry'
    assert len(tg.sent) == 1


def test_next_day_after_uncertain_report_warns_and_uses_last_complete_baseline():
    repo = FakeRepo()
    tg = FakeTelegram(repo, [r.DeliveryUncertain('network')])
    rep, _ = reporter(repo, tg)
    initialize(rep)
    repo.update_source({'BTC': 2, 'ETH': 1, 'XRP': 2})
    rep.run(MIDNIGHT)
    assert rep.run(MIDNIGHT + timedelta(days=1)) == 'report_sent_and_baseline_saved'
    assert 'لم يتأكد اكتمال تقرير 2026-11-06' in tg.sent[-1]
    assert 'BTC: من حلال إلى مشبوه' in tg.sent[-1]
    assert repo.state['last_covered_day'] == '2026-11-07'


def test_explicit_telegram_rejection_retries_only_after_delay():
    repo = FakeRepo()
    tg = FakeTelegram(repo, [r.DeliveryRejected(429, retry_seconds=120)])
    rep, _ = reporter(repo, tg)
    initialize(rep)
    assert rep.run(MIDNIGHT) == 'telegram_rejected_safe_retry_later'
    assert repo.state['pending']['phase'] == 'ready'
    assert rep.run(MIDNIGHT + timedelta(seconds=119)) == 'waiting_before_safe_retry'
    assert len(tg.sent) == 1
    assert rep.run(MIDNIGHT + timedelta(seconds=120)) == 'report_sent_and_baseline_saved'
    assert len(tg.sent) == 2
    assert tg.sent[0] == tg.sent[1]


def test_crash_during_send_does_not_blindly_retry():
    repo = FakeRepo()
    tg = FakeTelegram(repo, [RuntimeError('simulated crash')])
    rep, _ = reporter(repo, tg)
    initialize(rep)
    with pytest.raises(RuntimeError):
        rep.run(MIDNIGHT)
    assert repo.state['pending']['phase'] == 'sending'
    assert rep.run(MIDNIGHT + timedelta(minutes=10)) == 'delivery_uncertain_no_retry'
    assert len(tg.sent) == 1


def test_failure_before_reservation_never_sends():
    repo = FakeRepo()
    rep, tg = reporter(repo)
    initialize(rep)
    repo.fail_write_number = 2
    with pytest.raises(r.ReportError, match='github_write'):
        rep.run(MIDNIGHT)
    assert not tg.sent and repo.state['pending'] is None


def test_failure_saving_success_leaves_uncertain_state_and_no_resend():
    repo = FakeRepo()
    rep, tg = reporter(repo)
    initialize(rep)
    repo.fail_write_number = 3
    with pytest.raises(r.ReportError, match='github_write'):
        rep.run(MIDNIGHT)
    assert len(tg.sent) == 1 and repo.state['pending']['phase'] == 'sending'
    assert rep.run(MIDNIGHT + timedelta(minutes=5)) == 'delivery_uncertain_no_retry'
    assert len(tg.sent) == 1


def test_multi_part_resume_never_repeats_already_acknowledged_part():
    repo = FakeRepo()
    tg = FakeTelegram(repo, [111, r.DeliveryRejected(429, 60)])
    rep, _ = reporter(repo, tg)
    initialize(rep)
    many = {'BTC': 0, 'ETH': 1, 'XRP': 2}
    many.update({f'COIN{i:04d}': i % 3 for i in range(700)})
    repo.update_source(many)
    assert rep.run(MIDNIGHT) == 'telegram_rejected_safe_retry_later'
    assert len(tg.sent) == 2 and tg.sent[0] != tg.sent[1]
    pending = repo.state['pending']
    assert pending['next_chunk'] == 1 and pending['message_ids'] == [111]
    first_part = tg.sent[0]
    assert rep.run(MIDNIGHT + timedelta(seconds=60)) == 'report_sent_and_baseline_saved'
    assert sum(x == first_part for x in tg.sent) == 1
    assert all(len(x.encode('utf-8')) <= 4096 for x in tg.sent)
    assert repo.state['snapshot'] == many


def test_corrupt_baseline_fails_closed_and_sends_nothing():
    repo = FakeRepo()
    rep, tg = reporter(repo)
    initialize(rep)
    repo.state['snapshot']['BTC'] = 2
    with pytest.raises(r.ReportError, match='state'):
        rep.run(MIDNIGHT)
    assert not tg.sent


def test_future_baseline_does_not_send():
    repo = FakeRepo()
    rep, tg = reporter(repo)
    initialize(rep, MIDNIGHT)
    with pytest.raises(r.ReportError, match='clock'):
        rep.run(BEFORE_MIDNIGHT)
    assert not tg.sent


def test_missing_or_mismatched_metadata_is_explicitly_uncertain():
    snapshot = {'BTC': 0}
    text = '\n'.join(r.format_report(snapshot, snapshot, '2026-11-05', 'report',
                                     MIDNIGHT.astimezone(r.RIYADH).date(), {}, MIDNIGHT))
    assert 'آخر فحص للمصدر: غير مؤكد' in text
    good = {'primary_sha256': r.snapshot_digest(snapshot), 'last_check_utc': '2026-11-05T20:55:00Z'}
    text = '\n'.join(r.format_report(snapshot, snapshot, '2026-11-05', 'report',
                                     MIDNIGHT.astimezone(r.RIYADH).date(), good, MIDNIGHT))
    assert '2026-11-05 23:55:00 الرياض' in text


def test_multipart_has_no_oversize_messages():
    old = {'BTC': 0}
    new = {'BTC': 0, **{f'TOKEN{i:04d}': i % 3 for i in range(818)}}
    chunks = r.format_report(new, old, '2026-11-05', 'report',
                             MIDNIGHT.astimezone(r.RIYADH).date(), {}, MIDNIGHT)
    assert len(chunks) > 1
    assert all('الجزء ' in x and len(x.encode('utf-8')) <= 4096 for x in chunks)
    assert 'TOKEN0000: حلال' in '\n'.join(chunks)
    assert 'TOKEN0817:' in '\n'.join(chunks)


def test_bootstrap_main_checks_chat_without_sending(monkeypatch, capsys):
    repo = FakeRepo()
    tg = FakeTelegram(repo)
    monkeypatch.setattr(r, 'GitHubRepo', lambda *_: repo)
    monkeypatch.setattr(r, 'Telegram', lambda *_: tg)
    monkeypatch.setenv('REPORT_BOOTSTRAP_ONLY', 'true')
    assert r.main() == 0
    assert tg.chat_checks == 1 and not tg.sent
    assert repo.state['baseline_kind'] == 'activation'
    assert 'no message sent' in capsys.readouterr().out


def test_telegram_read_only_chat_check_sends_no_message(monkeypatch):
    calls = []
    def fake_http(method, url, headers, payload):
        calls.append((method, url, json.loads(payload)))
        return 200, b'{"ok":true,"result":{"id":123}}'
    monkeypatch.setattr(r, '_http', fake_http)
    tg = r.Telegram('synthetic-token', 'synthetic-chat')
    tg.check_chat()
    assert len(calls) == 1 and calls[0][1].endswith('/getChat')
    assert calls[0][2] == {'chat_id': 'synthetic-chat'}


def test_telegram_http_rejection_and_uncertain_failure(monkeypatch):
    tg = r.Telegram('synthetic-token', 'synthetic-chat')
    monkeypatch.setattr(r, '_http', lambda *a, **kw: (200, b'{"ok":true,"result":{"message_id":42}}'))
    assert tg.send('hello') == 42
    monkeypatch.setattr(r, '_http', lambda *a, **kw: (429, b'{"ok":false,"parameters":{"retry_after":90}}'))
    with pytest.raises(r.DeliveryRejected) as rejected:
        tg.send('hello')
    assert rejected.value.code == 429 and rejected.value.retry_seconds == 90
    monkeypatch.setattr(r, '_http', lambda *a, **kw: (500, b'{"ok":false}'))
    with pytest.raises(r.DeliveryUncertain):
        tg.send('hello')
    def network(*_a, **_kw):
        raise OSError('sensitive URL must not be printed')
    monkeypatch.setattr(r, '_http', network)
    with pytest.raises(r.DeliveryUncertain) as ex:
        tg.send('hello')
    assert 'sensitive' not in str(ex.value)


def test_github_contents_api_writes_only_report_state(monkeypatch):
    calls = []
    primary = encode({'BTC': 0})
    primary_sha = r._blob_sha(primary)
    def fake_http(method, url, headers, payload=None):
        calls.append((method, url, payload))
        assert url.startswith('https://api.github.com/repos/example/repo/')
        if '/git/ref/' in url:
            return 200, json.dumps({'object': {'sha': 'a' * 40}}).encode()
        if method == 'GET':
            obj = {'type': 'file', 'encoding': 'base64', 'sha': primary_sha,
                   'content': base64.b64encode(primary).decode()}
            return 200, json.dumps(obj).encode()
        body = json.loads(payload)
        assert url.endswith('/contents/' + r.STATE)
        assert body['branch'] == 'main' and '[skip ci]' in body['message']
        raw = base64.b64decode(body['content'])
        return 200, json.dumps({'content': {'sha': r._blob_sha(raw)}}).encode()
    monkeypatch.setattr(r, '_http', fake_http)
    gh = r.GitHubRepo('example/repo', 'synthetic-token')
    assert gh.head() == 'a' * 40
    raw, sha = gh.read_file(r.PRIMARY, 'a' * 40)
    assert raw == primary and sha == primary_sha
    assert gh.save_state(r.initial_state({'BTC': 0}, MIDNIGHT.date()), None)
    assert len(calls) == 3


def test_github_file_digest_mismatch_fails_closed(monkeypatch):
    obj = {'type': 'file', 'encoding': 'base64', 'sha': 'f' * 40,
           'content': base64.b64encode(b'{}').decode()}
    monkeypatch.setattr(r, '_http', lambda *a, **kw: (200, json.dumps(obj).encode()))
    gh = r.GitHubRepo('example/repo', 'synthetic-token')
    with pytest.raises(r.ReportError, match='github_read'):
        gh.read_file(r.PRIMARY, 'a' * 40)
