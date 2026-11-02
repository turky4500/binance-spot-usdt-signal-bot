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
    assert set(data.values()) <= {0, 1, 2}
    assert data["BTC"] == 0 and data["ETH"] == 0
    assert data["BNB"] == 1 and data["UNI"] == 1  # حرام حسب هذا المصدر


def test_primary_overrides_secondary():
    base = {"BTC": 0, "TAO": 0, "BNB": 0}
    primary = {"TAO": 1, "BNB": 1}
    merged = merge_with_primary(base, primary)
    assert merged["TAO"] == 1 and merged["BNB"] == 1, "الأساسي يجب أن يتقدّم"
    assert merged["BTC"] == 0, "ما لا يوجد في الأساسي يبقى من الثانوي"


def test_conflict_coins_become_non_halal():
    """ACT/BANANA/TAO/ZRO: كنّا نعدّها حلالًا والمصدر الأساسي يحرّمها."""
    data = json.loads(PRIMARY.read_text(encoding="utf-8"))
    for sym in ("ACT", "BANANA", "TAO", "ZRO"):
        assert data.get(sym) == 1, f"{sym} يجب أن يصبح غير مباح"


def test_source_expands_halal_universe():
    """عملات يحلّلها المصدر الأساسي وكان مصدرنا يحرّمها."""
    data = json.loads(PRIMARY.read_text(encoding="utf-8"))
    for sym in ("CRO", "DAI", "PAXG", "CTK", "FIDA"):
        assert data.get(sym) == 0, f"{sym} يجب أن يصبح مباحًا"


def test_missing_primary_file_is_not_fatal(tmp_path):
    assert load_primary_verdicts(str(tmp_path / "nope.json")) == {}


def test_unknown_coins_are_absent_not_halal():
    data = json.loads(PRIMARY.read_text(encoding="utf-8"))
    assert "AKRO" not in data and "GPS" not in data, "ما لا حكم له لا يُدرج (يُستبعد من التداول)"


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
