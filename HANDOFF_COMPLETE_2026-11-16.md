# HANDOFF — دفتر التسليم الشامل والكامل

> **آخر تحديث:** 2026-11-16 00:03:50 بتوقيت الرياض (2026-11-15 21:03:50Z، من `Date` لواجهة GitHub API).
> **HEAD عند اللقطة:** `cdb862e32180f954cf6b9f5c35901ce7224609b0`.
> **الغرض:** أن يقرأه أي وكيل/مطوّر لاحقًا ويعرف كل شيء دون الرجوع إلى رسائل المحادثة: ما يعمل الآن، ما صار، ما قيس ورُفض، ما هو مفتوح، وكيف لا يكسر شيئًا.
> **النطاق الحالي الصريح:** «النظام السادس الخاص بمؤشر نداريا» / **NW 1H فقط**. العمل على أي نظام آخر مرفوض ما لم يطلبه المستخدم الآن صراحةً.
> **هذا الملف يحل محل `handoff.md` القديم كمرجع شامل**؛ النسخة القديمة (43,017 بايت / 756 سطر، آخر تعديل `0443d03ca` في 2026-11-06 20:50 الرياض) تُترك كأرشيف فقط.
> **نُشر أول مرة في commit:** `PUBLISHED_IN_COMMIT: `710e8c71146810e8427ba33963992b4850c4adc8``.

**كل الأوقات أدناه بتوقيت الرياض إلا إذا كُتب `Z`.** لا يحتوي هذا الملف على أي PAT أو توكن أو معرّف تيليجرام.

---

## 0) القاعدة الذهبية قبل أي شيء

1. **المستخدم يتحدث العربية** — كل الردود والتقارير بالعربية، بنقاط وجداول وأرقام واضحة.
2. **لا تغيير على البوت الحيّ بلا أمر صريح.** «ناقش / رأيك / ما رأيك» = تحليل فقط؛ التنفيذ يحتاج «نفّذ / اعتمد / طبّق».
3. **تصحيح نطاق ملزم (2026-11-11 ~17:41):** المستخدم قال حرفيًا «ما طلبت منك البوت السعودي… طلبي كان خاص بالنظام السادس الخاص ب مؤشر نداريا… نظام السوق السعودي لا اريده… لا تصلح اي نظام اخر مالم اطلبه منك». إذن: **لا SA، لا daily-trend، لا 4H، لا 15M، لا halal، لا dashboard، لا Telegram delivery، لا Supabase** إلا بطلب جديد صريح. إصلاح SA السابق كان خارج النطاق؛ لا توسّعه ولا ترجّعه بلا إذن.
4. **الرسائل المفقودة:** المستخدم قال «ابحث عن الرسائل المفقودة… واخبرني بها»، لا «أعد إرسالها». ⇒ إعادة بناء جافة فقط، **بلا إرسال تيليجرام وبلا كتابة حالة/دفتر**. أي resend يحتاج موافقة صريحة لاحقة.
5. **Supabase الآن ON** (فعّله المستخدم في أحدث رسالة؛ ملاحظة «OFF» السابقة أصبحت obsolete). يُسمح بالقراءة فقط؛ لا تعديل schema/secrets/functions بلا طلب.
6. **المستودع:** استخدم دائمًا **GitHub API حديث** (`/git/ref/heads/main`، `/contents`، `/git/blobs|trees|commits`، `/actions/...`). **ممنوع `git push` من النسخة المحلية** لأن `HEAD` المحلي قديم والـ `origin` فارغ؛ استخدم Git Data API.
7. **الأمان:** PAT استُخدم inline في سجلات الأدوات وهو ظاهر هناك؛ **تدويره/إبطاله عاجل**. لا تُطبع قيمته ولا تُخزّنه في ملف. فحص `grep -RIl` سابقًا لم يجد PAT داخل ملفات الـ workspace، و`/tmp/.gh_pat` كان غائبًا. لا يوجد `push` trigger ولا `ghp_` ولا معرّفات تيليجرام في الملفات المنشورة.
8. **المصادر:** raw CDN قد يتأخر ⇒ Contents/API هو المرجع. `api.binance.com` يرد 451 من هذه البيئة ⇒ استخدم `data-api.binance.vision`.
9. **لا اختراع:** لا مقدّرات بديلة، لا Webhook، لا Pine mirror، لا endpoint-slope، لا أسهم `repaint=false`، لا قوائم 3 خيارات، لا فلاتر شرعية مضافة.

### حالة الاتصال بالتطبيقات
| التطبيق | الحالة | ملاحظة |
|---|---|---|
| supabase | **ON** | فعّله المستخدم في أحدث رسالة؛ project `nwyoxdvfjfdbnezpqnkn` |
| github connector | unsupported | لا تحاول `list_connector_tools(github)`؛ استخدم API مباشرة |

---

## 1) لقطة المستودع الحية — 2026-11-16 00:03 الرياض

| العنصر | القيمة |
|---|---|
| HEAD | `cdb862e32` |
| آخر 30 commit | كلها `chore: update ... state [skip ci]` / `chore(prices)` |
| منذ إصلاح 1H `a95f301ac` | **1542 commit**، **17 ملفًا تغيّر**، و**كلها `data/` فقط** |
| تغييرات كود/وركفلو/اختبارات منذ `a95f301ac` | **صفر** ⇒ إصلاح 1H ما زال سليمًا كما نُشر |
| Commit غير-state منذ الإصلاح | **14**، كلها `chore(halal): refresh Academy rulings ...` كل 6 ساعات |
| إجمالي ملفات الشجرة | 173 |
| اختبارات `tests/*.py` | 41 |
| وركفلو `.github/workflows/*.yml` | 20 (+ Pages الديناميكي) |
| `docs/index.html` | 87,202 بايت، sha `1bac8bccc` (لوحة v7 دون تغيير) |
| `handoff.md` القديم | 43,017 بايت / 756 سطر؛ header يقول 2026-11-02 لكن آخر commit `0443d03ca` |

