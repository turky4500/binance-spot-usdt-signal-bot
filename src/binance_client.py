from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

import pandas as pd
import requests


class BinanceClient:
    BASE_URL = "https://api.binance.com"

    def __init__(self, timeout: int = 20, max_workers: int = 10) -> None:
        self.timeout = timeout
        self.max_workers = max_workers
        self.session = requests.Session()

    def get_server_time(self) -> int:
        response = self.session.get(f"{self.BASE_URL}/api/v3/time", timeout=self.timeout)
        response.raise_for_status()
        return int(response.json()["serverTime"])

    def get_spot_usdt_symbols(self, quote_asset: str = "USDT") -> list[str]:
        response = self.session.get(f"{self.BASE_URL}/api/v3/exchangeInfo", timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        symbols: list[str] = []
        for item in payload.get("symbols", []):
            if item.get("quoteAsset") != quote_asset:
                continue
            if item.get("status") != "TRADING":
                continue
            if not item.get("isSpotTradingAllowed", False):
                continue
            symbols.append(item["symbol"])
        return sorted(symbols)

    def get_all_prices(self) -> dict[str, float]:
        response = self.session.get(f"{self.BASE_URL}/api/v3/ticker/price", timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        return {row["symbol"]: float(row["price"]) for row in data}

    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 260) -> pd.DataFrame:
        response = self.session.get(
            f"{self.BASE_URL}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=self.timeout,
        )
        response.raise_for_status()
        raw = response.json()
        columns = [
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_asset_volume",
            "num_trades",
            "taker_buy_base_volume",
            "taker_buy_quote_volume",
            "ignore",
        ]
        df = pd.DataFrame(raw, columns=columns)
        if df.empty:
            return df
        numeric_cols = [
            "open",
            "high",
            "low",
            "close",
            "volume",
            "quote_asset_volume",
            "taker_buy_base_volume",
            "taker_buy_quote_volume",
        ]
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        int_cols = ["open_time", "close_time", "num_trades"]
        for col in int_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
        return df

    def get_klines_for_symbols(self, symbols: Iterable[str], interval: str = "1h", limit: int = 260) -> dict[str, pd.DataFrame]:
        results: dict[str, pd.DataFrame] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_map = {
                executor.submit(self.get_klines, symbol, interval, limit): symbol
                for symbol in symbols
            }
            for future in as_completed(future_map):
                symbol = future_map[future]
                try:
                    results[symbol] = future.result()
                except Exception:
                    results[symbol] = pd.DataFrame()
        return results
