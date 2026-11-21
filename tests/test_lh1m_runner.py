"""Runner reports actual monitoring failures and frees the recovery slot."""
import requests

from run_lh1m import watch
from src.lh1m_bot import DurableStoreError


class Bot:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {'sent': 0, 'uncertain': 0}
        self.error = error
        self.calls = 0
    def cycle(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def test_one_clean_cycle_returns_success():
    bot = Bot()
    assert watch(bot, 0) == 0 and bot.calls == 1


def test_one_failed_cycle_and_durable_failure_are_not_green():
    for err in (requests.ConnectionError('no market data'), DurableStoreError('state')):
        bot = Bot(error=err)
        assert watch(bot, 0) == 2 and bot.calls == 1


def test_unresolved_send_exits_immediately_so_recovery_can_run():
    for result in ({'sent': 0, 'uncertain': 1}, {'paused_uncertain': True}):
        bot = Bot(result=result)
        assert watch(bot, 3000) == 2 and bot.calls == 1


def test_stale_rulings_and_all_pairs_missing_do_not_report_success():
    assert watch(Bot(result={'rulings_fresh': False, 'eligible_halal': 0, 'invalid': 0}), 0) == 2
    assert watch(Bot(result={'rulings_fresh': True, 'eligible_halal': 10, 'invalid': 10}), 0) == 2
    assert watch(Bot(result={'rulings_fresh': True, 'eligible_halal': 10, 'invalid': 1}), 0) == 0


def test_three_consecutive_cycle_errors_fail_the_workflow(monkeypatch):
    bot = Bot(error=requests.ConnectionError('no market data'))
    monkeypatch.setattr('run_lh1m.time.sleep', lambda _: None)
    assert watch(bot, 3000) == 2 and bot.calls == 3
