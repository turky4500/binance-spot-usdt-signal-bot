from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class BinanceClient:
    INTERVAL_MS = {
        "1m": 60_000,
        "5m": 300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
        "4h": 14_400_000,
        "1d": 86_400_000,
    }

    def __init__(self, timeout: int = 20, max_workers: int = 10, base_url: str = "https://data-api.binance.vision") -> None:
        self.timeout = timeout
        self.max_workers = max_workers
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.7,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _normalize_klines(self, raw: list[list]) -> pd.DataFrame:
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

    def get_server_time(self) -> int:
        response = self.session.get(f"{self.base_url}/api/v3/time", timeout=self.timeout)
        response.raise_for_status()
        return int(response.json()["serverTime"])

    def get_spot_usdt_symbols(self, quote_asset: str = "USDT") -> list[str]:
        response = self.session.get(f"{self.base_url}/api/v3/exchangeInfo", timeout=self.timeout)
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
        response = self.session.get(f"{self.base_url}/api/v3/ticker/price", timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        return {row["symbol"]: float(row["price"]) for row in data}

    def get_klines(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 260,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> pd.DataFrame:
        params: dict[str, int | str] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = int(end_time)

        response = self.session.get(
            f"{self.base_url}/api/v3/klines",
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return self._normalize_klines(response.json())

    def get_klines_range(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        limit: int = 1000,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        cursor = int(start_time)
        interval_ms = self.INTERVAL_MS.get(interval)
        if interval_ms is None:
            raise ValueError(f"Unsupported interval for range fetch: {interval}")

        while cursor <= end_time:
            chunk = self.get_klines(
                symbol=symbol,
                interval=interval,
                limit=min(limit, 1000),
                start_time=cursor,
                end_time=end_time,
            )
            if chunk.empty:
                break
            frames.append(chunk)

            next_cursor = int(chunk.iloc[-1]["close_time"]) + 1
            if next_cursor <= cursor:
                next_cursor = int(chunk.iloc[-1]["open_time"]) + interval_ms
            if next_cursor <= cursor:
                break
            cursor = next_cursor

            if len(chunk) < min(limit, 1000):
                break

        if not frames:
            return pd.DataFrame()
        combined = pd.concat(frames, ignore_index=True)
        combined = combined.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
        return combined

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
