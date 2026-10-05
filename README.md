# Binance Spot USDT Signal Bot

بوت بايثون يراقب جميع أزواج **Binance Spot مقابل USDT** على **فريم 1H** ويطبّق منطق السكربت الذي أرسلته، ثم يرسل إلى تيليجرام فقط:

1. إشارة دخول شراء
2. رسالة تحقيق الهدف
3. رسالة وقف الخسارة

> كل رسالة يتبعها الآن سطر **الحكم الشرعي** للعملة، ويتم جلبه من
> `cryptohalal.cc` مع تخزين مؤقت في `data/halal_verdicts.json`.

## ما الذي تم اعتماده من السكربت؟
- **Spot فقط**
- **فريم 1H فقط**
- **الإعدادات الافتراضية كما هي**
- **رسالة دخول واحدة فقط** مع توضيح هل الإشارة قوية أم عادية
- **تحقيق الهدف** عند لمس السعر للهدف
- **وقف الخسارة** فقط إذا أغلقت شمعة 1H أسفل مستوى الوقف المرسل في أول رسالة
- تم تجاهل:
  - القاع/القمة المحتملة
  - البيع عند قمة مؤكدة
  - الخروج الاحترازي
  - نقل الوقف للتعادل والتريلنج

## هيكل المشروع
- `app.py` التطبيق الرئيسي
- `src/strategy.py` منطق الاستراتيجية المحوّل من Pine Script إلى Python
- `src/binance_client.py` جلب بيانات Binance
- `src/telegram_client.py` إرسال رسائل تيليجرام
- `src/state.py` حفظ حالة الصفقات المفتوحة
- `tools/get_telegram_chat_id.py` استخراج Chat ID
- `tools/create_github_repo.py` إنشاء مستودع GitHub عبر توكن مؤقت

> ملاحظة: في هذا المشروع تم اعتماد `https://data-api.binance.vision` كمصدر بيانات Binance لأنه في بعض البيئات قد يكون `api.binance.com` محجوبًا جغرافيًا، بينما `binance.vision` يوفّر بيانات السوق الرسمية المطلوبة للمتابعة.

## الرسائل المرسلة
### 1) الدخول
- الزوج
- الفريم
- قوة الإشارة
- سعر الدخول
- الهدف
- وقف الخسارة
- وقت الإشارة

### 2) تحقيق الهدف
- الزوج
- سعر الدخول
- سعر تحقيق الهدف
- المدة

### 3) وقف الخسارة
- الزوج
- سعر الدخول
- سعر وقف الخسارة
- المدة
- سبب الإغلاق: إغلاق شمعة 1H أسفل الوقف

## التشغيل
### 1) ثبّت المتطلبات
```bash
pip install -r requirements.txt
```

### 2) أنشئ ملف بيئة
انسخ `.env.example` إلى `.env` ثم عبّئ القيم:
```bash
cp .env.example .env
```

### 3) صدّر المتغيرات ثم شغّل البوت
```bash
set -a
source .env
set +a
python app.py
```

## استخراج Chat ID
1. افتح البوت على تيليجرام
2. أرسل له `/start` في الخاص، أو أضفه إلى المجموعة/القناة وأرسل رسالة
3. شغّل:
```bash
set -a
source .env
set +a
python tools/get_telegram_chat_id.py
```

## إنشاء مستودع GitHub
استخدم توكن GitHub مؤقت بصلاحية **public_repo** إذا كان المستودع عامًا:
```bash
export GITHUB_TOKEN=replace_me
python tools/create_github_repo.py my-repo-name
```

## تشغيله عبر GitHub Actions + Google Apps Script
تم تجهيز Workflow جاهز داخل:
`/.github/workflows/signal-bot-cron.yml`

الـ Workflow الآن يعمل عبر:
- `workflow_dispatch` فقط

ويتم استدعاؤه كل 5 دقائق من **Google Apps Script** باستخدام الملف:
- `google_apps_script_trigger.js`

### ماذا يفعل؟
- يشغّل البوت **مرة واحدة كل 5 دقائق**
- يراقب **تحقق الهدف داخل الساعة** باستخدام شموع 1 دقيقة
- يفحص **إشارات الدخول** و **وقف الخسارة** عند إغلاق شمعة الساعة
- يحفظ الحالة في `data/state.json`
- ثم يقوم بعمل commit تلقائي للحالة عند تغيّرها

### الأسرار المطلوبة في GitHub
أضف في Secrets الخاصة بالمستودع:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### إعداد Google Apps Script
1. افتح `script.google.com`
2. أنشئ مشروعًا جديدًا
3. الصق محتوى `google_apps_script_trigger.js`
4. من **Project Settings > Script properties** أضف:
   - `GITHUB_TOKEN`
   - `GITHUB_OWNER=turky4500`
   - `GITHUB_REPO=binance-spot-usdt-signal-bot`
   - `GITHUB_WORKFLOW=signal-bot-cron.yml`
   - `GITHUB_REF=main`
5. شغّل الدالة `createFiveMinuteTrigger` مرة واحدة
6. وافق على الصلاحيات

> ملاحظة: السكربت يتحقق أولًا هل يوجد Workflow ما زال يعمل أو في الانتظار،
> وإذا وجد تشغيلًا نشطًا فإنه **لن يرسل Dispatch جديدًا**. هذا يمنع تكرار
> الرسائل مثل تكرار رسالة تحقق الهدف مرتين بسبب تراكب تشغيلات GitHub.

بعد ذلك سيقوم Google Apps Script بإرسال تشغيل للـ workflow كل 5 دقائق.

## ملاحظات مهمة
- البوت يحفظ حالة الصفقات في `data/state.json`
- عند التشغيل عبر GitHub Actions يتم تحديث هذا الملف ورفعه تلقائيًا
- عند توقف البوت وعودته، يحاول الاستدراك عبر الشمعة المغلقة الأخيرة للهدف/الوقف
- GitHub Actions المجدول ليس بثًا لحظيًا 100%، لكنه مناسب إذا كان المطلوب تشغيلًا دوريًا منخفض التكلفة
- التقييم الحقيقي يظل مرهونًا بتطابق تحويل Pine إلى Python مع المنصة الفعلية وبيانات Binance
