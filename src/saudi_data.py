"""بيانات تداول اليومية (Yahoo Finance) — أسعار معدّلة بالكامل للأسهم السعودية.

الرموز بصيغة تداول: `{code}.SR` مثل 2222.SR.

⚠️ لماذا التعديل (adjustment)؟ توزيعات الأرباح والانقسامات تُحدث قفزات مصطنعة في
الأسعار الخام فتفسد القمم (pivot) وEMA200 وتخلق إشارات كاذبة قرب تاريخ الحدث.
الحل هنا: نضرب OHLC الخام في معامل كل شمعة = adjclose/close (تعديل خلفي back-adjust)
فتصبح السلسلة متصلة داخليًا، مع بقاء الشمعة **الأحدث** بمعامل 1 تمامًا — أي أن أسعار
الإشارات والعرض في أحدث شمعة هي الأسعار الفعلية القابلة للتداول (لا تُعدَّل للأمام).

لا نعتمد على مكتبة yfinance (تتكسر كثيرًا مع تغيّرات Yahoo) — نداء HTTP خام بـrequests
مع إعادة محاولة، أخفّ وأثبت كما في binance_client.py الخاص بنظام القاع.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DAY_MS = 86_400_000
RIYADH_TZ = timezone(timedelta(hours=3))  # تداول: UTC+3 بلا تحوّل صيفي
MARKET_CLOSE_HOUR_RIYADH = 15  # إغلاق تداول 15:00 الرياض = 12:00 UTC


def riyadh_date_str(ms: int) -> str:
    """تاريخ التداول (YYYY-MM-DD) لوقت ملّي ثانية بتوقيت الرياض."""
    return datetime.fromtimestamp(ms / 1000, RIYADH_TZ).date().isoformat()


class TadawulClient:
    """Yahoo Finance daily client للأسهم السعودية (.SR) بأسعار معدّلة بالكامل."""

    HOSTS = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
        ),
        "Accept": "application/json",
    }

    def __init__(self, timeout: int = 20, max_workers: int = 8) -> None:
        self.timeout = timeout
        self.max_workers = max_workers
        self.session = requests.Session()
        retry = Retry(
            total=3, connect=3, read=3, backoff_factor=0.7,
            status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # ─────────────────────────────────────────── رمز التداول
    @staticmethod
    def to_yahoo_symbol(code: str) -> str:
        """يحوّل رمز تداول (4 أرقام) إلى رمز Yahoo: 2222 → 2222.SR."""
        code = str(code).strip().upper()
        if code.endswith(".SR"):
            return code
        return f"{code}.SR"

    # ─────────────────────────────────────────── جلب شموع يومية معدّلة
    def _get_json(self, path: str, params: dict) -> dict:
        last_err: Exception | None = None
        for host in self.HOSTS:
            try:
                resp = self.session.get(
                    f"{host}{path}", params=params, headers=self._HEADERS, timeout=self.timeout
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as err:  # noqa: BLE001 — نجرّب المضيف الآخر ثم نرفع آخر خطأ
                last_err = err
        raise RuntimeError(f"Yahoo request failed for {path}: {last_err}")

    def get_daily(self, code: str, range: str = "2y") -> pd.DataFrame:
        """شموع يومية معدّلة بالكامل لرمز تداول.

        تُرجع DataFrame بأعمدة: open_time(ms), open, high, low, close, volume,
        date(YYYY-MM-DD بتوقيت الرياض). القيم OHLC معدّلة (معامل 1 على آخر شمعة).
        """
        symbol = self.to_yahoo_symbol(code)
        params = {
            "range": range,
            "interval": "1d",
            "events": "div|split",
            "includeAdjustedClose": "true",
        }
        payload = self._get_json("/v8/finance/chart/" + symbol, params)
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            return pd.DataFrame()
        res = result[0]
        ts = res.get("timestamp")
        if not ts:
            return pd.DataFrame()
        quote = (res.get("indicators") or {}).get("quote") or [{}]
        q = quote[0]
        adj_series = ((res.get("indicators") or {}).get("adjclose") or [{}])
        adj = adj_series[0].get("adjclose") if adj_series else None

        closes = pd.Series(q.get("close"), dtype="float64")
        opens = pd.Series(q.get("open"), dtype="float64")
        highs = pd.Series(q.get("high"), dtype="float64")
        lows = pd.Series(q.get("low"), dtype="float64")
        volumes = pd.Series(q.get("volume"), dtype="float64")

        if adj is not None and len(adj) == len(ts):
            adjclose = pd.Series(adj, dtype="float64")
            # معامل التعديل = adjclose/close، ونثبّته 1.0 على آخر شمعة (أسعار فعلية)
            factor = (adjclose / closes).fillna(1.0)
            factor = factor.where(closes > 0, 1.0)
        else:
            factor = pd.Series(1.0, index=range(len(ts)))

        df = pd.DataFrame({
            # Yahoo chart يُرجع timestamp بالثواني — نحوّلها ms لتطابق بنية نظام القاع
            "open_time": (pd.Series(ts, dtype="int64") * 1000),
            "open": (opens * factor).to_numpy(),
            "high": (highs * factor).to_numpy(),
            "low": (lows * factor).to_numpy(),
            "close": (closes * factor).to_numpy(),
            "volume": volumes.to_numpy(),
        })
        # تاريخ التداول (YYYY-MM-DD بتوقيت الرياض) — أساس معالجة الأيام غير المتصلة
        df["date"] = [riyadh_date_str(int(ms)) for ms in df["open_time"]]
        # قرّب إلى منزلتان عمليًا (تجميع جيد للعمليات الحسابية على أسعار معدّلة)
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].round(6)
        # ترتيب صاعد بلا تكرار (Yahoo قد يعيد شمعة جزئية لليوم الحالي)
        df = df.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
        return df

    def get_daily_for_symbols(self, codes: list[str], range: str = "2y") -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            futs = {ex.submit(self.get_daily, c, range): c for c in codes}
            for fut in as_completed(futs):
                code = futs[fut]
                try:
                    out[code] = fut.result()
                except Exception:  # noqa: BLE001 — رمز واحد فاشل لا يوقف المسح
                    out[code] = pd.DataFrame()
        return out


# ───────────────────────────────────────────── تنسيق سعر الريال
def format_sar_price(value) -> str:
    """تنسيق سعر بالريال: خانتان عشريتان كما في تداول (مثل 25.48)."""
    if value is None:
        return "—"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def now_ms() -> int:
    return int(time.time() * 1000)


def riyadh_today_str(now: int | None = None) -> str:
    """تاريخ اليوم بتوقيت الرياض (YYYY-MM-DD)."""
    return riyadh_date_str(now if now is not None else now_ms())


def date_close_ms(date_str: str) -> int:
    """وقت إغلاق جلسة يوم تداول (15:00 الرياض = 12:00 UTC) بملّي ثانية — لعرض تواريخ الأحداث."""
    d = datetime.fromisoformat(date_str).replace(tzinfo=RIYADH_TZ)
    close_dt = d.replace(hour=MARKET_CLOSE_HOUR_RIYADH, minute=0, second=0, microsecond=0)
    return int(close_dt.timestamp() * 1000)


def is_bar_closed(date_str: str, now: int) -> bool:
    """هل شمعة يوم `date_str` اكتمل إغلاقها عند اللحظة `now` (ملّي ثانية)؟

    قاعدة مزدوجة: أي يوم قبل اليوم الحالي مغلق بالتأكيد؛ ويوم اليوم مغلق فقط بعد 15:00 الرياض.
    يتعامل تلقائيًا مع العطلات (لا شمعة في تلك الأيام فلا تُؤخذ بعين الاعتبار).
    """
    today = riyadh_today_str(now)
    if date_str < today:
        return True
    if date_str > today:
        return False
    return now >= date_close_ms(date_str)
