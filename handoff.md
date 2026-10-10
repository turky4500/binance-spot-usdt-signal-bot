# Handoff — دفتر التسليم

> **آخر تحديث: 2026-10-09** — بعد تنفيذ قرار المستخدم: حذف كل الأنظمة القديمة
> والاعتماد على مؤشر واحد فقط.

## الهدف العام

بوت يراقب جميع أزواج **Binance Spot / USDT** على **فريم 1H** ويطبّق مؤشرًا واحدًا معتمدًا،
ويرسل إلى تيليجرام:
1. رسالة دخول شراء
2. رسائل تحقق الأهداف الثلاثة
3. رسالة انتهاء الصفقة (رابحة/خاسرة + الأهداف المحققة)

وكل رسالة متبوعة بـ **الحكم الشرعي** من `cryptohalal.cc` مع كاش محلي.

المستودع: `https://github.com/turky4500/binance-spot-usdt-signal-bot`

## القرار الحاكم (2026-10-09)

**المؤشر المعتمد الوحيد: Target Trend [BigBeluga]** (Pine v5 → `src/strategy.py`).

### ما أُلغي بالكامل
- `src/strategy.py` القديم (نظام القمم والقيعان الأصلي)
- `src/strategy_pivots.py` + متغير `PIVOT_STRATEGY`
- `src/strategy_shadow.py` + `src/shadow_journal.py` + متغير `STRATEGY_SHADOW`
- `src/entry_filters.py` (بوابات rvol/DI) + متغيرات `GATE_*`
- `src/smart_entry.py` (النافذة الزمنية) + `SMART_ENTRY` / `ENTRY_HOURS` / `MAX_OPEN_TRADES`
- `src/exit_rules.py` (hybrid/trailing/fixed) + `EXIT_MODE` / `TRAIL_*` / `REFERENCE_TARGET_PCT` / `HYBRID_LOCK_PCT`
- `QUIET_ENTRY_SIGNALS` (وضع الهدوء) — لم يعد له معنى بلا نظام قديم
- `tools/indicators_lab/` كاملًا + `tools/replay_exit_rules.py` + `tools/strategy_shadow_report.py`
- `reports/` كاملًا (أرشيفات القياس والخطط)
- كل الاختبارات القديمة (9 ملفات) — استُبدلت بـ `tests/test_target_trend.py` و `tests/test_bot_flow.py`

### ما بقي كما هو
- الحكم الشرعي `src/halal.py` + كاش `data/halal_verdicts.json`
- تيليجرام `src/telegram_client.py`
- `src/binance_client.py` و `src/state.py` و `src/utils.py` و `src/config.py`
- دفتر `data/trades_log.csv` (بمخطط جديد مخصص لـ Target Trend)
- التقارير اليومية/الأسبوعية + `run_once.py` + `google_apps_script_trigger.js`
- الوركفلو `.github/workflows/signal-bot-cron.yml` (بمتغيرات بيئة جديدة)

### بيانات
**حذف نهائي وبدء نظيف** (قرار المستخدم): أُزيل `data/state.json` القديم (22 صفقة من
الأنظمة السابقة) و`data/trades_log.csv` القديم (201 صفقة بمخطط قديم). يبدأ البوت
بلا صفقات مفتوحة ودفتر فارغ.

## منطق Target Trend (كما هو في Pine)

```
atr_value = sma(atr(200), 200) * 0.8
sma_high  = sma(high, length) + atr_value
sma_low   = sma(low,  length) - atr_value

trend = true  عند crossover(close, sma_high)
trend = false عند crossunder(close, sma_low)

signal_up   = trend: false → true   → دخول
signal_down = trend: true → false   → إغلاق
```

- **الدخول**: إغلاق شمعة التقاطع الصاعد، `entry = close`، `stop = sma_low` من نفس الشمعة
- **الأهداف**: `close + (5,10,15)×atr_value` — الأول والثاني رسائل تحقق فقط؛ تحقق الثالث
  يُغلق الصفقة تلقائيًا وتُسجل ناجحة `exit_reason=all_targets` (قرار المستخدم 2026-10-10)
