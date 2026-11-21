"""Read-only Binance Spot public market data; no API keys and no order calls."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

LOG = logging.getLogger(__name__)


class SpotMarket:
    def __init__(self, base_url: str = "https://data-api.binance.vision",
                 workers: int = 10, timeout: int = 8, universe_ttl: int = 300) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("market URL must use HTTPS")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.universe_ttl = universe_ttl
        self.local = threading.local()
        self.pool = ThreadPoolExecutor(max_workers=max(1, min(20, workers)))
        self.lock = threading.Lock()
        self.cooldown_until = 0.0
        self.symbols: list[str] = []
        self.symbols_at = 0.0

    def close(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)

    def _session(self) -> requests.Session:
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
            self.local.session.headers["User-Agent"] = "isolated-lh1m-signal-bot"
        return self.local.session

    def _get(self, path: str, params: dict | None = None) -> Any:
        with self.lock:
            if time.monotonic() < self.cooldown_until:
                raise RuntimeError("Binance rate-limit cooldown active")
        response = self._session().get(self.base_url + path, params=params, timeout=self.timeout)
        if response.status_code in (418, 429):
            try:
                wait = float(response.headers.get("Retry-After", "60"))
            except ValueError:
                wait = 60.0
            with self.lock:
                self.cooldown_until = max(self.cooldown_until, time.monotonic() + max(wait, 60.0))
            raise RuntimeError(f"Binance HTTP {response.status_code}; respecting cooldown")
        response.raise_for_status()
        return response.json()

    def server_time_ms(self) -> int:
        return int(self._get("/api/v3/time")["serverTime"])

    def active_spot_usdt_symbols(self, force: bool = False) -> list[str]:
        now = time.monotonic()
        if not force and self.symbols and now - self.symbols_at < self.universe_ttl:
            return list(self.symbols)
        payload = self._get("/api/v3/exchangeInfo")
        raw = payload.get("symbols") if isinstance(payload, dict) else None
        if not isinstance(raw, list) or not raw:
            raise RuntimeError("incomplete exchangeInfo; refusing to infer spot universe")
        symbols: set[str] = set()
        for item in raw:
            if (item.get("quoteAsset") == "USDT" and item.get("status") == "TRADING"
                    and item.get("isSpotTradingAllowed") is True):
                sym, base = str(item.get("symbol") or ""), str(item.get("baseAsset") or "")
                if base and sym == base + "USDT":
                    symbols.add(sym)
        if not symbols:
            raise RuntimeError("no verified active Binance spot-USDT pairs")
        self.symbols, self.symbols_at = sorted(symbols), now
        return list(self.symbols)

    def _candles(self, symbol: str) -> list:
        data = self._get("/api/v3/klines", {"symbol": symbol, "interval": "1m", "limit": 100})
        if not isinstance(data, list):
            raise RuntimeError("unexpected klines response")
        return data

    def candles_for_symbols(self, symbols: list[str]) -> dict[str, list]:
        futures = {self.pool.submit(self._candles, sym): sym for sym in symbols}
        result: dict[str, list] = {}
        failed = 0
        for future in as_completed(futures):
            sym = futures[future]
            try:
                result[sym] = future.result()
            except Exception as exc:
                failed += 1
                LOG.warning("No candles for %s: %s", sym, exc)
        if failed:
            LOG.warning("%d/%d symbols not evaluated this cycle", failed, len(symbols))
        return result
