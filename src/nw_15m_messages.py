"""وسم فريم 15 دقيقة في رسائل NW — **ملف جديد** لا يعدّل ``src/nw_messages.py``.

نظام 15m يعيد استخدام ``build_nw_bot()`` و``NWBot._apply`` كما هما على ``main``
(بما فيها «درع إعادة الرسم» والتقرير اليومي الذي أضافه المستخدم)، فالقالب
المُنتَج مطابق حرفيًا لقالب 1H. التعديل الوحيد هنا: سطر الفريم داخل النص
النهائي قبل إرساله، تحقيقًا لطلب «يذكر بالرسالة نوع الفريم مع التوصية».

لا يوجد هنا أي منطق إشارة ولا أي شرط دخول/خروج جديد.
"""

from __future__ import annotations

from src import nw_messages as _messages

FRAME_1H = "🕐 فريم الساعة (1H) · Nadaraya"
FRAME_15M = "🕐 فريم 15 دقيقة (15M) · Nadaraya"


def with_15m_frame(text: str) -> str:
    """يُبدّل سطر الفريم فقط، ويُبقي بقية الأسطر والفواصل كما هي بالحرف.

    إن غيّر المستخدم القالب يومًا ولم يعد سطر الفريم موجودًا، نُدرج سطر 15m
    بعد العنوان مباشرةً بدل إرسال رسالة بلا فريم أو إيقاف الدورة باستثناء.
    """
    if FRAME_1H in text:
        return text.replace(FRAME_1H, FRAME_15M, 1)
    if FRAME_15M in text:
        return text
    lines = text.split("\n\n")
    return "\n\n".join([lines[0], FRAME_15M, *lines[1:]])


def entry_message(*args, **kwargs) -> str:
    return with_15m_frame(_messages.entry_message(*args, **kwargs))


def repeat_message(*args, **kwargs) -> str:
    return with_15m_frame(_messages.repeat_message(*args, **kwargs))


def exit_message(*args, **kwargs) -> str:
    return with_15m_frame(_messages.exit_message(*args, **kwargs))
