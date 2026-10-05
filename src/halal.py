"""جلب الحكم الشرعي للعملات من cryptohalal.cc مع تخزين مؤقت وخروج آمن.

الهدف هنا مشابه للمستودع المرجعي للمستخدم:
- مصدر الأحكام: https://api.cryptohalal.cc/api/coins
- إذا فشل الجلب لا تتعطل الإشارات أبدًا
- نعرض في الرسالة أحد النصوص التالية:
  ✅ مباح
  ⛔ غير مباح
  ⚠️ مشبوه
  ℹ️ لا يوجد حكم
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Optional

HALAL_API_URL = "https://api.cryptohalal.cc/api/coins"
HALAL_CACHE_FILE = "halal_verdicts.json"

VERDICT_TEXTS = {
    0: "✅ مباح",
    1: "⛔ غير مباح",
    2: "⚠️ مشبوه",
}
NO_RULING = "ℹ️ لا يوجد حكم"

_QUOTE_SUFFIXES = ("USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI")


def _base_symbol(symbol: str) -> str:
    sym = (symbol or "").strip().upper()
    for suffix in _QUOTE_SUFFIXES:
        if sym.endswith(suffix) and sym != suffix:
            return sym[: -len(suffix)]
    return sym


def _cache_path(data_dir: str) -> str:
    return os.path.join(data_dir, HALAL_CACHE_FILE)


def _parse_items(payload: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        items = (payload.get("data") or {}).get("items") or []
        if not isinstance(items, list):
            return out
    except AttributeError:
        return out

    for item in items:
        symbol = str(item.get("symbol") or "").strip().upper()
        judgement = item.get("judgement")
        if symbol and isinstance(judgement, int) and judgement in VERDICT_TEXTS:
            out[symbol] = judgement
    return out


def load_cached_verdicts(data_dir: str) -> dict[str, int] | None:
    try:
        with open(_cache_path(data_dir), encoding="utf-8") as f:
            data = json.load(f)
        judgements = data.get("judgements")
        return judgements if isinstance(judgements, dict) else None
    except (OSError, ValueError):
        return None


def _save_cache(data_dir: str, verdicts: dict[str, int], fetched_at_ms: int) -> None:
    os.makedirs(data_dir, exist_ok=True)
    payload = {
        "vendor": "cryptohalal.cc",
        "fetched_at_ms": fetched_at_ms,
        "note": "مصدر الأحكام: CryptoHalal.cc",
        "judgements": verdicts,
    }
    with open(_cache_path(data_dir), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _merge_into_cache(data_dir: str, extra: dict[str, int]) -> None:
    try:
        with open(_cache_path(data_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {
            "vendor": "cryptohalal.cc",
            "fetched_at_ms": int(time.time() * 1000),
            "note": "مصدر الأحكام: CryptoHalal.cc",
            "judgements": {},
        }

    judgements = data.get("judgements")
    if not isinstance(judgements, dict):
        judgements = {}
        data["judgements"] = judgements

    judgements.update(extra)
    with open(_cache_path(data_dir), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _urlopen_json(url: str, timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _search_symbol(base_symbol: str, timeout: int = 8, retries: int = 1) -> Optional[int]:
    if not base_symbol:
        return None

    url = f"{HALAL_API_URL}?search={urllib.parse.quote(base_symbol)}"
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(0.3)
        try:
            payload = _urlopen_json(url, timeout=timeout)
        except Exception:
            continue
        return _parse_items(payload).get(base_symbol)
    return None


def fetch_verdicts(max_pages: int = 3, timeout: int = 20) -> dict[str, int] | None:
    verdicts: dict[str, int] = {}
    try:
        for page in range(1, max_pages + 1):
            payload = _urlopen_json(f"{HALAL_API_URL}?page={page}&per_page=25", timeout=timeout)
            verdicts.update(_parse_items(payload))
            meta = (payload.get("data") or {}).get("meta") or {}
            current_page = meta.get("current_page")
            last_page = meta.get("last_page")
            if current_page and last_page and int(current_page) >= int(last_page):
                break
    except Exception:
        return None
    return verdicts or None


def refresh_if_stale(data_dir: str, max_age_hours: int = 6) -> dict[str, int]:
    cached = load_cached_verdicts(data_dir)
    try:
        with open(_cache_path(data_dir), encoding="utf-8") as f:
            fetched_at_ms = int(json.load(f).get("fetched_at_ms") or 0)
    except (OSError, ValueError):
        fetched_at_ms = 0

    stale = not cached or (time.time() * 1000 - fetched_at_ms) > max_age_hours * 3600_000
    if stale:
        fresh = fetch_verdicts()
        if fresh is not None:
            _save_cache(data_dir, fresh, int(time.time() * 1000))
            return fresh
    return cached or {}


def verdict_label(verdicts: dict[str, int], symbol: str) -> str:
    judgement = verdicts.get(_base_symbol(symbol))
    return VERDICT_TEXTS.get(judgement, NO_RULING)


def ensure_verdict(data_dir: str, symbol: str, verdicts: dict[str, int]) -> str:
    label = verdict_label(verdicts, symbol)
    if label != NO_RULING:
        return label

    base_symbol = _base_symbol(symbol)
    judgement = _search_symbol(base_symbol)
    if judgement is None:
        return NO_RULING

    verdicts[base_symbol] = judgement
    _merge_into_cache(data_dir, {base_symbol: judgement})
    return VERDICT_TEXTS[judgement]