### الملفات الـ17 التي تغيّرت منذ `a95f301ac` (كلها بيانات تشغيلية)
`data/daily_trend/halal_academy.json` (+16/−1)، `data/daily_trend/halal_academy_meta.json` (+14/−11)، `data/halal_verdicts.json` (+7/−2)، `data/prices.json` (+1/−1)، `data/qaa/halal_verdicts.json` (+4/−2)، `data/state.json` (+627/−594)، `data/state_daily_trend.json` (+7/−2)، `data/state_nw.json` (+5762/−2842)، `data/state_nw4h.json` (+1925/−687)، `data/state_qaa.json` (+185/−126)، `data/state_sa.json` (+113/−59)، `data/trades_log.csv` (+44/−21)، `data/trades_log_nw.csv` (+73/−0)، `data/trades_log_nw4h.csv` (+1/−0)، `data/trades_log_qaa.csv` (+11/−6)، `data/trades_log_sa.csv` (+1/−1)، `data/zigzag/halal_verdicts.json` (+3/−2).

### متغيرات Actions
| المتغيّر | القيمة |
|---|---|
| `NW15_ENABLED` | `false` |
| `NW4H_ENABLED` | `true` |
| `NW_ENABLED` | غير معرّف ⇒ افتراضيًا `!= 'false'` ⇒ 1H يعمل |

### كل الوركفلوهات (ID / الحالة)
| Workflow | ID | الحالة |
|---|---:|---|
| chain-check.yml | 401753149 | active |
| daily-trend-bot.yml | 403720566 | active |
| halal-academy-refresh.yml | 414225955 | active |
| nw-bot-15m-trigger.yml | 410732266 | disabled_manually |
| nw-bot-15m.yml | 410732268 | disabled_manually |
| nw-bot-1h-guard.yml | 412275269 | active |
| **nw-bot-1h-watchdog.yml** | **414806984** | **active (جديدنا)** |
| nw-bot-4h-guard.yml | 412420073 | active |
| nw-bot-4h-report.yml | 411860857 | active |
| nw-bot-4h.yml | 411860858 | active |
| nw-bot-trigger.yml | 409220950 | disabled_manually |
| nw-bot.yml | 408673593 | active |
| nw-prices-snapshot.yml | 413140650 | active |
| nw-pulse-fallback.yml | 413293983 | active |
| qaa-signal-bot-cron.yml | 388576138 | active |
| sa-signal-bot-cron.yml | 391936426 | active |
| signal-bot-cron.yml | 375062554 | active |
| zigzag-signal-bot-cron.yml | 401005558 | active |
| zigzag-watch-loop.yml | 401741909 | active |
| zigzag-watchdog.yml | 402503514 | active |
| dynamic/pages/pages-build-deployment | 412588347 | active |

### آخر تشغيلات مهمة عند اللقطة
| النظام | Run | التوقيت | النتيجة |
|---|---|---:|---|
| nw-bot.yml (1H) | 43761470482 | 11-16 00:03 | in_progress (لقطة) |
| nw-bot.yml | 43758703879 | 11-15 23:33 | success |
| nw-bot.yml | 43755818370 | 11-15 23:03 | success |
| 1H guard | 43761459464 | 11-16 00:03 | success |
| 1H guard | 43760982588 | 11-15 23:58 | success |
| watchdog | 43761459482 | 11-16 00:03 | success |
| watchdog | 43760982558 | 11-15 23:58 | success |
| signal-bot-cron | 43761391322 | 11-16 00:02 | success |
| nw-bot-4h.yml | 43759737171 | 11-15 23:44 | success |
| 4H guard | 43761459386 | 11-16 00:03 | success |
| sa-signal-bot-cron | 43748536453 | 11-15 21:45 | success |
| halal-academy-refresh | 43716825101 | 11-15 16:01 | success |
| nw-prices-snapshot | 43758842926 | 11-15 23:34 | success |
| nw-pulse-fallback | 43755168484 | 11-15 22:56 | success |
| daily-trend-bot | 43746132551 | 11-15 21:19 | **in_progress (خارج النطاق)** |
| daily-trend-bot | 43718504715 | 11-15 16:20 | success |
| daily-trend-bot | 43718117221 | 11-15 16:16 | cancelled |
| nw-bot-trigger (معطّل) | 42578989512 | 11-09 10:23 | آخر تشغيل قبل التعطيل اليدوي |

### الحالة والبيانات الحية
| الملف | القيم |
|---|---|
| `data/state_nw.json` | `cycles=467`، `messages_sent=1456`، `last_cycle_ms=1794776613766` = **11-16 00:03:33**، `sent=400` (ممتلئ بالحد الأقصى)، `symbols=501`، `open_positions=383`، مفاتيح خاصة: `sent_legacy_repaint`, `mode_reset_at_ms`, `positions_legacy_endpoint`, `sent_legacy_endpoint`, `bad_cycle_2026_11_06_1735Z`, `daily_report_nw`, `cancelled_buy_bars`, `prev_buy_bars`, `prev_sell_bars`, `buy_history` |
| `data/state_nw4h.json` | 12 مفتاحًا، `cycles=51`، `messages_sent=170`، `last_cycle_ms=1794775474741` = 11-15 23:44:34 |
| `data/state_sa.json` | `last_processed_date=2026-11-15`، `open_trades=25`، `event_log=133` |
| `data/prices.json` | 713 رمزًا، `missing=0` |
| `data/daily_trend/halal_academy_meta.json` | `total=786`، `halal=441`، `haram=330`، `doubtful=15`، `merge_only=true`، `last_check_utc=2026-11-15T13:10:52Z` |
| `data/daily_trend/halal_academy.json` | 786 أحكامًا |
| `data/trades_log_nw.csv` | 205 سطرًا / 21,901 بايت |
| `data/trades_log_nw4h.csv` | +1 سطر منذ الإصلاح |

