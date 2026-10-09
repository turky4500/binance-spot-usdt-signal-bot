# Binance Spot USDT Signal Bot — Target Trend

بوت بايثون يراقب جميع أزواج **Binance Spot مقابل USDT** على **فريم 1H** ويطبّق مؤشرًا واحدًا معتمدًا، ثم يرسل إلى تيليجرام:

1. 📥 إشارة دخول شراء (مع الأهداف الثلاثة ووقف الخسارة)
2. 🎯 رسالة تحقق أحد الأهداف (تبقى الصفقة مفتوحة)
3. ✅/🛑 رسالة انتهاء الصفقة (رابحة/خاسرة + عدد الأهداف المحققة)

> كل رسالة يتبعها سطر **الحكم الشرعي** للعملة، يُجلب من `cryptohalal.cc`
> مع تخزين مؤقت في `data/halal_verdicts.json`.

## المؤشر المعتمد الوحيد

**Target Trend [BigBeluga]** — محوّل من Pine Script v5 إلى Python في `src/strategy.py`.

المنطق كما في Pine حرفيًا:

```
atr_value = sma(atr(200), 200) * 0.8
sma_high  = sma(high, length) + atr_value      ← خط الاتجاه الصاعد
sma_low   = sma(low,  length) - atr_value      ← خط الاتجاه الهابط (والوقف)

trend = true  عند crossover(close, sma_high)
trend = false عند crossunder(close, sma_low)

signal_up   = انقلاب الاتجاه هابطًا ← صاعدًا → إشارة شراء
signal_down = انقلاب الاتجاه صاعدًا ← هابطًا → إشارة إغلاق الصفقة
```

- `length = 10` (قابل للتغيير بـ `TREND_LENGTH`)
- الأهداف عند الدخول: `الدخول + (5, 10, 15)×ATR` (الفارق من `TREND_TARGET_OFFSET`، افتراضي 0)
- الوقف عند الدخول: قيمة `sma_low` من شمعة الإشارة (ثابت لا يتحرك)
- **الخروج**: إشارة البيع (signal_down) أو لمس وقف الخسارة — لا قواعد خروج خارجية
- الأهداف الثلاثة **رسائل تحقق فقط** ولا تُغلق الصفقة

> الرخصة: CC BY-NC-SA 4.0 © BigBeluga — عند نشر المؤشر أو مشتقاته أرجع الفضل.

### الانتباه التقني: `KLINE_LIMIT=499`

`sma(atr(200), 200)` تحتاج ≥ 400 شمعة حتى تصحح، لذلك البوت يجلب **499 شمعة** لكل زوج
(وهو أيضًا أقصى حد يبقى فيه وزن طلب Binance لـ klines = 2 بدل 5). لا تخفض `KLINE_LIMIT`
دون 420 وإلا لن تظهر أي إشارة.

## هيكل المشروع

- `app.py` — التطبيق الرئيسي: دورة الصفقة الكاملة + التقارير
- `src/strategy.py` — **Target Trend** (تحويل Pine → pandas) + الإعدادات
- `src/binance_client.py` — جلب بيانات Binance (شموع 1H و1m)
- `src/telegram_client.py` — إرسال رسائل تيليجرام
- `src/state.py` — حفظ حالة الصفقات المفتوحة
- `src/trade_journal.py` — دفتر الصفقات CSV (مخطط مخصص لـ Target Trend)
- `src/trade_analysis.py` — تحليل الدفتر للتقارير اليومية/الأسبوعية
- `src/halal.py` — الحكم الشرعي من cryptohalal.cc مع كاش
- `src/config.py` / `src/utils.py` — الإعدادات والأدوات
- `tests/` — اختبارات المؤشر ودورة الصفقة (20 اختبارًا)
- `tools/analyze_trades.py` — تحليل أولي لسجل الصفقات
- `tools/get_telegram_chat_id.py` — استخراج Chat ID
- `tools/create_github_repo.py` — إنشاء مستودع GitHub

> ملاحظة: مصدر البيانات `https://data-api.binance.vision` لأن `api.binance.com`
> قد يكون محجوبًا جغرافيًا في بعض البيئات، و`binance.vision` يوفّر البيانات الرسمية.

## الرسائل المرسلة

### 1) 📥 الدخول
الزوج • الفريم 1H • سعر الدخول • الهدف 1/2/3 (بنسبتها) • وقف الخسارة (بنسبته) • وقت الإشارة • الحكم الشرعي

### 2) 🎯 تحقق هدف
الزوج • سعر الدخول • سعر الهدف ونسبة الربح • الأهداف المحققة (n من 3) • الوقت والمدة • الحكم الشرعي
— **الصفقة تبقى مفتوحة**: «الإغلاق بإشارة البيع أو الوقف».

