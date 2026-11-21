"""No-network integration tests for entry, exit, halal, state and daily reports."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest
import requests

from src.lh1m_bot import DurableStoreError, Lh1mBot, day_of, empty_state
from src.lh1m_github import HalalSnapshot
from src.lh1m_indicator import MINUTE_MS
from src.lh1m_telegram import SignalDeadlineExpired

BASE = int(datetime(2026, 11, 5, 10, tzinfo=timezone.utc).timestamp() * 1000)


class Clock:
    def __init__(self, t=BASE + 62 * MINUTE_MS + 5000): self.t = t
    def __call__(self): return self.t


class Store:
    def __init__(self, state=None, fail_at=None):
        self.state, self.calls, self.fail_at = copy.deepcopy(state), 0, fail_at
    def load(self): return copy.deepcopy(self.state)
    def save(self, state):
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError('simulated durable failure')
        self.state = copy.deepcopy(state)


class Provider:
    def __init__(self, snapshot): self.snapshot = snapshot
    def get(self): return self.snapshot


class Telegram:
    muted = False
    def __init__(self, fail_after_accept=False):
        self.texts = []
        self.fail_after_accept = fail_after_accept
    def send_message(self, text):
        self.texts.append(text)
        if self.fail_after_accept:
            self.fail_after_accept = False
            raise requests.Timeout('simulated ambiguous response')
    def send_before(self, text, deadline_ms):
        self.send_message(text)


def candles():
    out = []
    for i in range(63):
        high, low, close = 10.0, 8.0, 9.0
        if i == 31: low = 1.0      # L confirmed at candle 61
        if i == 32: high = 20.0    # H confirmed at candle 62
        if i == 62: close = 9.9
        t = BASE + i * MINUTE_MS
        out.append([t, str(close), str(high), str(low), str(close), '1',
                    t + MINUTE_MS - 1, '1', 1, '0', '0', '0'])
    return out


class Market:
    def __init__(self, clock, symbols=None, frames=None):
        self.clock = clock
        self.symbols = symbols if symbols is not None else ['AAAUSDT', 'BBBUSDT', 'CCCUSDT']
        self.frames = frames if frames is not None else {'AAAUSDT': candles(), 'BBBUSDT': candles()}
        self.fetched = []
    def server_time_ms(self): return self.clock()
    def active_spot_usdt_symbols(self): return list(self.symbols)
    def candles_for_symbols(self, symbols):
        self.fetched.extend(symbols)
        return {s: self.frames[s] for s in symbols if s in self.frames}


def setup_bot(*, snapshot=None, store=None, telegram=None, clock=None, market=None):
    clock = clock or Clock()
    snapshot = snapshot or HalalSnapshot({'AAA': 0, 'BBB': 1, 'USDT': 0}, frozenset(), clock())
    store, telegram = store or Store(), telegram or Telegram()
    market = market or Market(clock)
    bot = Lh1mBot(market, telegram, store, Provider(snapshot), clock=clock)
    return bot, store, telegram, clock, market


def test_fresh_spot_halal_L_entry_then_H_exit_with_reference_prices_and_no_duplicates():
    bot, store, tg, clock, market = setup_bot()
    first = bot.cycle()
    assert first['eligible_halal'] == 1 and market.fetched == ['AAAUSDT']
    assert len(tg.texts) == 1 and '📥 توصية دخول — AAAUSDT' in tg.texts[0]
    assert 'إغلاق شمعة التأكيد: 9.00' in tg.texts[0]
    pos = bot.state['positions']['AAAUSDT']
    assert pos['entry_price'] == 9 and pos['entry_time_ms'] == BASE + 62 * MINUTE_MS
    bot.cycle()
    assert len(tg.texts) == 1
    clock.t += MINUTE_MS
    bot.cycle()
    assert len(tg.texts) == 2 and '🔴 إشارة خروج — AAAUSDT' in tg.texts[1]
    assert '9.90' in tg.texts[1] and '+9.80%' in tg.texts[1]
    assert 'AAAUSDT' not in bot.state['positions']
    counts = bot.state['daily'][day_of(clock())]
    assert counts['entries'] == 1 and counts['closed'] == 1 and counts['wins'] == 1
    assert counts['net_sum'] == pytest.approx(9.8)
    again = Lh1mBot(market, tg, store, bot.provider, clock=clock)
    again.cycle()
    assert len(tg.texts) == 2


def test_repeated_L_does_not_create_a_second_position():
    bot, _, tg, clock, _ = setup_bot()
    bot.cycle()
    original = copy.deepcopy(bot.state['positions']['AAAUSDT'])
    e = {'id': 'AAAUSDT:L:later', 'type': 'signal', 'symbol': 'AAAUSDT', 'kind': 'L',
         'confirmation_time_ms': clock(), 'confirmation_open_ms': clock() - MINUTE_MS,
         'confirmation_close': 9.7, 'confirmation_trades': 1, 'confirmation_volume': 1,
         'status': 'pending', 'created_ms': clock()}
    bot.state['events'][e['id']] = e
    bot._save()
    bot._dispatch(bot.provider.get())
    pos = bot.state['positions']['AAAUSDT']
    assert pos['entry_price'] == original['entry_price'] and pos['entry_id'] == original['entry_id']
    assert pos['entry_count'] == 2 and 'إشارة دخول ثانية' in tg.texts[-1]


def test_stale_unknown_or_ambiguous_rulings_block_all_LH():
    clock = Clock()
    stale = HalalSnapshot({'AAA': 0, 'USDT': 0}, frozenset(), clock() - 4_500_001)
    bot, _, tg, _, market = setup_bot(clock=clock, snapshot=stale)
    out = bot.cycle()
    assert not out['rulings_fresh'] and out['eligible_halal'] == 0
    assert not market.fetched and len(tg.texts) == 1 and 'توقفت التوصيات' in tg.texts[0]
    bot.cycle()
    assert len(tg.texts) == 1  # warning not repeated with unchanged stale check
    for snap in (HalalSnapshot({'AAA': 0, 'USDT': 1}, frozenset(), clock()),
                 HalalSnapshot({'AAA': 0, 'USDT': 0}, frozenset({'AAA'}), clock()),
                 HalalSnapshot({'AAA': 0, 'USDT': 0}, frozenset({'USDT'}), clock())):
        b, _, t, _, _ = setup_bot(clock=clock, snapshot=snap)
        b.cycle()
        assert not any('توصية دخول' in x or 'إشارة خروج' in x for x in t.texts)


def test_bad_market_candles_do_not_generate_any_messages():
    clock = Clock()
    gaps = candles()
    del gaps[20]
    market = Market(clock, symbols=['AAAUSDT'], frames={'AAAUSDT': gaps})
    bot, _, tg, _, _ = setup_bot(clock=clock, market=market)
    out = bot.cycle()
    assert out['invalid'] == 1 and not tg.texts


def test_pending_signal_is_not_sent_if_pair_no_longer_active_on_spot():
    clock = Clock()
    market = Market(clock, symbols=[], frames={})
    bot, _, tg, _, _ = setup_bot(clock=clock, market=market)
    e = {'id': 'AAAUSDT:L:pending', 'type': 'signal', 'symbol': 'AAAUSDT', 'kind': 'L',
         'confirmation_time_ms': clock(), 'confirmation_open_ms': clock() - MINUTE_MS,
         'confirmation_close': 9, 'confirmation_trades': 1, 'confirmation_volume': 1,
         'status': 'pending', 'created_ms': clock()}
    bot.state['events'][e['id']] = e
    bot._save()
    bot.cycle()
    assert e['status'] == 'blocked_market' and not tg.texts and not bot.state['positions']


def test_H_without_recorded_entry_is_still_sent_without_inventing_a_trade():
    bot, _, tg, clock, _ = setup_bot()
    e = {'id': 'AAAUSDT:H:only', 'type': 'signal', 'symbol': 'AAAUSDT', 'kind': 'H',
         'confirmation_time_ms': clock(), 'confirmation_open_ms': clock() - MINUTE_MS,
         'confirmation_close': 9, 'confirmation_trades': 1, 'confirmation_volume': 1,
         'status': 'pending', 'created_ms': clock()}
    bot.state['events'][e['id']] = e
    bot._dispatch(bot.provider.get())
    assert e['status'] == 'sent' and len(tg.texts) == 1
    assert 'إشارة خروج' in tg.texts[0] and 'لا توجد توصية دخول' in tg.texts[0]
    assert not bot.state['positions']
    daily = bot.state['daily'][day_of(clock())]
    assert daily['unmatched_exits'] == 1 and daily['closed'] == 0 and daily['net_sum'] == 0


def test_simultaneous_H_L_is_ordered_H_first_and_does_not_invent_previous_entry():
    bot, _, tg, clock, _ = setup_bot()
    for kind in ('L', 'H'):  # input order intentionally reversed
        event = {'id': f'AAAUSDT:{kind}:same', 'type': 'signal', 'symbol': 'AAAUSDT',
                 'kind': kind, 'confirmation_time_ms': clock(),
                 'confirmation_open_ms': clock() - MINUTE_MS, 'confirmation_close': 9,
                 'confirmation_trades': 1, 'confirmation_volume': 1,
                 'status': 'pending', 'created_ms': clock()}
        bot.state['events'][event['id']] = event
    bot._dispatch(bot.provider.get())
    assert len(tg.texts) == 2
    assert 'إشارة خروج' in tg.texts[0] and 'لا توجد توصية دخول' in tg.texts[0]
    assert 'توصية دخول' in tg.texts[1]
    assert bot.state['positions']['AAAUSDT']['entry_id'] == 'AAAUSDT:L:same'
    daily = bot.state['daily'][day_of(clock())]
    assert daily['unmatched_exits'] == 1 and daily['entries'] == 1 and daily['closed'] == 0


def test_failure_before_outbox_commit_never_sends():
    store = Store(fail_at=2)  # initial state succeeds, queue commit fails
    bot, _, tg, _, _ = setup_bot(store=store)
    with pytest.raises(DurableStoreError):
        bot.cycle()
    assert not tg.texts and not store.state['events']


def test_failure_after_telegram_acceptance_becomes_uncertain_no_resend_on_restart():
    store = Store(fail_at=4)  # initial, queue, sending succeed; final ack save fails
    bot, _, tg, clock, market = setup_bot(store=store)
    with pytest.raises(DurableStoreError):
        bot.cycle()
    assert len(tg.texts) == 1
    assert next(iter(store.state['events'].values()))['status'] == 'sending'
    store.fail_at = None
    restarted = Lh1mBot(market, tg, store, bot.provider, clock=clock)
    assert next(iter(restarted.state['events'].values()))['status'] == 'uncertain'
    assert restarted.cycle()['paused_uncertain'] is True
    assert len(tg.texts) == 1


def test_network_timeout_requires_review_then_can_resolve_without_duplicate():
    tg = Telegram(fail_after_accept=True)
    bot, _, _, clock, _ = setup_bot(telegram=tg)
    out = bot.cycle()
    assert out['uncertain'] == 1 and len(tg.texts) == 1
    event = next(iter(bot.state['events'].values()))
    assert event['status'] == 'uncertain' and not bot.state['positions']
    assert bot.cycle()['paused_uncertain'] and len(tg.texts) == 1
    bot.resolve(event['id'], was_sent=True)
    assert bot.state['positions']['AAAUSDT']['entry_price'] == 9
    assert bot.state['daily'][day_of(clock())]['entries'] == 1
    bot.cycle()
    assert len(tg.texts) == 1


def test_daily_report_previous_riyadh_day_exactly_once_and_not_cumulative():
    t = int(datetime(2026, 11, 5, 21, 0, 15, tzinfo=timezone.utc).timestamp() * 1000)
    clock = Clock(t)
    state = empty_state(t - 86_400_000)
    state['last_heartbeat_ms'] = t - MINUTE_MS  # continuous monitoring until report time
    previous = day_of(t - 86_400_000)
    state['daily'][previous] = {'entries': 3, 'closed': 2, 'wins': 1, 'losses': 1,
                                'breakeven': 0, 'net_sum': 2.5}
    store, tg = Store(state), Telegram()
    market = Market(clock, symbols=['AAAUSDT'], frames={})
    bot, _, _, _, _ = setup_bot(clock=clock, market=market, store=store, telegram=tg)
    bot.cycle(); bot.cycle()
    assert len(tg.texts) == 1
    assert previous in tg.texts[0] and 'توصيات الدخول المرسلة: 3' in tg.texts[0]
    assert '50.0%' in tg.texts[0] and '+2.50%' in tg.texts[0]
    assert bot.state['reports'][previous] == 'sent'


def test_deadline_expiring_during_telegram_pacing_drops_signal_without_uncertain_send():
    clock = Clock()
    class ExpiringTelegram(Telegram):
        def send_before(self, text, deadline_ms):
            clock.t = deadline_ms + 1
            raise SignalDeadlineExpired('before any accepted HTTP request')
    tg = ExpiringTelegram()
    bot, store, _, _, _ = setup_bot(clock=clock, telegram=tg)
    out = bot.cycle()
    event = next(e for e in bot.state['events'].values() if e['type'] == 'signal')
    assert not tg.texts and not bot.state['positions']
    assert event['status'] == 'expired' and out['uncertain'] == 0
    assert store.state['events'][event['id']]['status'] == 'expired'


def test_halal_snapshot_expiring_during_pacing_also_blocks_signal():
    clock = Clock()
    class ExpiringTelegram(Telegram):
        def send_before(self, text, deadline_ms):
            clock.t = deadline_ms + 1
            raise SignalDeadlineExpired('before any accepted HTTP request')
    snap = HalalSnapshot({'AAA': 0, 'USDT': 0}, frozenset(), clock() - 4_499_999)
    bot, _, tg, _, _ = setup_bot(clock=clock, snapshot=snap, telegram=ExpiringTelegram())
    out = bot.cycle()
    event = next(e for e in bot.state['events'].values() if e['type'] == 'signal')
    assert not tg.texts and not bot.state['positions']
    assert event['status'] == 'blocked_ruling' and out['uncertain'] == 0


def test_zero_volume_candle_warning_is_present():
    clock = Clock()
    data = candles()
    data[61][5], data[61][8] = '0', 0
    market = Market(clock, symbols=['AAAUSDT'], frames={'AAAUSDT': data})
    bot, _, tg, _, _ = setup_bot(clock=clock, market=market)
    bot.cycle()
    assert 'شمعة التأكيد بلا تداول' in tg.texts[0]


def extend_candles(data, through):
    for i in range(len(data), through):
        t = BASE + i * MINUTE_MS
        data.append([t, '9', '10', '8', '9', '1', t + MINUTE_MS - 1, '1', 1, '0', '0', '0'])
    return data


def test_missed_exit_warns_without_creating_a_backdated_close():
    bot, _, tg, clock, market = setup_bot()
    bot.cycle()  # initial L
    clock.t += 5 * MINUTE_MS
    market.frames['AAAUSDT'] = extend_candles(candles(), 69)
    out = bot.cycle()
    assert out['missed_exit_warnings'] == 1
    assert any('إشارة H فائتة — AAAUSDT' in text for text in tg.texts)
    assert not any('🔴 إشارة خروج' in text for text in tg.texts)
    assert bot.state['positions']['AAAUSDT']['missed_exit_notified'] is True
    assert bot.state['daily'][day_of(clock())]['closed'] == 0
    n = len(tg.texts)
    bot.cycle()
    assert len(tg.texts) == n


def test_monitoring_gap_warns_without_claiming_all_exits_were_seen():
    bot, _, tg, clock, market = setup_bot()
    bot.cycle()
    clock.t += 10 * MINUTE_MS
    market.frames['AAAUSDT'] = extend_candles(candles(), 74)
    bot.cycle()
    assert any('فجوة في متابعة نظام L/H' in text for text in tg.texts)
    assert bot.state['last_heartbeat_ms'] == clock()


def test_changed_halal_ruling_warns_and_suspends_previously_open_position():
    bot, _, tg, clock, market = setup_bot()
    bot.cycle()
    bot.provider.snapshot = HalalSnapshot({'AAA': 1, 'USDT': 0}, frozenset(), clock())
    clock.t += MINUTE_MS
    bot.cycle()
    assert any('توصيات سابقة لم تعد مستوفية للمتابعة' in text and 'AAAUSDT' in text for text in tg.texts)
    assert 'AAAUSDT' in bot.state['positions']
    assert not any('🔴 إشارة خروج' in text for text in tg.texts)


def test_coverage_miss_survives_worker_restart_before_second_miss():
    bot, store, tg, clock, market = setup_bot()
    bot.cycle()  # creates an open AAAUSDT position
    market.frames.pop('AAAUSDT')
    clock.t += MINUTE_MS; bot.cycle()
    assert store.state['coverage_misses']['AAAUSDT'] == 1
    restarted = Lh1mBot(market, tg, store, bot.provider, clock=clock)
    clock.t += MINUTE_MS; restarted.cycle()
    assert sum('تعذرت متابعة الزوج المفتوح' in t for t in tg.texts) == 1


def test_open_position_missing_two_market_cycles_produces_one_safety_warning():
    bot, _, tg, clock, market = setup_bot()
    bot.cycle()
    market.frames.pop('AAAUSDT')
    clock.t += MINUTE_MS; bot.cycle()
    assert not any('تعذرت متابعة الزوج المفتوح' in t for t in tg.texts)
    clock.t += MINUTE_MS; bot.cycle()
    assert sum('تعذرت متابعة الزوج المفتوح' in t for t in tg.texts) == 1
    clock.t += MINUTE_MS; bot.cycle()
    assert sum('تعذرت متابعة الزوج المفتوح' in t for t in tg.texts) == 1
    market.frames['AAAUSDT'] = extend_candles(candles(), 72)
    clock.t += MINUTE_MS; bot.cycle()
    market.frames.pop('AAAUSDT')
    clock.t += MINUTE_MS; bot.cycle()
    clock.t += MINUTE_MS; bot.cycle()
    assert sum('تعذرت متابعة الزوج المفتوح' in t for t in tg.texts) == 2
