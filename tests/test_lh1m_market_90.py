"""The 90/90 worker must fetch enough bars for confirmation and gap warnings."""
from src.lh1m_indicator import LEFT_BARS, RIGHT_BARS, MIN_CLOSED_BARS
from src.lh1m_market import SpotMarket


def test_market_fetch_retains_history_for_90_right_bars(monkeypatch):
    market = SpotMarket(workers=1)
    calls = []
    try:
        monkeypatch.setattr(market, '_get', lambda path, params=None: calls.append((path, params)) or [])
        assert market._candles('FLOWUSDT') == []
        path, params = calls[0]
        assert path == '/api/v3/klines' and params['interval'] == '1m'
        assert LEFT_BARS == 90 and RIGHT_BARS == 90 and MIN_CLOSED_BARS == 182
        assert params['limit'] >= MIN_CLOSED_BARS + 35 + 1
    finally:
        market.close()
