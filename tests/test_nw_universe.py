"""الكون الجديد: كل العملات بجميع أحكامها، بلا بوابة BTC ولا فلتر شرعي."""

from __future__ import annotations

import json

from nw_bot import NWBot
from src.halal import verdict_label
from src.nw_state import NWStateStore

SYMBOLS = [
    "AAAUSDT",       # غير مباح
    "BBBUSDT",       # مشبوه
    "CCCUSDT",       # غير مباح
    "DDDUSDT",       # مباح
    "ZZZUSDT",       # بلا حكم
    "AAPLBUSDT",     # سهم مرمّز، بلا حكم
    "USDCUSDT",      # عملة مستقرة مربوطة
    "FDUSDUSDT",     # عملة مستقرة مربوطة
]


class FakeClient:
    def get_spot_usdt_symbols(self):
        return list(SYMBOLS)


class FakeTelegram:
    def send_message(self, text: str) -> None:
        pass


def make_bot(tmp_path, **kwargs):
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({
        "vendor": "cryptohalal.cc", "fetched_at_ms": 1, "note": "وصف",
        "judgements": {"AAA": 0, "BBB": 2},
    }), encoding="utf-8")
    (tmp_path / "extra.json").write_text(json.dumps({"CCC": 1}), encoding="utf-8")
    (tmp_path / "primary.json").write_text(json.dumps({"AAA": 1, "DDD": 0}), encoding="utf-8")
    return NWBot(
        client=FakeClient(),
        telegram=FakeTelegram(),
        state_store=NWStateStore(str(tmp_path / "state.json")),
        halal_primary=str(tmp_path / "primary.json"),
        halal_extra=str(tmp_path / "extra.json"),
        halal_cache=str(cache),
        **kwargs,
    )


def test_verdicts_are_loaded_even_with_metadata_and_primary_wins(tmp_path):
    bot = make_bot(tmp_path)
    v = bot._load_verdicts()
    assert v["AAA"] == 1          # المصدر الأساسي يتقدّم على الكاش
    assert v["BBB"] == 2 and v["CCC"] == 1 and v["DDD"] == 0
    assert "vendor" not in v and "judgements" not in v


def test_all_rulings_are_included_and_labelled(tmp_path):
    bot = make_bot(tmp_path)
    symbols = bot.refresh_universe()
    # المستقرة المربوطة فقط مستبعدة (ضجيج)، وكل الأحكام الأخرى داخلة
    assert "USDCUSDT" not in symbols and "FDUSDUSDT" not in symbols
    for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "ZZZUSDT", "AAPLBUSDT"):
        assert s in symbols, s
    assert bot.verdicts["AAA"] == 1
    assert verdict_label(bot.verdicts, "AAAUSDT") == "⛔ غير مباح"
    assert verdict_label(bot.verdicts, "BBBUSDT") == "⚠️ مشبوه"
    assert verdict_label(bot.verdicts, "DDDUSDT") == "✅ مباح"
    assert verdict_label(bot.verdicts, "ZZZUSDT") == "ℹ️ لا يوجد حكم"


def test_optional_halal_only_mode(tmp_path):
    bot = make_bot(tmp_path, include_all=False)
    assert bot.refresh_universe() == ["DDDUSDT"]


def test_optional_stock_and_stable_exclusions(tmp_path):
    bot = make_bot(tmp_path, exclude_tokenized_stocks=True)
    assert "AAPLBUSDT" not in bot.refresh_universe()
    bot2 = make_bot(tmp_path, exclude_pegged_stables=False)
    assert "USDCUSDT" in bot2.refresh_universe()


def test_no_btc_gate_in_entry_logic(tmp_path):
    """الدخول من المؤشر وحده: لا بوابة BTC ولا EMA ولا gate_open في كود النظام."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    files = [root / "nw_bot.py", root / "src" / "nw_signals.py", root / "src" / "nw_strategy.py"]
    forbidden = ("BTCUSDT", "EMA", "btc_gate", "gate_open", "DT_GATE")
    for f in files:
        text = f.read_text(encoding="utf-8")
        for bad in forbidden:
            assert bad not in text, f"{f.name} يحتوي {bad}"