- **الخروج**: signal_down أو لمس الوقف أو تحقق الأهداف الثلاثة — لا قواعد خروج خارجية (قرار المستخدم)
- **تصنيف النتيجة** (2026-10-10، مُعدَّل): الأهداف الثلاثة كاملة → `win` (تُغلق تلقائيًا) •
  هدف واحد أو هدفان (1-2 من 3) أيًا كانت النتيجة → `partial` (لا تُحتسب ناجحة ولا خاسرة) •
  دون أهداف وصافي موجب → `win` • دون أهداف وصافي سالب → `loss` —
  نسبة النجاح = رابحة ÷ (رابحة + خاسرة)
- `length = 10` عبر `TREND_LENGTH`، فارق الأهداف عبر `TREND_TARGET_OFFSET` (افتراضي 0)
- **`KLINE_LIMIT=499` إلزامي**: الـ ATR المسطّح يحتاج ≥400 شمعة، و499 هو أقصى حد
  يبقى فيه وزن طلب Binance = 2 (500+ يصبح 5)

## آلية التشغيل

1. **Google Apps Script** يرسل `workflow_dispatch` كل 5 دقائق
   (مع حماية من التداخل: لا يُرسل إن كان هناك تشغيل نشط)
2. الوركفلو يشغّل `run_once.py` → `bot.run_once()`
3. كل دورة: تحديث الرموز (عند الاقتضاء) → كاش الحكم الشرعي → مزامنة الدفتر →
   متابعة المفتوحة على شموع **1m** (أهداف + وقف) → معالجة الشمعة المغلقة 1H
   (خروج بإشارة البيع ثم دخول بإشارة جديدة) → التقارير اليومي/التحليلي/الأسبوعي
4. commit تلقائي لـ `data/` مع آلية إعادة محاولة عند تعارض الرحلات المتزامنة

- **الاستدراك**: عند التوقف يعالَج كل شمعة مفلتة حتى 24 ساعة إلى الوراء
- **التعويم**: أزواج العملات المستقرة المربوطة (USDC/FDUSD/...) مستثناة افتراضيًا
  (`EXCLUDE_PEGGED_STABLES=1`) لأن هدف ATR عليها ضجيج يقتل الوقف

## الحالة الحالية والخطوة التالية

- ✅ التنفيذ مكتمل محليًا: الكود، الحذف، الوركفلو، `.env.example`، README، الاختبارات (15/15 PASS)
- ⏳ لم يُنشأ commit أو push بعد
- **الخطوة التالية**: مراجعة المستخدم ثم commit وpush إلى `main`، ثم التأكد من أول
  تشغيل حيّ للوركفلو (التحقق من وصول الرسائل وظهور `Loaded N spot symbols`)

## قرارات سابقة ما زالت سارية

- فريم **1H فقط**، **سبوت فقط**، بلا سقف للصفقات وبلا بوابة زمنية
- العمولة: 0.1% لكل جهة (تُخصم في `net_return_pct`)
- التقرير اليومي عند 00:00 بتوقيت `TIMEZONE` (افتراضيًا Asia/Riyadh)، والأسبوعي يوم الأحد
- `git rebase -X theirs` مع 5 محاولات لحل تعارض حالة الرحلات المتزامنة

## أوامر مفيدة

```bash
pip install -r requirements.txt      # المتطلبات
python -m pytest tests/ -q           # الاختبارات (20)
python run_once.py                   # دورة واحدة
python app.py                        # تشغيل مستمر محلي
python tools/analyze_trades.py       # تحليل الدفتر
python tools/get_telegram_chat_id.py # استخراج Chat ID
```

على ويندوز: شغّل الاختبارات مع `PYTHONIOENCODING=utf-8` وإلا `UnicodeEncodeError` على الإيموجي.
