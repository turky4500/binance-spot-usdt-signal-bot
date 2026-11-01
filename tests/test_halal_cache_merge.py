"""اختبارات دمج كاش الأحكام الشرعية (2026-11-01).

الخطأ القديم: التحديث الشامل كل 6 ساعات كان **يستبدل** الملف بقائمة المصدر (51 عملة)
فيمحو كل حكم جُمع بالبحث الفردي (51 → 55 → 51 …). هذه الاختبارات تمنع رجوعه.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src import halal  # noqa: E402


def _write(data_dir: pathlib.Path, judgements: dict, fetched_at_ms: int = 1) -> pathlib.Path:
    path = data_dir / "halal_verdicts.json"
    data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "vendor": "cryptohalal.cc",
        "fetched_at_ms": fetched_at_ms,
        "note": "مصدر الأحكام: CryptoHalal.cc",
        "judgements": judgements,
    }), encoding="utf-8")
    return path


def _read(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_bulk_refresh_keeps_search_added_coins(tmp_path, monkeypatch):
    path = _write(tmp_path, {"BTC": 0, "ETH": 0, "REZ": 1})   # REZ جاءت من بحث فردي
    monkeypatch.setattr(halal, "fetch_verdicts", lambda *a, **k: {"BTC": 0, "ETH": 0})
    monkeypatch.setattr(halal.time, "time", lambda: 1_700_000_000.0)   # الكاش عمره أكبر من 6 ساعات

    verdicts = halal.refresh_if_stale(str(tmp_path), max_age_hours=6)

    assert verdicts["REZ"] == 1, "العملة المضافة بالبحث الفردي يجب ألا تُمحى"
    saved = _read(path)
    assert saved["judgements"]["REZ"] == 1
    assert saved["fetched_at_ms"] == 1_700_000_000_000, "وقت الجلب يجب أن يُحدَّث"


def test_bulk_refresh_updates_changed_verdicts(tmp_path, monkeypatch):
    path = _write(tmp_path, {"BTC": 0, "XRP": 0})
    monkeypatch.setattr(halal, "fetch_verdicts", lambda *a, **k: {"XRP": 1})  # تغيّر الحكم في المصدر
    monkeypatch.setattr(halal.time, "time", lambda: 1_700_000_000.0)   # الكاش قديم ⇒ تحديث شامل

    verdicts = halal.refresh_if_stale(str(tmp_path), max_age_hours=6)

    assert verdicts["XRP"] == 1, "قيمة المصدر الجديدة تُحدِّث القديمة"
    assert verdicts["BTC"] == 0, "العملات غير المذكورة في التحديث تبقى"
    assert _read(path)["judgements"]["XRP"] == 1


def test_failed_refresh_keeps_cache(tmp_path, monkeypatch):
    path = _write(tmp_path, {"BTC": 0, "ZIL": 0})
    monkeypatch.setattr(halal, "fetch_verdicts", lambda *a, **k: None)
    monkeypatch.setattr(halal.time, "time", lambda: 1_700_000_000.0)   # الكاش قديم ⇒ محاولة تحديث

    verdicts = halal.refresh_if_stale(str(tmp_path), max_age_hours=6)

    assert verdicts == {"BTC": 0, "ZIL": 0}
    assert _read(path)["judgements"] == {"BTC": 0, "ZIL": 0}


def test_fresh_cache_is_not_refetched(tmp_path, monkeypatch):
    _write(tmp_path, {"BTC": 0}, fetched_at_ms=1_000_000)
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        return {"BTC": 1}

    monkeypatch.setattr(halal, "fetch_verdicts", boom)
    monkeypatch.setattr(halal.time, "time", lambda: 2_000.0)   # 1000 ثانية < 6 ساعات

    verdicts = halal.refresh_if_stale(str(tmp_path), max_age_hours=6)

    assert calls["n"] == 0, "الكاش حديث ⇒ لا جلب"
    assert verdicts == {"BTC": 0}


def test_merge_creates_file_when_missing(tmp_path):
    merged = halal._merge_and_save(str(tmp_path), {"ZIL": 0}, fetched_at_ms=5)
    assert merged == {"ZIL": 0}
    assert _read(tmp_path / "halal_verdicts.json")["judgements"] == {"ZIL": 0}


def test_cache_grows_across_repeated_bulk_refreshes(tmp_path, monkeypatch):
    """السيناريو الذي كان يمحو الأحكام: ثلاث تحديثات شاملة + بحث فردي بينها."""
    _write(tmp_path, {"BTC": 0})
    monkeypatch.setattr(halal.time, "time", lambda: 100_000.0)
    monkeypatch.setattr(halal, "fetch_verdicts", lambda *a, **k: {"BTC": 0, "ETH": 0})
    halal.refresh_if_stale(str(tmp_path), max_age_hours=6)

    monkeypatch.setattr(halal, "_search_symbol", lambda symbol, **k: 1)
    halal.ensure_verdict(str(tmp_path), "ZILUSDT", halal.load_cached_verdicts(str(tmp_path)) or {})

    monkeypatch.setattr(halal.time, "time", lambda: 900_000.0)   # بعد 6 ساعات
    halal.refresh_if_stale(str(tmp_path), max_age_hours=6)

    final = _read(tmp_path / "halal_verdicts.json")["judgements"]
    assert final == {"BTC": 0, "ETH": 0, "ZIL": 1}, "لا شيء يجب أن يضيع بين التحديثات"
