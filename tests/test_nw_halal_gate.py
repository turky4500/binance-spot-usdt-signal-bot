"""Academy-only NW entry gate: no network and no real Telegram messages."""
from __future__ import annotations

import base64
import copy
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import nw_bot
from nw_bot import NWBot
from src import nw_halal_gate as gate_module
from src.nw_halal_gate import AcademyPrimaryGate, Decision
from src.nw_journal import read_trades
from src.nw_state import NWStateStore, symbol_state

ROOT = Path(__file__).resolve().parents[1]
BASE_TIME = 1_700_000_000_000
BUY = {
    'bar_time': BASE_TIME,
    'confirm_bar_time': BASE_TIME + 3_600_000,
    'confirm_close': 10.0,
    'signal_price': 9.5,
}
SELL = {
    'bar_time': BASE_TIME + 7_200_000,
    'confirm_bar_time': BASE_TIME + 10_800_000,
    'confirm_close': 11.0,
    'signal_price': 11.0,
}


@pytest.fixture(autouse=True)
def local_gate_environment(monkeypatch):
    # Do not inherit the CI runner's environment into tests of local files.
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    monkeypatch.delenv('NW_HALAL_GITHUB_TOKEN', raising=False)
    monkeypatch.delenv('GITHUB_REPOSITORY', raising=False)


def primary(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding='utf-8')


def res(buy=(), sell=(), cancelled=()):
    return {
        'ok': True, 'buy': list(buy), 'sell': list(sell),
        'cancelled_buy': list(cancelled), 'ignored_retro': 0,
        'ignored_shallow': 0,
    }


class Telegram:
    def __init__(self, after_send=None):
        self.messages = []
        self.after_send = after_send

    def send_message(self, text):
        self.messages.append(text)
        if self.after_send:
            self.after_send()


class Client:
    def __init__(self, symbols=('AAAUSDT', 'BBBUSDT')):
        self.symbols = list(symbols)
        self.requested = []

    def get_server_time(self):
        return BASE_TIME + 15_000_000

    def get_spot_usdt_symbols(self):
        return list(self.symbols)

    def get_klines_for_symbols(self, symbols, interval, limit):
        self.requested.append((list(symbols), interval, limit))
        return {s: s for s in symbols}


def bot_for(tmp_path, rulings, *, include_all=True, after_send=None, **kw):
    path = tmp_path / 'primary.json'
    primary(path, rulings)
    tg = Telegram(after_send)
    store = NWStateStore(str(tmp_path / 'state.json'))
    bot = NWBot(
        client=Client(), telegram=tg, state_store=store,
        state_path=str(tmp_path / 'state.json'),
        trade_log=str(tmp_path / 'trades.csv'),
        halal_primary=str(path),
        halal_extra=str(tmp_path / 'extra.json'),
        halal_cache=str(tmp_path / 'cache.json'),
        include_all=include_all, **kw,
    )
    bot.send_daily_report_if_due = lambda now: None
    return bot, tg, store, path


def open_position(state, sym='AAAUSDT'):
    item = symbol_state(state, sym)
    item.update({
        'position': {'entry_price': 10.0, 'entry_time': BASE_TIME,
                     'signal_bar_time': BASE_TIME - 3_600_000,
                     'signal_price': 9.5, 'entry_count': 1},
        'entry_count': 1, 'last_buy_bar': BASE_TIME - 3_600_000,
        'last_entry_price': 10.0,
    })
    return item


def stub_signals(monkeypatch, results):
    monkeypatch.setattr(nw_bot, 'candles_closed', lambda df, now: (df, None, None))
    monkeypatch.setattr(nw_bot, 'evaluate_symbol',
                        lambda sym, c, t, cfg: results.get(sym, res()))