### Supabase (بعد تفعيله — قراءة فقط)
| العنصر | القيمة |
|---|---|
| Project | `nwyoxdvfjfdbnezpqnkn` — «turky4500's Project» |
| Org / region | `cdpbbgysnmzflfyuhcak` / `ap-southeast-2` |
| أُنشئ | 2026-11-11T10:30:54Z |
| Postgres | 17.11.0.018 / engine 17 / ga |
| Health | `auth, db, db_postgres_user, pooler, realtime, rest, storage, pg_bouncer` = **كلها ACTIVE_HEALTHY** |
| Edge functions | `items=[]` |
| الجداول | `balady_demo_requests` = 6 صفوف، `report_auth` = 1، `visits` = 3 |
| Credentials | `validate_credential` = success |
| مراجع في المستودع | لا توجد مراجع تطبيقية سابقة؛ لا تربطه بأي بوت بلا طلب |

---

## 2) قصة تعطّل نظام 1H — 9 نوفمبر 2026 (التشخيص الكامل)

### الطلب الصحيح
«ما طلبت منك البوت السعودي انا طلبي كان خاص بالنظام السادس الخاص ب مؤشر نداريا… الان فقط ابحث عن الرسائل المفقودة من تعطل نظام 1 ساعه واخبرني بها واصلح العطل واريدك ان تعمل ما يلزم لكي لا يتعطل مرة اخرى».  
اعتذرنا عن خطأ التركيز السابق على SA؛ لم يُلمس أي نظام آخر بعدها.

### زمن التعطّل
| الحدث | التوقيت الرياض | UTC | Run |
|---|---|---|---|
| آخر دورة ناجحة قبل الصمت | 10:03:00 | 07:03:00 | 42576817300 |
| بدء الصمت | 10:03:00 | 07:03:00 | — |
| أول تشغيل يعود | 18:59:40 | 15:59:40 | **42650206085 failure** |
| عودة ناجحة | 19:09:34 | 16:09:34 | 42651900409 |
| فجوة ثانية | 19:09 → 20:03 | 16:09 → 17:03 | 53.5 د |
| فجوة ثالثة | 20:44 → 21:33 | 17:44 → 18:33 | 48.3 د |
| **طول الصمت الرئيسي** | **10:03 → 18:59** | **07:03 → 15:59** | **536.7 د = 8 س 56 د** |

فجوات أقدم مسجلة: 11-06 (253.2 / 313.9 / 79 / 63.6 / 56 / 152.2 د)، 11-10 (49.6 / 47.8 د)، 11-11 (54.4 د).

### لماذا توقّف (أدلة API)
- `nw-bot.yml`: 262 تشغيلًا من 11-06 00:38 إلى 11-11 17:33؛ **فشلان فقط**: `42650206085` و`42664380566`.
- النافذة الدقيقة `created=2026-11-09T06:30:00Z..17:00:00Z` (09:30–20:00 الرياض):
  - `signal-bot-cron.yml` = **126** تشغيلًا (صفحة 100 من 11:42:19 إلى 19:57:19 بلا فجوة >20 د) ⇒ النبض الخارجي كان حيًا.
  - `nw-bot-1h-guard.yml` = **13** تشغيلًا فقط، أولها 19:03 (اثنان skipped، 19:10 dispatch ناجح، آخر 19:58 ناجح) ⇒ **سلسلة `workflow_run` انقطعت**.
  - `nw-bot.yml` = **5** (09:33 / 10:02 / 10:03 نجاح، 18:59 فشل، 19:09 نجاح).
  - `nw-pulse-fallback.yml` = **0**.
- job `129274221138` (run 42650206085): الخطوات 1–6 نجاح، الخطوة 7 `Run NW bot once` فشلت بـ `ConnectionResetError: [Errno 104] Connection reset by peer` → `urllib3.exceptions.ProtocolError` → `requests.exceptions.ConnectionError`؛ الخطوة 8 `Commit NW state updates` skipped ⇒ **لم تُحفظ حالة**.
- job `129324001490` (run 42664380566): الخطوة 2 `Guard against concurrent 1H runs` فشلت بـ `##[error]تعذّر فحص التشغيلات النشطة: HTTP Error 502: Bad Gateway`، وبقية الخطوات skipped ⇒ كسر سلسلة `workflow_run`.
- فحص لاحق: `nw-bot.yml` قبل 16:08Z لم يكن يحتوي غلاف إعادة المحاولة؛ أُضيف في `70a9c4f6` (16:08Z / 19:08 الرياض) بعد الفشل مباشرة. `run_nw1h_once_safe.py` أُضيف في `983119ae`.
- `NWBot.run_once()` يجلب `refresh_universe()` و`get_klines_for_symbols()` **قبل** `store.load()`؛ و`_evaluate_envelope()` لا يُقيّم إلا `bars_ago == 1` من `candles_closed(frames, now_ms)` ⇒ **أي دورة ضائعة لا تُسترجع لاحقًا**. هذا هو سبب فقد الرسائل لا تلف الحالة.
- ليس خلل تيليجرام/Supabase/لوحة/حالة تالفة.

### الرسائل المفقودة — 73 رسالة (أُبلغ بها المستخدم، لم تُرسل)
| النوع | العدد |
|---|---:|
| توصية دخول أول | 18 |
| إشارة دخول متكررة | 23 |
| إشارة خروج | 32 |
| **المجموع** | **73** |

حسب دورة القرار: 11:03 → 2، 12:03 → 38، 14:03 → 2، 15:03 → 1، 16:03 → 3، 17:03 → 27.  
حسب شمعة الإشارة: 09:00 → 2، 10:00 → 38، 12:00 → 2، 13:00 → 1، 14:00 → 3، 15:00 → 27.  
الوقت داخل نص الرسالة (`🕒`) هو **شمعة التأكيد = شمعة الإشارة + ساعة**، بحسب القالب.

