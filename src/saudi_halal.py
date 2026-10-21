"""الحكم الشرعي لأسهم تداول — قائمة يدوية محلية (لا اعتماد على أي واجهة خارجية).

الفرق عن نظام القاع (كريبتو): لا يوجد ما يقابل cryptohalal.cc لأسهم تداول بمصدر
مجاني موثوق. لذا المصدر هنا **ملف محلي يملؤه المستخدم**:
    config/sa_halal.json  →  {"2222": "halal", "1120": "haram", "7010": "doubtful"}

القيم المسموحة: "halal" | "haram" | "doubtful" (أي مفتاح آخر يُعامَل كـ«غير معرَّف»).
إن غاب الملف أو كان فارغًا (الوضع الحالي: بدون حكم) تُرجع كل رسالة «ℹ️ لا يوجد حكم» —
لا يُعطَّل النظام ولا يُمنع دخول، بل يُعرض الحكم إن وُجد ويُترك فارغًا إن لم يوجد.

البنية جاهزة: أضف/عدّل الملف لتفعيل الأحكام دون لمس الكود.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

HALAL_FILE = os.getenv("SA_HALAL_FILE", "config/sa_halal.json")

VERDICT_TEXTS = {
    "halal": "✅ مباح",
    "haram": "⛔ غير مباح",
    "doubtful": "⚠️ مشبوه",
}
NO_RULING = "ℹ️ لا يوجد حكم"


def load_verdicts(path: str | None = None) -> dict[str, str]:
    """يقرأ القائمة اليدوية: {رمز: halal|haram|doubtful}. غائب/فارغ = {} (لا حكم)."""
    file_path = Path(path or HALAL_FILE)
    if not file_path.exists():
        return {}
    try:
        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for code, verdict in data.items():
        code_s = str(code).strip().upper().removesuffix(".SR")
        verdict_s = str(verdict).strip().lower()
        if code_s and verdict_s in VERDICT_TEXTS:
            out[code_s] = verdict_s
    return out


def refresh_if_stale(_data_dir: str, max_age_hours: int = 0) -> dict[str, str]:
    """واجهة مطابقة لنظام القاع — لكن القراءة محلية فورية (لا شبكة ولا كاش زمني)."""
    return load_verdicts()


def ensure_verdict(_data_dir: str, code: str, verdicts: dict[str, str]) -> str:
    """نص الحكم لرمز سهم: من القائمة اليدوية، أو «لا يوجد حكم» إن لم يُعرَّف."""
    code_s = str(code).strip().upper().removesuffix(".SR")
    return VERDICT_TEXTS.get(verdicts.get(code_s, ""), NO_RULING)


def verdict_label(verdicts: dict[str, str], code: str) -> str:
    return ensure_verdict("", code, verdicts)