@pytest.mark.parametrize('symbol,expected', [
    ('AAAUSDT', Decision(True, 'halal')),
    ('BBBUSDT', Decision(False, 'haram')),
    ('CCCUSDT', Decision(False, 'doubtful')),
    ('DDDUSDT', Decision(False, 'unknown')),
    ('MISSINGUSDT', Decision(False, 'unknown')),
    ('USDT', Decision(False, 'invalid_symbol')),
    ('aaausdt', Decision(False, 'invalid_symbol')),
    ('AAA/USDT', Decision(False, 'invalid_symbol')),
])
def test_primary_values_and_missing_keys(symbol, expected, tmp_path):
    path = tmp_path / 'primary.json'
    primary(path, {'AAA': 0, 'BBB': 1, 'CCC': 2, 'DDD': None})
    gate = AcademyPrimaryGate(str(path))
    assert gate(symbol) == expected


@pytest.mark.parametrize('raw', [
    b'', b'not json', b'{}', b'[]', b'null', b'"x"',
    b'{"AAA":true}', b'{"AAA":false}', b'{"AAA":0.0}',
    b'{"AAA":"0"}', b'{"AAA":3}', b'{"AAA":-1}',
    b'{"AAA":NaN}', b'{"AAA":0,"AAA":1}', b'{"aaa":0}',
    b'{" AAA":0}', b'{"A$":0}', b'{"AAA":0, "OTHER":{}}', b'\xff',
])
def test_corrupt_or_ambiguous_file_never_permits_any_entry(tmp_path, raw):
    path = tmp_path / 'primary.json'
    path.write_bytes(raw)
    assert AcademyPrimaryGate(str(path))('AAAUSDT') == Decision(False, 'unavailable')


def test_missing_file_denies_and_recovers_next_cycle(tmp_path):
    path = tmp_path / 'missing.json'
    gate = AcademyPrimaryGate(str(path))
    assert gate('AAAUSDT') == Decision(False, 'unavailable')
    primary(path, {'AAA': 0})
    assert gate('AAAUSDT') == Decision(False, 'unavailable')  # circuit breaker
    gate.begin_cycle()
    assert gate('AAAUSDT') == Decision(True, 'halal')


def test_local_file_is_reread_for_each_new_or_repeat_entry(tmp_path):
    path = tmp_path / 'primary.json'
    primary(path, {'AAA': 0})
    gate = AcademyPrimaryGate(str(path))
    assert gate('AAAUSDT') == Decision(True, 'halal')
    primary(path, {'AAA': 1})
    assert gate('AAAUSDT') == Decision(False, 'haram')
    primary(path, {'AAA': 2})
    assert gate('AAAUSDT') == Decision(False, 'doubtful')
    primary(path, {'AAA': None})
    assert gate('AAAUSDT') == Decision(False, 'unknown')


def test_actions_reads_current_main_each_time_and_never_falls_back_to_checkout(
        monkeypatch, tmp_path, caplog):
    path = tmp_path / 'local.json'
    primary(path, {'AAA': 0, 'BBB': 0})  # deliberately stale local snapshot
    calls = []
    values = [b'{"AAA":0}', b'{"AAA":1}', RuntimeError('fake-secret-value')]

    def read_current(repository, token):
        calls.append((repository, token))
        value = values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(gate_module, '_read_current_github_file', read_current)
    gate = AcademyPrimaryGate(str(path), github_actions=True,
                              repository='owner/repo', token='fake-secret-value')
    assert gate('AAAUSDT') == Decision(True, 'halal')
    assert gate('AAAUSDT') == Decision(False, 'haram')
    assert gate('BBBUSDT') == Decision(False, 'unavailable')
    assert gate('AAAUSDT') == Decision(False, 'unavailable')
    assert len(calls) == 3
    assert 'fake-secret-value' not in caplog.text
    gate.begin_cycle()
    values.append(b'{"AAA":2}')
    assert gate('AAAUSDT') == Decision(False, 'doubtful')
    assert len(calls) == 4


def test_actions_environment_requires_configured_token_not_stale_local(monkeypatch, tmp_path):
    path = tmp_path / 'local.json'
    primary(path, {'AAA': 0})
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_REPOSITORY', 'owner/repo')
    gate = AcademyPrimaryGate.from_environment(str(path))
    assert gate('AAAUSDT') == Decision(False, 'unavailable')


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, max_bytes):
        return self.body[:max_bytes]