### 3) ✅/🛑 انتهاء الصفقة
رابحة (✅) أو خاسرة (🛑) • الدخول والخروج • النتيجة الصافية بعد العمولة • أعلى سعر تحقق •
الأهداف المحققة (n من 3 مع ✔/✖ لكل هدف) • سبب الإغلاق (إشارة بيع / وقف خسارة) • المدة • الحكم الشرعي

### 4) التقارير
- **يومي (00:00)**: عدد الدخول، الأهداف المحققة، الرابحة/الخاسرة، نسبة النجاح
- **تحليل يومي**: متوسط النتيجة، أكثر عملة ربحًا/خسارة، توزيع الأهداف، ملاحظات نوعية
- **أسبوعي (الأحد 00:00)**: ملخص الفترة + تحليل الدفتر (المدد، الأهداف، أفضل/أسوأ عملات)

## التشغيل

### 1) ثبّت المتطلبات
```bash
pip install -r requirements.txt
```

### 2) أنشئ ملف بيئة
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

### تشغيل دورة واحدة (مثل GitHub Actions)
```bash
python run_once.py
```

### الاختبارات
```bash
python -m pytest tests/ -q
```

## إعدادات Target Trend (متغيرات البيئة)

| المتغير | الافتراضي | المعنى |
|---|---|---|
| `TREND_LENGTH` | `10` | طول الاتجاه (Trend Length في Pine) |
| `TREND_TARGET_OFFSET` | `0` | Set Targets في Pine (0 = أهداف 5/10/15×ATR) |
| `KLINE_LIMIT` | `499` | عدد شموع 1H لكل زوج (لا تقللها دون 420) |
| `EXCLUDE_PEGGED_STABLES` | `1` | استثناء أزواج العملات المستقرة المربوطة |

## استخراج Chat ID
1. افتح البوت على تيليجرام
2. أرسل له `/start` في الخاص، أو أضفه إلى مجموعة وأرسل رسالة
3. شغّل:
```bash
set -a
source .env
set +a
python tools/get_telegram_chat_id.py
```

## إنشاء مستودع GitHub
```bash
export GITHUB_TOKEN=replace_me
python tools/create_github_repo.py my-repo-name
```

## تشغيله عبر GitHub Actions + Google Apps Script
الـ Workflow جاهز في `/.github/workflows/signal-bot-cron.yml` ويعمل عبر `workflow_dispatch`
فقط، ويُستدعى كل 5 دقائق من **Google Apps Script** باستخدام `google_apps_script_trigger.js`.

### ماذا يفعل؟
- يشغّل البوت **مرة واحدة كل 5 دقائق**
- يفحص **تحقق الأهداف والوقف داخل الساعة** باستخدام شموع 1 دقيقة
- يفحص **إشارة الدخول وإشارة البيع** عند إغلاق شمعة الساعة
- يحفظ الحالة في `data/state.json` ويعمل commit تلقائيًا عند التغيّر

### الأسرار المطلوبة في GitHub
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### إعداد Google Apps Script
1. افتح `script.google.com` وأنشئ مشروعًا جديدًا
2. الصق محتوى `google_apps_script_trigger.js`
3. من **Project Settings > Script properties** أضف:
   - `GITHUB_TOKEN`
   - `GITHUB_OWNER=turky4500`
   - `GITHUB_REPO=binance-spot-usdt-signal-bot`
   - `GITHUB_WORKFLOW=signal-bot-cron.yml`
   - `GITHUB_REF=main`
4. شغّل الدالة `createFiveMinuteTrigger` مرة واحدة ووافق على الصلاحيات

> السكربت يتحقق أولًا هل يوجد Workflow ما زال يعمل، وإذا وجد تشغيلًا نشطًا
> فإنه **لن يرسل Dispatch جديدًا** — هذا يمنع تكرار الرسائل.

## ملاحظات مهمة
- البوت يحفظ حالة الصفقات في `data/state.json`، وعند التشغيل عبر GitHub Actions يُحدَّث ويرفع تلقائيًا
- عند التوقف والعودة يحاول الاستدراك عبر الشمعة المغلقة الأخيرة
- GitHub Actions المجدول ليس بثًا لحظيًا 100%، لكنه مناسب لتشغيل دوري منخفض التكلفة
- التقييم الحقيقي يظل مرهونًا بتطابق تحويل Pine مع المنصة الفعلية وبيانات Binance

## تاريخ الأنظمة السابقة (2026-10-09)

حُذفت كل الأنظمة القديمة بالكامل بناءً على قرار المستخدم: نظام strategy القديم،
القمم والقيعان (pivots)، الظلّي (shadow + shadow_journal)، بوابات الجودة (entry_filters)،
الدخول الذكي (smart_entry)، قواعد الخروج الثلاث (exit_rules)، مختبر القياس
`tools/indicators_lab/` وأرشيفاته في `reports/`، ومتغيرات البيئة المرتبطة بها.
المؤشر المعتمد الآن وحده: **Target Trend [BigBeluga]**.