**منهج الإثبات (dry-run):**
1. آخر حالة سليمة قبل التعطّل: commit `1f8e2764e` (10:03:34)، `cycles=165`، `messages_sent=407`، `sent=327`، كون = 496 زوجًا.
2. 1000 شمعة 1H لكل زوج من `data-api.binance.vision`، مقتطعة عند كل دورة إلى **الشموع المغلقة فقط** (لا بيانات مستقبلية، ولا إعادة رسم بأثر رجعي).
3. `NWBot.run_once()` بنفس الإعدادات الحية (`envelope`, h=8, mult=3, window=500, 560 klines) مع **عميل تيليجرام وهمي**.
4. مقارنة بـ `data/state_nw.json` و`data/trades_log_nw.csv`: **0 تطابق في `sent`**، **0 تطابق في الدفتر** ⇒ لم تُرسل ولم تُسجّل.
5. الرسائل 14 عند شمعة 17:00، و85 عند 18:00، و1 عند 19:00 **أُرسلت فعلًا** في 19:09 / 20:03 / 20:44 / 21:33؛ والفجوتان القصيرتان بعد 19:09 لم تُفقدا رسائل إضافية لأن كل ساعة متأثرة بقي فيها تشغيل ناجح واحد على الأقل.

**الملفات:** `/home/user/NW_1H_missing_messages_2026-11-09.md` (النصوص الكاملة الـ73، معروض على المستخدم) و`/home/user/NW_1H_missing_messages_2026-11-09.json`.

---

## 3) الإصلاح المنشور — commit `a95f301ac` (1H فقط)

**الرسالة:** `fix(nw-1h): prevent 9h outage recurrence — API retries, zombie expiry, state-age watchdog [skip ci]`  
**الوقت:** 2026-11-11 14:57:32Z = 17:57 الرياض. **4 ملفات لا غير:**

| الملف | التغيير |
|---|---|
| `.github/workflows/nw-bot.yml` | حاجز singleton: 3 محاولات لاستعلام GitHub API؛ تجاهل تشغيل «زومبي» أقدم من 30 د (يوازي `timeout-minutes: 25` + 5)؛ عند 502 عابر **fail-open** بـ `proceed=true` بدل تجميد الدورة، مع بقاء `concurrency` + `git reset --hard origin/main` + الحفظ التدريجي + مفاتيح `sent` كحواجز تكرار |
| `.github/workflows/nw-bot-1h-guard.yml` | 3 محاولات + انتهاء صلاحية الزومبي + عدم `raise SystemExit(1)` عند خطأ GitHub عابر (قراءة أو dispatch)؛ النبض التالي يعيد المحاولة |
| `.github/workflows/nw-bot-1h-watchdog.yml` **(جديد)** | حارس صحة مستقل: `workflow_run` من `Signal Bot Cron` (بشرط `conclusion == 'success'`) + `schedule */10` + `workflow_dispatch`. يقرأ `last_cycle_ms` من `data/state_nw.json`؛ إن زاد العمر عن **75 د** ولا يوجد تشغيل 1H حديث ⇒ يُطلق `nw-bot.yml` بـ `{"ref":"main"}`. **لا يرسل تيليجرام، لا يعيد الرسائل المفقودة، لا يقرأ حالة أي نظام آخر** |
| `tests/test_nw_1h_outage_resilience.py` **(جديد)** | 4 اختبارات تمنع عودة: 502 يُسقط المهمة، زومبي بلا expiry، غياب حارس مستقل، وجود `push` trigger |

**تحقق فوري (11-11):**
- `python3 -m pytest tests/ -q` ⇒ **471 passed**.
- حارس الصحة run `43043706171` نجح: «✅ حالة 1H طازجة: آخر دورة 2026-11-11 14:33:54Z (عمر 24.9 دقيقة) — لا إجراء».
- أول دورة 1H بعد النشر run `43044442569` نجحت؛ الحاجز طبع «✅ لا يوجد تشغيل أقدم نشط — متابعة الدورة»؛ `NW state pushed on attempt 1`.
- مطابقة الملفات الأربعة بين المحلي والـ remote عبر Contents API = True.

**تحقق لاحق (حتى 11-16 00:03):**
- **لا تغيير على أي كود/وركفلو/اختبار منذ `a95f301ac`** (compare: 17 ملفًا كلها `data/`).
- `nw-bot.yml` منذ 11-11 15:03Z: **208 تشغيلًا**، أولها وآخرها success؛ **أكبر فجوة 58.6 د**، و16 فجوة بين 40–59 د فقط، وكلها أقل من عتبة الحارس 75 د ⇒ **لم يتكرر صمت متعدد الساعات**.
- `nw-bot-1h-watchdog.yml`: **1236 تشغيلًا**، **`status=failure` = 0**.
- إجمالي failures التاريخية: `nw-bot.yml` = 2 (`42650206085`, `42664380566`)؛ `nw-bot-1h-guard.yml` = 1 (`42672930866` في 11-09 21:18، 0 jobs، بلا أثر على البوت)؛ watchdog = 0.

---

## 4) الأنظمة الأخرى — تاريخ مهم، **خارج النطاق الحالي، لا تلمسها**