def github_payload(contents=b'{"AAA":0}'):
    return {
        'type': 'file', 'encoding': 'base64', 'path': gate_module.PRIMARY_PATH,
        'content': base64.b64encode(contents).decode('ascii'),
        'sha': gate_module._blob_sha(contents),
    }


def mock_response(monkeypatch, body, status=200):
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            return Response(body, status)

    monkeypatch.setattr(gate_module.urllib.request, 'build_opener', lambda *args: Opener())
    return calls


def test_github_contents_request_is_scoped_current_main_and_checks_git_blob(monkeypatch):
    body = json.dumps(github_payload()).encode()
    calls = mock_response(monkeypatch, body)
    assert gate_module._read_current_github_file('owner/repo', 'fake-token') == b'{"AAA":0}'
    request, timeout = calls[0]
    assert request.full_url == ('https://api.github.com/repos/owner/repo/contents/'
                                'data/daily_trend/halal_academy.json?ref=main')
    assert request.get_header('Authorization') == 'Bearer fake-token'
    assert request.get_header('Cache-control') == 'no-cache, no-store'
    assert timeout == 8


@pytest.mark.parametrize('alter', [
    lambda x: {**x, 'type': 'dir'},
    lambda x: {**x, 'encoding': 'none'},
    lambda x: {**x, 'path': 'data/another_file.json'},
    lambda x: {**x, 'sha': '0' * 40},
    lambda x: {**x, 'sha': 'not-a-sha'},
    lambda x: {**x, 'content': '!!!'},
    lambda x: {k: v for k, v in x.items() if k != 'content'},
    lambda x: [],
])
def test_github_contents_rejects_unverified_or_bad_data(monkeypatch, alter):
    payload = alter(github_payload())
    mock_response(monkeypatch, json.dumps(payload).encode())
    with pytest.raises(ValueError):
        gate_module._read_current_github_file('owner/repo', 'fake-token')


@pytest.mark.parametrize('repository,token', [('', 'x'), ('../x', 'x'),
                                            ('owner/repo', ''), ('owner/repo', 'x y')])
def test_github_bad_configuration_fails_closed(repository, token):
    with pytest.raises(ValueError):
        gate_module._read_current_github_file(repository, token)


def test_github_network_errors_redirects_and_oversize_fail_closed(monkeypatch):
    class FailedOpener:
        def open(self, request, timeout):
            raise urllib.error.URLError('offline')

    monkeypatch.setattr(gate_module.urllib.request, 'build_opener', lambda *a: FailedOpener())
    with pytest.raises(ValueError):
        gate_module._read_current_github_file('owner/repo', 'fake-token')
    assert gate_module._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com') is None
    mock_response(monkeypatch, b'x' * (gate_module._MAX_RESPONSE + 1))
    with pytest.raises(ValueError):
        gate_module._read_current_github_file('owner/repo', 'fake-token')


@pytest.mark.parametrize('value', [1, 2, None, 'missing'])
def test_blocked_entry_changes_no_position_history_or_recommendation_count(tmp_path, value):
    rulings = {'AAA': value} if value != 'missing' else {'OTHER': 0}
    bot, tg, store, _ = bot_for(tmp_path, rulings)
    # Contradictory secondary sources may never permit the entry.
    primary(tmp_path / 'extra.json', {'AAA': 0})
    primary(tmp_path / 'cache.json', {'judgements': {'AAA': 0}})
    bot._load_verdicts()
    state = store.load()
    ev = bot._apply('AAAUSDT', res(buy=[BUY]), state)
    assert ev['ruling_filtered'] == 1 and ev['entry'] == 0 and ev['repeat'] == 0
    assert tg.messages == [] and state['messages_sent'] == 0 and state['sent'] == []
    assert state['symbols']['AAAUSDT']['position'] is None
    assert state['symbols']['AAAUSDT']['entry_count'] == 0
    assert state['buy_history']['AAAUSDT'] == []
    assert not (tmp_path / 'trades.csv').exists()


