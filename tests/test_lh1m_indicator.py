"""Independent reference tests for the submitted Pine marker expressions."""
import json
import math
import random
from pathlib import Path

import pytest

from src.lh1m_indicator import Bar, MINUTE_MS, confirmed_labels, normalize_klines


def bars(n=182):
    b = [Bar(i * MINUTE_MS, 10, 8, 9, 1, 1) for i in range(n)]
    b[91] = Bar(91 * MINUTE_MS, 20, 1, 9, 1, 1)
    return b


def raw_from_bars(b):
    return [[x.open_ms, str(x.close), str(x.high), str(x.low), str(x.close),
             str(x.volume), x.open_ms + MINUTE_MS - 1, '0', x.trades, '0', '0', '0'] for x in b]


def test_requires_extra_mb_history_and_only_closed_confirmation():
    b = bars(182)
    assert confirmed_labels(b[:181]) == []  # high[181] not available yet
    labels = confirmed_labels(b)
    assert [a.kind for a in labels] == ['H', 'L']
    assert all(a.pivot_open_ms == 91 * MINUTE_MS for a in labels)
    assert all(a.confirmation_open_ms == 181 * MINUTE_MS for a in labels)
    assert all(a.confirmation_time_ms == 182 * MINUTE_MS for a in labels)
    assert all(a.confirmation_close == 9 for a in labels)  # not 1 or 20 at pivot
    # The 182nd candle is still open at 182m-1ms => cannot confirm either label.
    closed = normalize_klines(raw_from_bars(b), 182 * MINUTE_MS - 1)
    assert len(closed) == 181 and not confirmed_labels(closed)


def test_highestbars_lowestbars_newest_tie_wins():
    b = bars()
    # An equal older (left) extreme does not veto the newer candidate.
    b[10] = Bar(10 * MINUTE_MS, 20, 1, 9, 1, 1)
    assert [x.kind for x in confirmed_labels(b)] == ['H', 'L']
    # An equal newer (right) extreme wins and vetoes the older candidate.
    b = bars()
    b[100] = Bar(100 * MINUTE_MS, 20, 1, 9, 1, 1)
    assert confirmed_labels(b) == []


def test_cursor_is_confirmation_bar_not_the_pivot():
    b = bars()
    assert len(confirmed_labels(b, after_open_ms=180 * MINUTE_MS)) == 2
    assert not confirmed_labels(b, after_open_ms=181 * MINUTE_MS)


def test_no_future_data_influence_on_already_confirmed_label():
    b = bars()
    before = confirmed_labels(b)
    extended = b + [Bar(182 * MINUTE_MS, 999, .01, 9, 1, 1)]
    after = [x for x in confirmed_labels(extended) if x.confirmation_open_ms == 181 * MINUTE_MS]
    assert before == after


def test_independent_newest_offset_reference_on_many_ties():
    random.seed(43)
    for left, right in [(1, 1), (2, 3), (5, 2), (30, 30), (90, 90)]:
        series = []
        for i in range(300):
            high = random.randint(10, 15)
            low = random.randint(1, 6)
            series.append(Bar(i * MINUTE_MS, high, low, 8, 1, 1))
        observed = {(x.confirmation_open_ms // MINUTE_MS, x.kind)
                    for x in confirmed_labels(series, left=left, right=right)}
        expected = set()
        width = left + right + 1
        for i in range(width, len(series)):
            window = series[i - width + 1:i + 1]
            hs, ls = [x.high for x in window], [x.low for x in window]
            if len(hs) - 1 - hs[::-1].index(max(hs)) == left:
                expected.add((i, 'H'))
            if len(ls) - 1 - ls[::-1].index(min(ls)) == left:
                expected.add((i, 'L'))
        assert observed == expected


def test_pinned_pinets_v5_tie_fixture_matches_confirmation_and_pivot_price():
    fixture = json.loads((Path(__file__).parent / 'fixtures/lh1m_pinets_v5_ties.json').read_text())
    data = [Bar(i * MINUTE_MS, h, l, fixture['close'], 1, 1)
            for i, (h, l) in enumerate(fixture['high_low'])]
    actual = sorted([x.confirmation_open_ms // MINUTE_MS, x.kind, x.pivot_price]
                    for x in confirmed_labels(data, fixture['left'], fixture['right']))
    assert actual == sorted(fixture['expected'])


def test_pinned_pinets_90_90_flow_reference_matches_all_labels():
    fixture = json.loads((Path(__file__).parent / 'fixtures/lh1m_pinets_90_flow.json').read_text())
    data = [Bar(i * MINUTE_MS, h, l, (h + l) / 2, 1, 1)
            for i, (h, l) in enumerate(fixture['high_low'])]
    actual = sorted([x.confirmation_open_ms // MINUTE_MS, x.kind, x.pivot_price]
                    for x in confirmed_labels(data))  # production defaults must be 90/90
    assert actual == sorted(fixture['expected'])
    assert any(kind == 'H' and price == 0.01493 for _, kind, price in actual)


def test_gap_duplicate_nan_and_bad_ohlc_fail_closed():
    original = raw_from_bars(bars())
    with pytest.raises(ValueError, match='missing'):
        normalize_klines(original[:25] + original[26:], 100 * MINUTE_MS)
    with pytest.raises(ValueError, match='duplicate'):
        normalize_klines(original[:25] + [original[24]] + original[25:], 100 * MINUTE_MS)
    invalid = raw_from_bars(bars())
    invalid[2][2] = 'nan'
    with pytest.raises(ValueError, match='nonpositive'):
        normalize_klines(invalid, 100 * MINUTE_MS)
    invalid = raw_from_bars(bars())
    invalid[2][4] = '99'
    with pytest.raises(ValueError, match='inconsistent'):
        normalize_klines(invalid, 100 * MINUTE_MS)


def test_zero_volume_is_not_silently_misrepresented_as_execution():
    b = bars()
    b[-1] = Bar(b[-1].open_ms, 10, 8, 9, 0, 0)
    labels = confirmed_labels(b)
    assert labels[0].confirmation_trades == 0 and labels[0].confirmation_volume == 0