### Halal / أكاديمية البيتكوين (مكتمل، يعمل تلقائيًا)
- الوركفلو `halal-academy-refresh.yml`: `0 */6 * * *` + `workflow_dispatch`، `GITHUB_TOKEN` فقط، `[skip ci]`، **بلا `push` trigger**، يكتب فقط `data/daily_trend/halal_academy.json` + `halal_academy_meta.json`.
- `tools/update_halal_academy.py` + `tests/test_halal_academy_refresh.py` (21 اختبارًا) + تحديث `tests/test_halal_primary_source.py`؛ `src/halal.py` وأ tests الأخرى قُرئت فقط.
- النشر: `8da6a1fb`→`793affa7` (7 ملفات)؛ dispatch `42966520034` جزئي 736؛ `b663c159`→`42968550933` فشل؛ `78e1a867`→`42968728139` نجاح نهائي؛ meta `4cd0306c`. 3 dispatch / 0 push. حينها: 771/432/325/14، `unknown=[]`، 766 صفًا/765 فريدًا/814 مقالًا، `ambiguous=["STRK"]`، `unclear_articles=["PI"]`، `retained_not_on_site=6`.
- **الآن (11-15 13:10Z): 786/441/330/15** بسبب 14 تحديثًا دوريًا لاحقًا؛ `merge_only=true`، لا حذف ولا اختراع أحكام.
- قواعد المستخدم الملزمة: تحديث كل 6h، merge-only، تحديث المتغيّر فقط، إضافة newly ruled/previously unruled، **عدم حذف مفاتيح غائبة**، عدم اختراع حكم، الكتابة على ملفي `daily_trend` فقط، بلا تعديل بوت/حالة/دفتر/إشارة/تقرير وبلا `push` trigger.
- مصادر/دروس: HTML المرئي 12 `<h2>` فقط؛ intro «221» يتعارض مع 766؛ `قيد المراجعة`/«اقرأ التقييم» ليست أحكامًا؛ خريطة حلال→0/حرام→1/مشبوهة→2؛ AJAX يحتاج `hc_js_gate=1`؛ Jina بـ `?cpage=N&btca_q&_=<ns>` حتى p77؛ p78 يكرر p77؛ REST-only لـ `GO`,`SD`,`DLICOM` غير صالح؛ أسماء مستعارة WORLDCOIN→WLD، MANTRA→OM، HYPERLANE/HYPERCHAINX→HYPER، STARKNET→STRK (تعارض `STRK` تُرك)، HEIMA/LIT→HEI؛ `PI` يُبقى 0، `AUDIO`→2؛ بوابات job-level `if` لا `exit 0`.

### NXPC / إعادة الرسم (تشخيص بلا تعديل)
- `src/nw_envelope.py` نقل حرفي لـ LuxAlgo Pine v5: `EnvelopeConfig(bandwidth=8,mult=3,window=500,repaint=True)`، `sae=Σ|src−nwe|/499*3`؛ ▲ إذا `src[i]<nwe[i]−sae` و`src[i+1]>lower`، ▼ إذا `src[i]>upper` و`src[i+1]<upper`. التسميات التاريخية قد تتغيّر.
- أول ▲ 11-09 00:00 / دخول 01:00؛ أول ▼ 08:00 / خروج 09:00؛ ثاني ▲ 11-10 06:00 / دخول 07:00؛ ▲ 08:00؛ ▼ بأثر رجعي 12:00.
- المستخدم اختار `keep_open` + `none` ⇒ **صفر تعديل، بلا تسوية**. الخروج التلقائي اللاحق عند 13:00/14:00 لم يكن منّا.
- `src/nw_signals.py`: `mode="envelope"`, `max_age_bars=1`, `require_confirm_green=True`, `require_exit_confirm_green=False`، `bars_ago==1` فقط؛ `nw_bot.py` يبيع فقط `pos` مفتوحًا بـ `exit_reason="red_label"`؛ `kline_limit 560`؛ `NW_SHOW_REPAINT_WARNING=0`.
- درس تقني: `exec(compile(remote src/nw_envelope.py), ..., {'__name__':'nw_env'})` يفشل مع `dataclass(slots=True)`؛ سجّل `sys.modules[name]=module` أولًا.

### SA / السوق السعودي — **خارج النطاق، لا توسّعه ولا ترجّعه**
- run `42887301567` فشل `ModuleNotFoundError: No module named 'yaml'`؛ `ea4ab479c` أضاف PyYAML؛ dispatch `43038737590` فشل 14/452 بسبب `TELEGRAM_MUTE=1` + اختبار `test_nw_4h.py` عدّادات صفرية قديمة؛ `0f3f4fc58` نشر الوركفلو + الاختبار؛ dispatch `43039283778` نجح `466 passed in 3.64s` + «TELEGRAM_MUTE=1 — رسالة لم تُرسل» + `State pushed on attempt 1`؛ state `927e8205c`؛ أسعار `43039606595` نجحت (713/0).
- المستخدم رفض هذا العمل صراحةً بعدها؛ تركنا الـ commits كما هي ولم نستمر. لا `git checkout` ولا إلغاء بلا إذن.
- درس: إضافة PyYAML وحدها لا تكفي؛ `TELEGRAM_MUTE=1` على مستوى المهمة يكتم `TelegramClient` في الاختبارات ⇒ استخدم `TELEGRAM_MUTE=0 python -m pytest tests/ -q` inline، ولا تضع `env: TELEGRAM_MUTE:"0"` على خطوة لأن `test_workflow_env_consistency.py::_envs` يتوقع «1» على مستوى المهمة.

### 4H (مكتمل، لا تتراجع عنه)
- native-only، حارس من وظيفتين، شرط صارم `workflow_run.conclusion=='success'`، `vars.NW4H_ENABLED!='false'`، نجاح واحد لكل شمعة 4H، active-run/`last_report_day`/فجوة 5 د، singleton، بلا stale local push وبلا `git rebase -X theirs`، `age_min<5` بلا cooldown 20h.
- تقرير `42904205564` بالسجل الحرفي: «✅ أُرسل تقرير NW 4H عن يوم 2026-11-10 (مفتوحة=8 مغلقة_جديدة=0 توصيات_اليوم=6)»؛ state `a6871bb7`؛ المستخدم أكّد.
- دروس مرفوضة: `dt.hour==0` بـ UTC، `report_day` باليوم الحالي UTC، مساواة UTC، إعادة جمع كل التاريخ، `messages_sent`/`cycles`/مفاتيح كل الوقت/`bar_time` بلا interval.