def test_primary_halal_wins_even_if_fallback_disagrees_and_is_labelled_correctly(tmp_path):
    bot, tg, store, _ = bot_for(tmp_path, {'AAA': 0})
    primary(tmp_path / 'extra.json', {'AAA': 1})
    primary(tmp_path / 'cache.json', {'judgements': {'AAA': 2}})
    bot._load_verdicts()
    state = store.load()
    ev = bot._apply('AAAUSDT', res(buy=[BUY]), state)
    assert ev['entry'] == 1 and ev['ruling_filtered'] == 0
    assert 'الحكم الشرعي: ✅ مباح' in tg.messages[0]
    assert state['symbols']['AAAUSDT']['position']['entry_count'] == 1
    assert state['messages_sent'] == 1 and state['buy_history']['AAAUSDT'] == [BUY['bar_time']]


@pytest.mark.parametrize('bad', [1, 2, None, 'missing', 'corrupt'])
def test_changed_ruling_blocks_repeat_but_allows_exit_same_cycle(tmp_path, bad):
    bot, tg, store, path = bot_for(tmp_path, {'AAA': 0})
    state = store.load()
    assert bot._apply('AAAUSDT', res(buy=[BUY]), state)['entry'] == 1
    pos_before = copy.deepcopy(state['symbols']['AAAUSDT']['position'])
    assert len(tg.messages) == 1
    if bad == 'corrupt':
        path.write_text('[]', encoding='utf-8')
    elif bad == 'missing':
        path.unlink()
    else:
        primary(path, {'AAA': bad})
    second = {**BUY, 'bar_time': BUY['bar_time'] + 3_600_000,
              'confirm_bar_time': BUY['confirm_bar_time'] + 3_600_000}
    ev = bot._apply('AAAUSDT', res(buy=[second], sell=[SELL]), state)
    assert ev['repeat'] == 0 and ev['ruling_filtered'] == 1 and ev['exit'] == 1
    assert len(tg.messages) == 2 and 'إشارة خروج' in tg.messages[-1]
    if bad == 1:
        assert '⛔ غير مباح' in tg.messages[-1]
    if bad == 2:
        assert '⚠️ مشبوه' in tg.messages[-1]
    assert state['symbols']['AAAUSDT']['position'] is None
    assert state['symbols']['AAAUSDT']['entry_count'] == 0
    trades = read_trades(str(tmp_path / 'trades.csv'))
    assert len(trades) == 1 and trades[0]['entries_count'] == str(pos_before['entry_count'])
    assert state['messages_sent'] == 2


def test_blocked_repeat_alone_does_not_mutate_existing_position(tmp_path):
    bot, tg, store, _ = bot_for(tmp_path, {'AAA': 2})
    state = store.load()
    item = open_position(state)
    before = copy.deepcopy(item)
    ev = bot._apply('AAAUSDT', res(buy=[BUY]), state)
    assert ev['repeat'] == 0 and ev['ruling_filtered'] == 1
    assert item == before and tg.messages == [] and state['messages_sent'] == 0


def test_file_change_during_same_apply_stops_next_repeat(tmp_path):
    path = tmp_path / 'primary.json'
    bot, tg, store, _ = bot_for(tmp_path, {'AAA': 0},
                                after_send=lambda: primary(path, {'AAA': 1}))
    second = {**BUY, 'bar_time': BUY['bar_time'] + 3_600_000,
              'confirm_bar_time': BUY['confirm_bar_time'] + 3_600_000}
    state = store.load()
    ev = bot._apply('AAAUSDT', res(buy=[BUY, second]), state)
    assert ev['entry'] == 1 and ev['repeat'] == 0 and ev['ruling_filtered'] == 1
    assert len(tg.messages) == 1 and state['symbols']['AAAUSDT']['entry_count'] == 1


