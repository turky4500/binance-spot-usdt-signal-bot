"""A stale recommendation must not start a new Telegram request."""
import pytest

from src.lh1m_telegram import SignalDeadlineExpired, SignalTelegramClient


class Clock:
    def __init__(self): self.t = 1_000
    def __call__(self): return self.t
    def sleep(self, seconds): self.t += int(seconds * 1_000)


class Response:
    def __init__(self, status=200): self.status_code = status
    def json(self):
        return {'ok': self.status_code == 200, 'parameters': {'retry_after': 2}}
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(f'HTTP {self.status_code}')


def make_client(monkeypatch):
    monkeypatch.setenv('TELEGRAM_MUTE', '0')
    monkeypatch.setenv('TELEGRAM_MIN_INTERVAL_SECONDS', '0')
    clock = Clock()
    client = SignalTelegramClient('fake-token', 'fake-chat', clock=clock)
    return client, clock


def test_deadline_expired_before_first_attempt_does_not_contact_telegram(monkeypatch):
    client, _ = make_client(monkeypatch)
    calls = []
    monkeypatch.setattr(client.session, 'post', lambda *a, **kw: calls.append(a) or Response())
    with pytest.raises(SignalDeadlineExpired):
        client.send_before('signal', 999)
    assert not calls and client._deadline_ms is None


def test_429_backoff_rechecks_deadline_before_retry(monkeypatch):
    client, clock = make_client(monkeypatch)
    calls = []
    monkeypatch.setattr('src.telegram_client.time.sleep', clock.sleep)
    monkeypatch.setattr(client.session, 'post', lambda *a, **kw: calls.append(a) or Response(429))
    with pytest.raises(SignalDeadlineExpired):
        client.send_before('signal', 2_000)
    assert len(calls) == 1 and clock.t == 3_000


def test_timely_signal_and_non_signal_notice_can_send(monkeypatch):
    client, _ = make_client(monkeypatch)
    calls = []
    monkeypatch.setattr(client.session, 'post', lambda *a, **kw: calls.append(a) or Response())
    client.send_before('signal', 1_001)
    client.send_message('notice')
    assert len(calls) == 2