### 15M
- صمت تام: **لا حذف، لا reset، لا إعادة تفعيل**؛ الوركفلوان `410732268` و`410732266` `disabled_manually`؛ `NW15_ENABLED=false`.

### 1H — التكرار القديم
- التريغر `409220950` `disabled_manually` ولا يُعاد تفعيله؛ سبب التكرار كان `workflow_run` على SHA مثبّت + `sent` قديم. لا يعتمد على `_send_once()`/العدّادات وحدها.
- `sent` بحد أقصى 400؛ المفتاح `bar_time+interval_ms`؛ `confirm_bar_time` لا وقت الـ dispatch؛ `entry_blocked_until` ليس رسالة.

### Dashboards / Pages / fallback
- `docs/index.html` v7 = 87,202 بايت، مطابق للمنشور؛ commits `f9be1061528c`, `5b4ac4d3`, `f97e16d9`, `58c190a3`, `19013cdc`, `5dcfbba2`؛ fallback `f23b91d5` + docs `d96b1b47` + run `42821034086` نجاح؛ snapshot `19013cdc`.
- محرك v7 يدمج Jina + peer + snapshot + Binance REST/WS؛ `allfail` يجب أن يمنع `PEER_PRICES`؛ WS وحده يفوت الرموز المفتوحة؛ `data/status.json` الأساسي 404 متوقّع لأن اللوحة تقرأ peer؛ `/data/prices.json` و`/data/state_nw.json` على Pages الأساسي 404 متوقّع.
- peer `https://turky4500.github.io/crypto-signals-arabic/data/current_signals.json` كان 454 صفًا؛ `allorigins/codetabs` 522 و`corsproxy.io` 401؛ استخدم `data-api.binance.vision`.

### Daily-trend (خارج النطاق)
- سابقًا `42997977633` in_progress 12:46 و`43028630083` pending 16:23 بسبب `concurrency: group: daily-trend-bot` + `cancel-in-progress:false`؛ logs كانت 302→BlobNotFound أثناء in_progress. **الآن** لم يعدا ظاهرين؛ الحالي `43746132551` in_progress 11-15 21:19 و`43718504715` success 16:20. لا تلمس/تُلغِ/تُطلق بلا طلب.
- `daily-trend-bot.yml` فيه `git rebase -X theirs` خاص به؛ تحذير `-X theirs` الآخر خاص بـ 4H فقط.

---

## 5) القيود الصريحة للمستخدم — لا تحذف ولا تخفّف

1. **النطاق الحالي:** النظام السادس / NW 1H فقط؛ «لا تصلح اي نظام اخر مالم اطلبه منك». واصل **نفس المحادثة**، ولا تؤجّل التشخيص/الإصلاح خلف سؤال توضيحي.
2. **الرسائل المفقودة:** «اخبرني بها» ⇒ تقرير + dry-run مكتوم + dedup ضد `data/state_nw.json` / `data/trades_log_nw.csv`؛ **لا resend** ولا تعديل أنظمة أخرى بلا موافقة.
3. **NXPC:** `keep_open` + `none`؛ بلا إصلاح دائم/تسوية/تعديل.
4. **Halal:** refresh كل 6h من أكاديمية البيتكوين؛ merge-only؛ حدّث المتغيّر فقط؛ أضف newly ruled/previously unruled؛ لا حذف غائب ولا اختراع؛ اكتب فقط ملفي `daily_trend`؛ بلا تعديل بوت/حالة/دفتر/إشارة/تقرير وبلا `push` trigger.
5. **15M:** صمت؛ بلا حذف/reset/إعادة تفعيل.
6. **4H:** لا ترجّع native-only / حارس الوظيفتين / الشرط الصارم / `vars.NW4H_ENABLED!='false'` / نجاح واحد لكل شمعة / singleton / بلا stale push وبلا `-X theirs`.
7. **1H duplicates:** لا تُعد تفعيل `409220950`؛ لا اعتماد على `_send_once()`/العدّادات وحدها.
8. **لا اختراع:** لا مقدّرات بديلة، لا Webhook، لا Pine mirror، لا endpoint-slope، لا أسهم `repaint=false`، لا قوائم 3 خيارات. `NW_KLINE_LIMIT=501` سبّب دورات بلا رسائل؛ `entry=0` ليس دليلًا.
9. **الأمان/النشر:** API حديث فقط، بلا stale `git push`؛ بلا `push` trigger؛ بلا PAT/ghp/Telegram IDs في الملفات المنشورة؛ دور PAT عاجل.
10. **Supabase:** الآن ON؛ سابقًا كان OFF وتلك الملاحظة ملغاة. قراءة فقط بلا تعديل بلا طلب.

---

## 6) سجل الـ commits المهمة

| Commit | المعنى |
|---|---|
| `6858e457` / `1f8e2764` | تعطيل تريغر 1H المكرّر + `NW15_ENABLED=false` |
| `70a9c4f6` / `7d0238b3` / `983119ae` | إصلاح 1H: guarded kick + singleton + fresh checkout + `run_nw1h_once_safe.py` + اختبارات/توثيق (11-09 18:58/19:08) |
| `ec39bac1` → `bdc1fc5d` | نشر 4H (8 ملفات/وركفلوهات) |
| `f9be1061528c`, `5b4ac4d3`, `f97e16d9`, `58c190a3`, `19013cdc`, `5dcfbba2` | لوحات/أسعار v7 |
| `f23b91d5` / `d96b1b47` | نبض fallback + توثيق؛ run `42821034086` |
| `80c6bb66` / `c4711bce` | تقرير 4H اليومي؛ dispatch `411860857`، run `42904205564`، state `a6871bb7` |
| `8da6a1fb` → `793affa7` | ملفات Halal (7) |
| `b663c159` / `78e1a867` / `4cd0306c` | إصلاح تحديث Halal + meta نهائي |
| `ea4ab479c` / `0f3f4fc58` | إصلاح SA **خارج النطاق** (لا توسّع/لا ترجّع) |
| **`a95f301ac`** | **إصلاح تكرار عطل 1H: retries + zombie expiry + watchdog + اختبارات** |
| `0443d03ca` | آخر تعديل على `handoff.md` القديم |
| `41ad54c14` … `f1762cc09` | 14 تحديثًا دوريًا لـ Halal (11-12 → 11-15) |
| `cdb862e32` | HEAD عند هذه اللقطة |

