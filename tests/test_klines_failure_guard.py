import pandas as pd

from src.binance_client import BinanceClient


def _df():
    return pd.DataFrame({"open_time": [1], "close_time": [2], "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]})


def test_failed_symbol_is_retried_and_recovered(monkeypatch):
    client = BinanceClient(max_workers=2)
    calls = {"AAA": 0}

    def fake(symbol, interval, limit):
        if symbol == "AAA":
            calls["AAA"] += 1
            if calls["AAA"] == 1:
                raise RuntimeError("boom")
        return _df()

    monkeypatch.setattr(client, "get_klines", fake)
    monkeypatch.setattr("src.binance_client.time.sleep", lambda s: None)
    res = client.get_klines_for_symbols(["AAA", "BBB"], "4h", 10)
    assert not res["AAA"].empty and not res["BBB"].empty
    assert client.last_failed_symbols == []


def test_persistent_failure_is_reported(monkeypatch):
    client = BinanceClient(max_workers=2)

    def fake(symbol, interval, limit):
        if symbol == "AAA":
            raise RuntimeError("down")
        return _df()

    monkeypatch.setattr(client, "get_klines", fake)
    monkeypatch.setattr("src.binance_client.time.sleep", lambda s: None)
    res = client.get_klines_for_symbols(["AAA", "BBB"], "4h", 10)
    assert res["AAA"].empty and not res["BBB"].empty
    assert client.last_failed_symbols == ["AAA"]
