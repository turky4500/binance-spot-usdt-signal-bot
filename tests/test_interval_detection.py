"""كشف الشمعة المغلقة واعيًا بالفريم (4H): الشبكة، محاذاة تغيير الفريم، وحد الالتقاط."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

H1 = 3_600_000
H4 = 4 * H1
# بداية محاذاة 4H (من مضاعفات 14,400,000)
S = 1_700_006_400_000


class FakeBinance:
    def __init__(self, df: pd.DataFrame):
        self._df = df

    def get_klines_for_symbols(self, symbols, interval, limit):
        return {s: self._df for s in symbols}


def make_bars(start: int, n: int) -> pd.DataFrame:
    opens = [start + i * H4 for i in range(n)]
    return pd.DataFrame({
        "open_time": opens,
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.0] * n,
        "volume": [1000.0] * n,
        "close_time": [o + H4 - 1 for o in opens],
    })


def make_detection_bot(tmp_path: Path, bars: pd.DataFrame):
    from app import SpotSignalBot
    from src.config import AppConfig

    bot = SpotSignalBot.__new__(SpotSignalBot)
    bot.config = AppConfig(telegram_bot_token="t", telegram_chat_id="c")
    bot.logger = logging.getLogger("interval-test")
    bot.binance = FakeBinance(bars)
    bot.symbols = ["AAAUSDT"]
    bot.interval_ms = H4
    bot.state = {}
    bot.store = type("S", (), {"save": lambda self, s: None})()
    bot.purge_pegged_stable_trades = lambda: None
    bot.processed: list[int] = []
    bot._process_one_closed_hour = lambda kmap, t: bot.processed.append(t)
    return bot


# ───────────────────────── كشف الإغلاق على شبكة 4H
def test_processes_last_closed_bar(tmp_path):
    bot = make_detection_bot(tmp_path, make_bars(S, 3))
    bot.process_new_closed_hour(server_time=S + 3 * H4)  # انتهت شمعة S+8h
    assert bot.processed == [S + 2 * H4]
    assert bot.state["last_processed_open_time"] == S + 2 * H4


def test_skips_still_open_bar(tmp_path):
    bot = make_detection_bot(tmp_path, make_bars(S, 3))
    bot.process_new_closed_hour(server_time=S + 2 * H4 + H1)  # داخل شمعة S+8h
    assert bot.processed == [S + H4]


def test_nothing_when_no_new_bar(tmp_path):
    bot = make_detection_bot(tmp_path, make_bars(S, 3))
    bot.state["last_processed_open_time"] = S + 2 * H4
    bot.process_new_closed_hour(server_time=S + 3 * H4 + H1)  # لا شمعة جديدة مغلقة
    assert bot.processed == []


# ───────────────────────── محاذاة شبكة الفريم (خاطئة تُقتلك عند الانتقال 1h→4h)
def test_snaps_misaligned_last_processed_to_4h_grid(tmp_path):
    # 14:00-منطق: قيمة من فريم 1H خارج شبكة 4H (S+1h ليست بداية شمعة 4H)
    bot = make_detection_bot(tmp_path, make_bars(S, 4))  # S .. S+12h
    bot.state["last_processed_open_time"] = S + H1
    bot.process_new_closed_hour(server_time=S + 4 * H4)  # انتهت شمعة S+12h
    # بعد القرب لأسفل إلى S: تُعالَج الشموع الحقيقية فقط، لا شموع وهمية بخطوة 4h من S+1h
    assert bot.processed == [S + H4, S + 2 * H4, S + 3 * H4]


def test_aligned_last_processed_is_untouched(tmp_path):
    bot = make_detection_bot(tmp_path, make_bars(S, 4))
    bot.state["last_processed_open_time"] = S + H4  # على الشبكة أصلًا
    bot.process_new_closed_hour(server_time=S + 4 * H4)
    assert bot.processed == [S + 2 * H4, S + 3 * H4]


# ───────────────────────── حد الالتقاط (catch-up ≤ 24 شمعة)
def test_catchup_caps_at_24_bars(tmp_path):
    bot = make_detection_bot(tmp_path, make_bars(S, 201))  # S .. S+200×4h
    bot.state["last_processed_open_time"] = S
    bot.process_new_closed_hour(server_time=S + 201 * H4)
    target = S + 200 * H4
    assert len(bot.processed) == 24
    assert bot.processed[0] == target - 23 * H4
    assert bot.processed[-1] == target


# ───────────────────────── رفض فريم غير مدعوم
def test_unknown_interval_is_rejected_at_startup(tmp_path):
    import pytest

    from app import SpotSignalBot
    from src.config import AppConfig

    cfg = AppConfig(
        telegram_bot_token="t",
        telegram_chat_id="c",
        state_file=str(tmp_path / "state.json"),
        interval="3h",
    )
    with pytest.raises(ValueError, match="BINANCE_INTERVAL"):
        SpotSignalBot(cfg)


def test_known_4h_interval_wires_interval_ms(tmp_path):
    from app import SpotSignalBot
    from src.config import AppConfig

    cfg = AppConfig(
        telegram_bot_token="t",
        telegram_chat_id="c",
        state_file=str(tmp_path / "state.json"),
        interval="4h",
    )
    bot = SpotSignalBot(cfg)
    assert bot.interval_ms == H4