### سجل الـ runs المهمة
| Run | المعنى |
|---:|---|
| `42650206085` | فشل 1H بـ `ConnectionResetError` (18:59 11-09) |
| `42664380566` | فشل حارس 1H بـ `HTTP 502` (20:24 11-09) |
| `42672930866` | فشل حارس 1H وحيد لاحق (21:18 11-09، 0 jobs) |
| `42821034086` | نجاح pulse fallback |
| `42904205564` | نجاح تقرير 4H |
| `42966520034` / `42968550933` / `42968728139` | Halal: جزئي / فشل / نجاح نهائي |
| `43038737590` / `43039283778` | SA: فشل / نجاح (خارج النطاق) |
| `43039606595` | نجاح لقطة الأسعار |
| `43043706171` | نجاح اختبار حارس صحة 1H |
| `43044442569` | أول دورة 1H ناجحة بعد نشر `a95f301ac` |
| `43746132551` | daily-trend in_progress الحالي (خارج النطاق) |
| `43761470482` | 1H in_progress عند اللقطة |

---

## 7) خريطة الملفات والمسارات

| المسار | المحتوى / الأهمية |
|---|---|
| `/home/user/research/signal-bot/` | نسخة محلية `## main` **قديمة**، `origin` فارغ؛ 10 معدّلة + 14 غير متتبَّعة؛ لا commit محلي. **لا تستخدم `git push`** |
| `/home/user/NW_1H_missing_messages_2026-11-09.md` | تقرير الرسائل الـ73 الكامل (معروض على المستخدم) |
| `/home/user/NW_1H_missing_messages_2026-11-09.json` | نفس البيانات آليًا |
| `/home/user/HANDOFF_COMPLETE_2026-11-16.md` | هذا الملف (نسخة الـ workspace) |
| `/home/user/README_HALAL_REFRESH.md`, `/home/user/README_NW_4H_REPORT_FIX.md` | تقارير محلية سابقة |
| `/home/user/publish_halal.py`, `halal_refresh_dryrun.json`, `btcacademy_directory_full*.json`, `btcacademy_posts.json`, `btcacademy_page.html` | artifacts محلية فقط لـ Halal |
| `handoff.md` (repo) | أرشيف قديم 43KB؛ هذا الملف يحل محله |
| `README_NW_1H_FIX.md`, `README_NW_4H.md`, `README_NW_SYSTEM.md`, … | توثيق الأنظمة في الـ repo |
| `docs/index.html` | لوحة v7، 87,202 بايت |
| `run_nw1h_once_safe.py`, `run_nw4h_once_safe.py` | أغلفة الحفظ التدريجي ومنع تكرار الدفتر |
| `tests/` | 41 ملفًا؛ `test_nw_1h_outage_resilience.py` و`test_nw_1h_guard.py` و`test_halal_academy_refresh.py` الأحدث |
| `/tmp/nw_replay/` | ** ephemeral خارج `/home/user` **: كود الـ 11 ملفًا + `klines.json` + `state_at_outage.json` + `lost_messages.json` + `replay.py`؛ لا يُعتمد عليه عبر الرسائل |
| `/tmp/nw_engine.js`, `/tmp/dashdiag/got.json`, `/tmp/sa_run_id.txt`, `/tmp/price_run_id.txt` | ephemeral أيضًا |

---

## 8) كيف تتحقق سريعًا دون أن تكسر شيئًا

```bash
# HEAD والمتغيرات والوركفلوهات — دائمًا API حديث
curl -H "Authorization: Bearer $GH_PAT" \
  https://api.github.com/repos/turky4500/binance-spot-usdt-signal-bot/git/ref/heads/main
curl -H "Authorization: Bearer $GH_PAT" \
  https://api.github.com/repos/turky4500/binance-spot-usdt-signal-bot/actions/variables
curl -H "Authorization: Bearer $GH_PAT" \
  https://api.github.com/repos/turky4500/binance-spot-usdt-signal-bot/actions/workflows

# حالة 1H / الحارس / النبض
.../actions/workflows/408673593/runs?per_page=5
.../actions/workflows/412275269/runs?per_page=5
.../actions/workflows/414806984/runs?per_page=5
.../actions/workflows/375062554/runs?per_page=5
.../contents/data/state_nw.json?ref=main

# اختبارات (محليًا، مع الحالة الحية لا المخمّدة)
cd /home/user/research/signal-bot
TELEGRAM_MUTE=0 python3 -m pytest tests/ -q     # 471 passed عند آخر تحقق
python3 -c "import yaml,pathlib;[yaml.safe_load(p.read_text()) for p in pathlib.Path('.github/workflows').glob('*.yml')]"
```

**نقاط تحقق ثابتة:** لا `push` في أي `on`؛ `NW_ENABLED` افتراضيًا يعمل؛ `409220950` و15M معطّلان؛ watchdog `414806984` active؛ `last_cycle_ms` عمره <75 د؛ `sent` ≤400؛ `messages_sent` يتزايد بلا قفزات مكرّرة.

---

## 9) أخطاء ومسدودات موثّقة (لا تُعد تجربتها)

