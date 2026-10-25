/**
 * مستقبل TradingView Webhook ← تيليجرام — الطريق الثاني لنظام Smart Flow
 * (جاهز للاستخدام في اليوم الذي يشترك فيه المالك في TradingView؛
 * بدون اشتراك لا يمكن إنشاء أي تنبيه على TradingView إطلاقًا).
 *
 * الفكرة: تنبيه TradingView على نسخة البائع المغلقة نفسها (أي تحديث للنسخة
 * الأصلية يلتقط فورًا) → هذا المستقبل المجاني (Google Apps Script) → تيليجرام.
 *
 * خطوات الإعداد (مرة واحدة):
 *  1) script.google.com → مشروع جديد → الصق هذا الملف كاملًا.
 *  2) Deploy > New deployment > Web app:
 *       Execute as: Me   |   Who has access: Anyone
 *     وانسخ رابط الـ /exec الناتج.
 *  3) Project Settings > Script properties — أضف:
 *       TELEGRAM_BOT_TOKEN  = توكن بوت تيليجرام
 *       TELEGRAM_CHAT_ID    = رقم الدردشة المستهدفة
 *  4) في TradingView: Create Alert →
 *       Condition  = Smart Flow PRO MAX + Last 2 Signals 🔥 (نسخة البائع)
 *       Trigger    = «شراء» أو «بيع» (أو Any alert() function call)
 *       Message    = نص الرسالة مع الحقول الديناميكية (انظر القالب أدناه)
 *       Notifications = Webhook URL → رابط الـ /exec
 *
 * قالب رسالة مقترح في خانة (Message) داخل التنبيه:
 *   📥 إشارة من نسخة البائع المغلقة
 *   الزوج: {{ticker}} والفريم: {{interval}}
 *   سعر الإغلاق: {{close}} ووقت الإشارة: {{time}}
 *   (سيصل هذا النص كما هو إلى تيليجرام مع حلول الحقول آليًا)
 */

const SCRIPT_PROPS = PropertiesService.getScriptProperties();

function doPost(e) {
  try {
    const body = e.postData ? e.postData.contents : "";
    if (!body) {
      sendToTelegram("⚠️ تنبيه TradingView وصل بدون محتوى (body فارغ)");
    } else {
      sendToTelegram(body);
    }
    return ContentService.createTextOutput("ok");
  } catch (err) {
    try {
      sendToTelegram("⚠️ خطأ في مستقبل Webhook: " + err);
    } catch (_) {
      // تجاهل — لا شيء يمكن فعله بعد فشل الإرسال نفسه
    }
    return ContentService.createTextOutput("error");
  }
}

/** إرسال نص جاهز إلى تيليجرام (بدون تنسيق إضافي) */
function sendToTelegram(text) {
  const token = SCRIPT_PROPS.getProperty("TELEGRAM_BOT_TOKEN");
  const chatId = SCRIPT_PROPS.getProperty("TELEGRAM_CHAT_ID");
  if (!token || !chatId) {
    throw new Error("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in Script properties");
  }
  // TradingView يرسل نص الرسالة خامًا (يُرسَل كما كتبه صاحب التنبيه)
  UrlFetchApp.fetch("https://api.telegram.org/bot" + token + "/sendMessage", {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify({
      chat_id: chatId,
      text: text,
      disable_web_page_preview: true,
    }),
    muteHttpExceptions: true,
  });
}