def test_include_all_false_keeps_nonhalal_open_position_for_exit(monkeypatch, tmp_path):
    bot, tg, store, _ = bot_for(tmp_path, {'AAA': 1, 'BBB': 0}, include_all=False)
    state = store.load()
    open_position(state)
    store.save(state)
    assert 'AAAUSDT' in bot.refresh_universe()
    stub_signals(monkeypatch, {'AAAUSDT': res(buy=[BUY], sell=[SELL])})
    summary = bot.run_once(now_ms=BASE_TIME + 15_000_000)
    assert 'AAAUSDT' in bot.client.requested[-1][0]
    assert summary['repeat'] == 0 and summary['ruling_filtered'] == 1 and summary['exit'] == 1
    assert len(tg.messages) == 1 and 'إشارة خروج' in tg.messages[0]
    assert store.load()['symbols']['AAAUSDT']['position'] is None


def test_custom_universe_and_exclusions_never_hide_open_positions(monkeypatch, tmp_path):
    bot, tg, store, _ = bot_for(tmp_path, {'AAA': 0, 'USDC': 0, 'BBB': 0},
                                include_all=False, use_exclude_file=True,
                                exclude_file=str(tmp_path / 'excluded.txt'))
    (tmp_path / 'excluded.txt').write_text('AAAUSDT\n', encoding='utf-8')
    state = store.load()
    open_position(state, 'AAAUSDT')
    open_position(state, 'USDCUSDT')  # excluded from the normal universe as stable
    store.save(state)
    stub_signals(monkeypatch, {'AAAUSDT': res(buy=[BUY], sell=[SELL]),
                              'USDCUSDT': res(buy=[BUY], sell=[SELL])})
    summary = bot.run_once(now_ms=BASE_TIME + 15_000_000, universe=['BBBUSDT'])
    assert set(bot.client.requested[-1][0]) == {'AAAUSDT', 'USDCUSDT', 'BBBUSDT'}
    assert summary['repeat'] == 0 and summary['exit'] == 2 and len(tg.messages) == 2
    assert all('إشارة خروج' in text for text in tg.messages)


def test_manual_exclusion_keeps_position_exit_only_not_repeat(monkeypatch, tmp_path):
    bot, tg, store, _ = bot_for(tmp_path, {'AAA': 0, 'BBB': 0},
                                use_exclude_file=True,
                                exclude_file=str(tmp_path / 'excluded.txt'))
    (tmp_path / 'excluded.txt').write_text('AAAUSDT\n', encoding='utf-8')
    state = store.load()
    open_position(state)
    store.save(state)
    stub_signals(monkeypatch, {'AAAUSDT': res(buy=[BUY], sell=[SELL])})
    summary = bot.run_once(now_ms=BASE_TIME + 15_000_000)
    assert 'AAAUSDT' in bot.client.requested[-1][0]
    assert summary['repeat'] == 0 and summary['exit'] == 1
    assert len(tg.messages) == 1 and 'إشارة خروج' in tg.messages[0]


@pytest.mark.parametrize('broken', ['missing', 'malformed', 'array', 'empty'])
def test_bad_display_or_primary_file_does_not_interrupt_open_exits(monkeypatch, tmp_path, broken):
    bot, tg, store, path = bot_for(tmp_path, {'AAA': 0})
    state = store.load()
    open_position(state)
    store.save(state)
    if broken == 'missing':
        path.unlink()
    else:
        path.write_text({'malformed': '???', 'array': '[]', 'empty': '{}'}[broken], encoding='utf-8')
    stub_signals(monkeypatch, {'AAAUSDT': res(buy=[BUY], sell=[SELL])})
    summary = bot.run_once(now_ms=BASE_TIME + 15_000_000, universe=['AAAUSDT'])
    assert summary['ruling_filtered'] == 1 and summary['ruling_unavailable'] == 1
    assert summary['exit'] == 1 and len(tg.messages) == 1
    assert 'إشارة خروج' in tg.messages[0]
    assert store.load()['symbols']['AAAUSDT']['position'] is None