- **النطاق:** لا تعتبر SA/daily-trend هو العطل النشط؛ تصحيح المستخدم ملزم. لا تُصلح/توسّع/ترجّع SA بلا إذن.
- **المستودع:** local HEAD stale، remote فارغ؛ `git ls-remote origin` و`git push` يفشلان/يُفسدان. استخدم Git Data API. `list_connector_tools(github)` = unsupported.
- **PAT:** ظهر inline في سجلات الأوامر؛ افحص `grep -RIl` ولا تخزّنه؛ التدوير عاجل. لا تطبع قيمته في هذا الملف.
- **تشخيص 11-09:** سكربت فيه خطأ `f(s)` ضاعف `+00:00`؛ نافذة UTC 00:00–24:00 طُبعت كأنها الرياض وأعطت أرقام خاطئة لـ cron/guard ⇒ اعتمد فقط `created=2026-11-09T06:30:00Z..17:00:00Z`، ومع ذلك `signal-bot-cron` أعاد 126 لكن صفحة 100 فقط ⇒ «بلا فجوة >20 د» تنطبق على الصفحة المعادة فقط.
- **سجلات jobs:** قد تحتوي سطور `::error`/`::warning` من **نص السكربت نفسه**؛ صفِّ ANSI/group قبل اعتبارها فشلًا.
- **daily-trend:** logs أثناء in_progress كانت 302→BlobNotFound؛ run/attempt logs 415؛ timing 0ms؛ لا تُطلق run جديدًا قبل فضّ الـ concurrency إن عاد الموضوع يومًا.
- **SA:** PyYAML وحده لا يكفي؛ `TELEGRAM_MUTE=1` يكتم الاختبارات؛ `test_nw_4h.py` عدّادات صفرية غير صالحة أمام حالة 4H الحية؛ محاولة Contents overwrite غير مُصادَقة فشلت `KeyError:'content'` وقطّعت CSV محليًا ثم استُعيد بـ `git checkout --`.
- **المتصفح/البيانات:** `api.allorigins.win`/codetabs 522، `corsproxy.io` 401، `api.binance.com` 451؛ `data-api.binance.vision` 200/`*`؛ raw CDN متأخر؛ `r.jina.ai` 200؛ WS وحده ينقص الرموز؛ `primary data/status.json` 404 ليس عطلًا.
- **الحالة:** `sent` cap 400؛ `bar_time+interval_ms`؛ `confirm_bar_time` لا dispatch time؛ `entry_blocked_until` ليس رسالة؛ `entry=0` ليس دليلًا؛ `NW_KLINE_LIMIT=501` سبّب دورات صفرية.
- **v7 DOM:** `ws_only` فشل؛ stub Node إثبات منطق فقط لا متصفح.
- **NXPC:** لا تقبل تسميات الشارت بأثر رجعي كسببية؛ `dataclass(slots=True)` يحتاج تسجيل `sys.modules` قبل `exec`.
- **Halal:** HTML المرئي 12 بطاقة فقط؛ `قيد المراجعة` ليس حكمًا؛ p78 يكرر p77؛ Jina حتى p77؛ أسماء مستعارة خطرة؛ `STRK` متعارض تُرك؛ fixture dry-run ≠ إثبات حي.

---

## 10) ما هو مفتوح / خطوات تالية (بلا تنفيذ تلقائي)

| البند | الحالة | الإجراء المطلوب |
|---|---|---|
| تدوير PAT | **عاجل** | المستخدم يعمل revoke/rotate؛ لا نلمس السر نيابة عنه |
| إعادة إرسال الرسائل الـ73 | **لم تُطلب** | لا ترسل؛ إن قال «أعد إرسالها» صراحةً ⇒ خطّة dedup + موافقة أولًا |
| daily-trend `43746132551` in_progress | خارج النطاق | لا clear/cancel/dispatch بلا طلب |
| Supabase | ON، سليم | قراءة فقط؛ أي schema/secret/function تغيير يحتاج طلبًا صريحًا |
| النسخة المحلية `/home/user/research/signal-bot` | stale مقابل الـ remote | دائمًا API؛ لا `git push` |
| عتبة الحارس 75 د | تعمل؛ أكبر فجوة حديثة 58.6 د | إن أراد المستخدم عتبة أضيق ⇒ اسأله قبل التغيير |
| `handoff.md` القديم | أرشيف | هذا الملف هو المرجع؛ لا تحذف القديم |
| فجوات 40–59 د بعد الإصلاح | 16 فجوة، كلها success | مراقَبة؛ لم تصل لعتبة الاستعادة، ولا تُعدّ تكرار العطل |
| لا تعديلات كود منذ `a95f301ac` | مؤكَّد | أي تغيير جديد يجب أن يمر بالاختبارات 471 + فحص `on.push` |

### حالة قبول طلب المستخدم (مكتمل)
- ✅ واصلنا نفس المحادثة على **النظام السادس / NW 1H فقط**.
- ✅ اعتذرنا عن خطأ SA ولم نلمس أنظمة أخرى.
- ✅ وجدنا **73 رسالة مفقودة** وأخبرنا المستخدم بها مع النصوص الكاملة، كـ dry-run بلا إرسال.
- ✅ أصلحنا العطل (`a95f301ac`) بثلاث طبقات: retry/backoff، zombie expiry، حارس صحة مستقل قائم على عمر الحالة.
- ✅ أضفنا اختبارات وتحققًا حيًا، ومنعنا التكرار: لا رسائل مكرّرة ولا تعديل حالات أخرى.
- ✅ حدّثنا هذا الـ handoff بكل تحديث مهم على المستودع حتى 2026-11-16 00:03 الرياض.

---

> **تذكير أخير:** أي وكيل يبدأ من هنا يقرأ §0 قبل أي سطر كود، ثم يتحقق من HEAD حديثًا عبر API، ثم ينفّذ **فقط** ما طلبه المستخدم الآن صراحةً.
