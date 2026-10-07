"""جلب وتخزين بيانات الشموع للاختبار — مع كاش محلي لتسريع التجارب المتكررة."""
from __future__ import annotations

import gzip
import io
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

BASE_URL = "https://data-api.binance.vision"
CACHE_ROOT = Path(__file__).resolve().parent / "data_cache"

INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time"]


def _cache_path(interval: str, symbol: str) -> Path:
    return CACHE_ROOT / interval / f"{symbol}.csv.gz"


def _read_cache(interval: str, symbol: str) -> pd.DataFrame | None:
    path = _cache_path(interval, symbol)
    if not path.exists():
        return None
    try:
        return pd.read_csv(path, compression="gzip")
    except Exception:
        return None


def _write_cache(interval: str, symbol: str, df: pd.DataFrame) -> None:
    path = _cache_path(interval, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, compression="gzip")


def fetch_klines(symbol: str, interval: str = "1h", bars: int = 2000, session: requests.Session | None = None) -> pd.DataFrame:
    """يجلب عددًا محددًا من الشموع المغلقة (أحدث bars شمعة)."""
    step = INTERVAL_MS[interval]
    end_time = int(time.time() * 1000 // step * step) - 1  # آخر شمعة مغلقة
    start_time = end_time - (bars * step)
    rows: list[list] = []
    cursor = start_time
    sess = session or requests
    while cursor < end_time:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_time,
            "limit": 1000,
        }
        resp = sess.get(f"{BASE_URL}/api/v3/klines", params=params, timeout=20)
        resp.raise_for_status()
        chunk = resp.json()
        if not chunk:
            break
        rows.extend(chunk)
        last_open = chunk[-1][0]
        if last_open <= cursor:
            break
        cursor = last_open + step
    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time", "qav",
        "trades", "tbb", "tbq", "ignore",
    ])
    df = df[COLUMNS].copy()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce").astype("int64")
    df["close_time"] = pd.to_numeric(df["close_time"], errors="coerce").astype("int64")
    df = df.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)
    # نُسقط آخر شمعة إن كانت ما زالت جارية
    if len(df) and int(df.iloc[-1]["close_time"]) > int(time.time() * 1000):
        df = df.iloc[:-1].reset_index(drop=True)
    return df


def load_universe(interval: str = "1h", bars: int = 2000, symbols: list[str] | None = None, workers: int = 16, refresh: bool = False) -> dict[str, pd.DataFrame]:
    """يحمّل كل الرموز مع استخدام الكاش. الرموز التي فشل جلبها تُتجاهل."""
    if symbols is None:
        symbols = _spot_usdt_symbols()
    out: dict[str, pd.DataFrame] = {}
    to_fetch: list[str] = []

    for symbol in symbols:
        if not refresh:
            cached = _read_cache(interval, symbol)
            if cached is not None and len(cached) >= bars * 0.9:
                out[symbol] = cached
                continue
        to_fetch.append(symbol)

    if to_fetch:
        with requests.Session() as session:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(fetch_klines, s, interval, bars, session): s for s in to_fetch}
                for i, fut in enumerate(as_completed(futures), 1):
                    symbol = futures[fut]
                    try:
                        df = fut.result()
                    except Exception:
                        continue
                    if df.empty or len(df) < 300:
                        continue
                    _write_cache(interval, symbol, df)
                    out[symbol] = df
                    if i % 100 == 0:
                        print(f"  fetched {i}/{len(to_fetch)}", file=sys.stderr)
    return out


def _spot_usdt_symbols() -> list[str]:
    resp = requests.get(f"{BASE_URL}/api/v3/exchangeInfo", timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return sorted(
        s["symbol"] for s in data["symbols"]
        if s["status"] == "TRADING" and s["quoteAsset"] == "USDT" and s.get("isSpotTradingAllowed")
    )
