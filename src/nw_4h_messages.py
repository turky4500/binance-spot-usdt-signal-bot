"""وسم فريم 4 ساعات في رسائل NW — **ملف جديد** لا يعدّل ``src/nw_messages.py``.

نظام 4H يعيد استخدام ``build_nw_bot()`` و``NWBot._apply`` كما هما على ``main``
(بما فيها «درع إعادة الرسم» و«مدة الصفقة» والتقرير اليومي). التعديل الوحيد هنا:
سطر الفريم داخل النص النهائي قبل إرساله. لا يوجد هنا أي منطق إشارة ولا أي
شرط دخول/خروج جديد.
"""

from __future__ import annotations

from src import nw_messages as _messages

FRAME_1H = "🕐 فريم الساعة (1H) · Nadaraya"
FRAME_4H = "🕐 فريم 4 ساعات (4H) · Nadaraya"


def with_4h_frame(text: str) -> str:
    """يُبدّل سطر الفريم فقط، ويُبقي بقية الأسطر والفواصل كما هي بالحرف.

    إن غيّر المستخدم القالب يومًا ولم يعد سطر الفريم موجودًا، نُدرج سطر 4H
    بعد العنوان مباشرةً بدل إرسال رسالة بلا فريم أو إيقاف الدورة باستثناء.
    """
    if FRAME_1H in text:
        return text.replace(FRAME_1H, FRAME_4H, 1)
    if FRAME_4H in text:
        return text
    lines = text.split("\n\n")
    return "\n\n".join([lines[0], FRAME_4H, *lines[1:]])


def entry_message(*args, **kwargs) -> str:
    return with_4h_frame(_messages.entry_message(*args, **kwargs))


def repeat_message(*args, **kwargs) -> str:
    return with_4h_frame(_messages.repeat_message(*args, **kwargs))


def exit_message(*args, **kwargs) -> str:
    return with_4h_frame(_messages.exit_message(*args, **kwargs))
