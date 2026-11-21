"""المصدر الأساسي للأحكام (أكاديمية البيتكوين) — الأولوية والتغطية والانعكاسات.

منهجية المصدر: إن لم يجد خدمات حرام/مشبوهة ⇒ حلال. الملف `halal_academy.json`
يحمل القيمة 0=مباح، 1=غير مباح، 2=مشبوه، ويتقدّم على cryptohalal.cc وhalal_extra.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.halal import load_primary_verdicts, merge_with_primary  # noqa: E402

PRIMARY = ROOT / "data" / "daily_trend" / "halal_academy.json"


def test_primary_file_is_sane():
    data = json.loads(PRIMARY.read_text(encoding="utf-8"))
    assert len(data) >= 700, "المصدر الأساسي يجب أن يغطي مئات العملات"
    assert all(type(v) is int and v in (0, 1, 2) for v in data.values())
    assert {"BTC", "ETH", "USDT"} <= set(data)
    # لا نثبّت الأحكام في الاختبارات؛ المصدر نفسه قد يغيّر حكمًا بعد التحديث.


def test_primary_overrides_secondary():
    base = {"BTC": 0, "TAO": 0, "BNB": 0}
    primary = {"TAO": 1, "BNB": 1}
    merged = merge_with_primary(base, primary)
    assert merged["TAO"] == 1 and merged["BNB"] == 1, "الأساسي يجب أن يتقدّم"
    assert merged["BTC"] == 0, "ما لا يوجد في الأساسي يبقى من الثانوي"


def test_live_primary_wins_even_when_rulings_change():
    """تعارض الثانوي لا يتغلب على حكم الملف الحالي، سواء تغيّر حكم TAO أم لا."""
    data = json.loads(PRIMARY.read_text(encoding="utf-8"))
    symbols = ("ACT", "BANANA", "TAO", "ZRO")
    assert all(sym in data for sym in symbols)
    secondary = {sym: (1 if data[sym] == 0 else 0) for sym in symbols}
    merged = merge_with_primary(secondary, data)
    assert all(merged[sym] == data[sym] for sym in symbols)


def test_halal_universe_respects_primary_override_with_fixtures():
    """حالة ثابتة للاختبار بدل افتراض أحكام الأصول الحقيقية للأبد."""
    secondary = {"AAA": 1, "BBB": 0}
    merged = merge_with_primary(secondary, {"AAA": 0, "BBB": 1})
    assert {sym for sym, verdict in merged.items() if verdict == 0} == {"AAA"}


def test_missing_primary_file_is_not_fatal(tmp_path):
    assert load_primary_verdicts(str(tmp_path / "nope.json")) == {}


def test_unclear_new_coin_is_not_invented():
    """المراجعة غير حكم، بصرف النظر عن أحكام الموقع التي قد تتغير لاحقًا."""
    from datetime import datetime, timezone
    from tools.update_halal_academy import build_merge
    rows = [{"symbol": "WAIT", "label": "قيد المراجعة", "name": "Wait", "article": ""}]
    merged, meta, _ = build_merge({}, {"source": "btcacademy.online"}, {}, {"prices": {}},
                                  rows, [], {"WAIT"}, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert "WAIT" not in merged and "WAIT" in meta["unknown"]


def test_bot_uses_primary_verdicts(tmp_path, monkeypatch):
    """التكامل: حكم المصدر الأساسي يقرّر دخول الزوج إلى الكون."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "daily_trend").mkdir(parents=True)
    (tmp_path / "data" / "daily_trend" / "halal_academy.json").write_text(
        json.dumps({"AAA": 0, "BBB": 1}), encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "zz_exclude.txt").write_text("", encoding="utf-8")

    import daily_trend_bot as dtb

    bot = dtb.DailyTrendBot.__new__(dtb.DailyTrendBot)

    class C:
        def get_spot_usdt_symbols(self):
            return ["AAAUSDT", "BBBUSDT", "CCCUSDT"]

        def get_price_ticks(self):
            return {}

    bot.client = C()
    bot.settings = dtb.DailyTrendSettings()
    import src.halal as H
    # المصدر الثانوي يقول: BBB مباح — لكن المصدر الأساسي يحرّمه ⇒ يُستبعد
    monkeypatch.setattr(H, "refresh_if_stale", lambda *a, **k: {"BBB": 0, "CCC": 0})
    bot.refresh_universe()
    assert bot.symbols == ["AAAUSDT", "CCCUSDT"], bot.symbols
