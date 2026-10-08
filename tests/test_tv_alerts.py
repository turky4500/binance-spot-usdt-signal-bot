"""اختبار معالج تنبيهات TradingView (مؤشر قمم وقيعان مؤكدة).

القواعد المختبرة:
  • buy/strong_buy  → فتح صفقة + رسالة دخول.
  • take_profit/stop_loss/exit/sell → إغلاق الصفقة + رسالة إغلاق بنتيجة.
  • لا رسالة ما دامت الصفقة مفتوحة على نفس العملة.
  • ticker يُحوّل من 'BINANCE:ETHUSDT' و'ETHUSDT.P' إلى 'ETHUSDT'.
  • potential_bottom/top تُتجاهل (مراقبة فقط).
  • ملف JSON فاسد لا يكسر البوت.
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ["SMART_ENTRY"] = "0"
os.environ["QUIET_ENTRY_SIGNALS"] = "0"
os.environ["STRATEGY_SHADOW"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from src.config import AppConfig  # noqa: E402
from src.tv_alerts import parse_alert, iter_inbox  # noqa: E402
from app import SpotSignalBot  # noqa: E402

TZ = ZoneInfo("Asia/Riyadh")


def _new_bot(data_dir):
    cfg = AppConfig(telegram_bot_token="x", telegram_chat_id="y",
                    state_file=f"{data_dir}/state.json")
    bot = SpotSignalBot(cfg)
    bot.telegram = type("T", (), {"send_message": staticmethod(lambda m: _SENT.append(m))})()
    return bot


_SENT: list[str] = []


def _write_alert(inbox: Path, name: str, payload: dict) -> Path:
    p = inbox / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _reset():
    _SENT.clear()


# ============ 1) parse_alert: تحويلات الـticker ============
def test_parse_ticker_normalization(tmp_path):
    p = _write_alert(tmp_path, "a.json", {
        "action": "buy", "ticker": "BINANCE:ETHUSDT", "price": 100.0,
        "stop": 98.0, "target": 102.0, "mode": "متوازن",
    })
    a = parse_alert(p)
    assert a is not None and a.symbol == "ETHUSDT"
    print(f"[OK] 'BINANCE:ETHUSDT' → {a.symbol} | action={a.action}")


def test_parse_ticker_futures_suffix(tmp_path):
    p = _write_alert(tmp_path, "b.json", {
        "action": "buy", "ticker": "BTCUSDT.P", "price": 50000.0,
    })
    a = parse_alert(p)
    assert a is not None and a.symbol == "BTCUSDT"
    print(f"[OK] 'BTCUSDT.P' → {a.symbol} (شِيل لاحقة العقود الآجلة)")


def test_parse_watch_only_ignored(tmp_path):
    p = _write_alert(tmp_path, "c.json", {"action": "potential_bottom", "ticker": "XRPUSDT"})
    assert parse_alert(p) is None
    print("[OK] potential_bottom يُتجاهل (مراقبة فقط)")


def test_parse_corrupt_file(tmp_path):
    p = tmp_path / "d.json"; p.write_text("{not json")
    assert parse_alert(p) is None
    assert not p.exists()  # نُقل إلى .corrupt
    print("[OK] JSON فاسد لا يكسر المعالجة")


# ============ 2) buy يفتح صفقة + رسالة ============
def test_buy_opens_trade(tmp_path):
    _reset()
    bot = _new_bot(str(tmp_path))
    inbox = tmp_path / "tv_inbox"; inbox.mkdir()
    _write_alert(inbox, "buy1.json", {
        "action": "strong_buy", "ticker": "BTCUSDT", "price": 50000.0,
        "stop": 49200.0, "target": 51000.0, "mode": "مؤكد",
    })
    bot.process_tv_alerts()
    assert "BTCUSDT" in bot.state.get("open_trades", {}), "الصفقة يجب أن تُفتح"
    t = bot.state["open_trades"]["BTCUSDT"]
    assert t["entry_price"] == 50000.0 and t["target_price"] == 51000.0
    assert t["stop_price"] == 49200.0 and t["source"] == "tv_alert"
    assert any("إشارة شراء" in m and "قوي" in m for m in _SENT), f"رسالة الشراء القوي مفقودة: {_SENT}"
    assert any("BTCUSDT" in m and "الهدف" in m and "وقف" in m for m in _SENT)
    assert any("الحكم الشرعي" in m for m in _SENT)
    assert not (inbox / "buy1.json").exists(), "الملف يجب أن يُنقل إلى tv_processed"
    print(f"[OK] buy قوي: فتح صفقة BTCUSDT + رسالة ({len(_SENT)} رسالة)")


# ============ 3) لا رسالة على عملة مفتوحة ============
def test_buy_ignored_when_open(tmp_path):
    _reset()
    bot = _new_bot(str(tmp_path))
    inbox = tmp_path / "tv_inbox"; inbox.mkdir()
    # إشارة أولى: تفتح
    _write_alert(inbox, "b1.json", {"action": "buy", "ticker": "ETHUSDT", "price": 2000.0,
                                     "stop": 1970.0, "target": 2040.0})
    bot.process_tv_alerts()
    first = list(_SENT)
    # إشارة ثانية لنفس العملة: تُتجاهل
    _write_alert(inbox, "b2.json", {"action": "strong_buy", "ticker": "ETHUSDT", "price": 2010.0,
                                     "stop": 1980.0, "target": 2050.0})
    bot.process_tv_alerts()
    assert len(_SENT) == len(first), f"لا رسالة جديدة يجب أن تُرسل على ETHUSDT المفتوحة | {first} vs {_SENT}"
    # لكن الصفقة المفتوحة ما زالت الأولى
    assert bot.state["open_trades"]["ETHUSDT"]["entry_price"] == 2000.0
    print(f"[OK] إشارة شراء ثانية على عملة مفتوحة → تُتجاهل بلا رسالة")


# ============ 4) take_profit يُغلق ويرسل نتيجة ناجحة ============
def test_take_profit_closes_won(tmp_path):
    _reset()
    bot = _new_bot(str(tmp_path))
    inbox = tmp_path / "tv_inbox"; inbox.mkdir()
    _write_alert(inbox, "o.json", {"action": "buy", "ticker": "SOLUSDT", "price": 100.0,
                                    "stop": 98.0, "target": 102.0})
    bot.process_tv_alerts()
    _reset()
    _write_alert(inbox, "c.json", {"action": "take_profit", "ticker": "SOLUSDT", "price": 102.0})
    bot.process_tv_alerts()
    assert "SOLUSDT" not in bot.state.get("open_trades", {}), "الصفقة يجب أن تُغلق"
    assert any("ناجحة" in m and "SOLUSDT" in m and "+2.00%" in m for m in _SENT), f"رسالة خاطئة: {_SENT}"
    assert any("الحكم الشرعي" in m for m in _SENT)
    print(f"[OK] take_profit → إغلاق ناجح + رسالة (+2.00%)")


# ============ 5) stop_loss يُغلق ويرسل خاسرة ============
def test_stop_loss_closes_lost(tmp_path):
    _reset()
    bot = _new_bot(str(tmp_path))
    inbox = tmp_path / "tv_inbox"; inbox.mkdir()
    _write_alert(inbox, "o.json", {"action": "buy", "ticker": "XRPUSDT", "price": 1.0,
                                    "stop": 0.98, "target": 1.02})
    bot.process_tv_alerts(); _reset()
    _write_alert(inbox, "c.json", {"action": "stop_loss", "ticker": "XRPUSDT", "price": 0.98})
    bot.process_tv_alerts()
    assert "XRPUSDT" not in bot.state.get("open_trades", {})
    assert any("خاسرة" in m and "XRPUSDT" in m and "-2.00%" in m for m in _SENT), f"رسالة خاطئة: {_SENT}"
    print(f"[OK] stop_loss → إغلاق خاسر + رسالة (−2.00%)")


# ============ 6) sell/exit تُغلق ============
def test_sell_and_exit_close(tmp_path):
    for action, expected_reason in [("sell", "إشارة بيع"), ("exit", "خروج احترازي")]:
        _reset()
        bot = _new_bot(str(tmp_path))
        inbox = tmp_path / "tv_inbox"; inbox.mkdir(exist_ok=True)
        sym = f"X{action.upper()}USDT"
        _write_alert(inbox, "o.json", {"action": "buy", "ticker": sym, "price": 50.0,
                                        "stop": 49.0, "target": 51.0})
        bot.process_tv_alerts(); _reset()
        _write_alert(inbox, "c.json", {"action": action, "ticker": sym, "price": 50.5})
        bot.process_tv_alerts()
        assert sym not in bot.state.get("open_trades", {})
        assert any(expected_reason in m for m in _SENT), f"سبب {expected_reason} مفقود: {_SENT}"
        print(f"[OK] {action} → إغلاق بـ'{expected_reason}'")


# ============ 7) exit على عملة غير مفتوحة → لا رسالة ============
def test_close_on_no_open_is_silent(tmp_path):
    _reset()
    bot = _new_bot(str(tmp_path))
    inbox = tmp_path / "tv_inbox"; inbox.mkdir()
    _write_alert(inbox, "c.json", {"action": "take_profit", "ticker": "GHOSTUSDT", "price": 1.0})
    bot.process_tv_alerts()
    assert _SENT == [], f"لا رسالة يجب أن تُرسل: {_SENT}"
    print("[OK] إغلاق على عملة غير مفتوحة → لا رسالة (تنبيه متأخر/مكرّر)")


# ============ 8) صندوق البريد يفرغ بعد المعالجة ============
def test_inbox_drained(tmp_path):
    bot = _new_bot(str(tmp_path))
    inbox = tmp_path / "tv_inbox"; inbox.mkdir()
    for i in range(3):
        _write_alert(inbox, f"a{i}.json", {"action": "buy", "ticker": f"S{i}USDT", "price": 1.0,
                                            "stop": 0.99, "target": 1.02})
    assert len(list(iter_inbox(str(tmp_path)))) == 3
    bot.process_tv_alerts()
    assert list(iter_inbox(str(tmp_path))) == []
    processed_dir = tmp_path / "tv_processed"
    assert processed_dir.exists() and len(list(processed_dir.glob("*.json"))) == 3
    print(f"[OK] 3 تنبيهات → 3 ملفات في tv_processed")


def main():
    import tempfile
    tests = [
        test_parse_ticker_normalization,
        test_parse_ticker_futures_suffix,
        test_parse_watch_only_ignored,
        test_parse_corrupt_file,
        test_buy_opens_trade,
        test_buy_ignored_when_open,
        test_take_profit_closes_won,
        test_stop_loss_closes_lost,
        test_sell_and_exit_close,
        test_close_on_no_open_is_silent,
        test_inbox_drained,
    ]
    for t in tests:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            t(tmp)
    print("\nALL TV ALERT TESTS PASSED")


if __name__ == "__main__":
    main()