def test_gate_exception_or_wrong_return_is_always_fail_closed_and_exit_continues(tmp_path):
    bot, tg, store, _ = bot_for(tmp_path, {'AAA': 0})
    state = store.load()
    open_position(state)
    bot._entry_check = lambda sym: (_ for _ in ()).throw(RuntimeError('boom'))
    ev = bot._apply('AAAUSDT', res(buy=[BUY], sell=[SELL]), state)
    assert ev['ruling_unavailable'] == 1 and ev['exit'] == 1 and len(tg.messages) == 1
    state2 = store.load()
    bot._entry_check = lambda sym: True
    ev2 = bot._apply('BBBUSDT', res(buy=[BUY]), state2)
    assert ev2['entry'] == 0 and ev2['ruling_unavailable'] == 1


@pytest.mark.parametrize('frame,expected_label', [
    ('1h', '(1H)'), ('4h', '(4H)'), ('15m', '(15M)'),
])
def test_shared_core_gate_and_exit_for_each_frame_without_starting_any_workflow(
        monkeypatch, tmp_path, frame, expected_label):
    from nw_bot import build_nw_bot
    from src.nw_4h_bot import build_nw_4h_bot
    from src.nw_15m_bot import build_nw_15m_bot

    path = tmp_path / 'academy.json'
    primary(path, {'AAA': 0})
    monkeypatch.setenv('TELEGRAM_MUTE', '1')
    monkeypatch.setenv('NW_HALAL_PRIMARY', str(path))
    monkeypatch.setenv('NW_STATE_FILE', str(tmp_path / 'state.json'))
    monkeypatch.setenv('NW_TRADE_LOG', str(tmp_path / 'trades.csv'))
    builder = {'1h': build_nw_bot, '4h': build_nw_4h_bot, '15m': build_nw_15m_bot}[frame]
    bot = builder()
    tg = Telegram()
    bot.tg = tg
    assert bot.interval == frame
    state = bot.store.load()
    assert bot._apply('AAAUSDT', res(buy=[BUY]), state)['entry'] == 1
    assert expected_label in tg.messages[-1] and '✅ مباح' in tg.messages[-1]
    primary(path, {'AAA': 1})
    second = {**BUY, 'bar_time': BUY['bar_time'] + 3_600_000}
    ev = bot._apply('AAAUSDT', res(buy=[second], sell=[SELL]), state)
    assert ev['repeat'] == 0 and ev['exit'] == 1
    assert len(tg.messages) == 2 and expected_label in tg.messages[-1]
    assert 'إشارة خروج' in tg.messages[-1]


def test_only_running_signal_workflows_receive_scoped_github_token():
    for name, step_name, cron in [
        ('nw-bot.yml', 'Run NW bot once', '5 * * * *'),
        ('nw-bot-4h.yml', 'Run NW 4H bot once', '3 0,4,8,12,16,20 * * *'),
    ]:
        doc = yaml.safe_load((ROOT / '.github/workflows' / name).read_text(encoding='utf-8'))
        job = doc['jobs']['run-once']
        step = next(s for s in job['steps'] if s.get('name') == step_name)
        assert step['env']['NW_HALAL_GITHUB_TOKEN'] == '${{ secrets.GITHUB_TOKEN }}'
        assert 'NW_HALAL_GITHUB_TOKEN' not in job['env']
        assert job['env']['NW_INCLUDE_ALL'] == '1'
        assert any(x['cron'] == cron for x in doc.get('on', doc.get(True))['schedule'])
        assert doc['permissions']['contents'] == 'write'
    # The disabled 15M workflow and its trigger remain unchanged (also checked by
    # the protected-file test); the core itself still fails closed if later run.
    doc15 = yaml.safe_load((ROOT / '.github/workflows/nw-bot-15m.yml').read_text(encoding='utf-8'))
    assert doc15['jobs']['run-once']['if'] == "vars.NW15_ENABLED != 'false'"
    assert 'NW_HALAL_GITHUB_TOKEN' not in doc15['jobs']['run-once']['env']
