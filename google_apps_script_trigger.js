function triggerCryptoSignalWorkflow() {
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty('GITHUB_TOKEN');
  const owner = props.getProperty('GITHUB_OWNER') || 'turky4500';
  const repo = props.getProperty('GITHUB_REPO') || 'binance-spot-usdt-signal-bot';
  const workflow = props.getProperty('GITHUB_WORKFLOW') || 'signal-bot-cron.yml';
  const ref = props.getProperty('GITHUB_REF') || 'main';

  if (!token) {
    throw new Error('Missing Script Property: GITHUB_TOKEN');
  }

  const commonHeaders = {
    Authorization: `Bearer ${token}`,
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
  };

  const runsUrl = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/runs?branch=${encodeURIComponent(ref)}&per_page=5`;
  const runsResp = UrlFetchApp.fetch(runsUrl, {
    method: 'get',
    headers: commonHeaders,
    muteHttpExceptions: true,
  });

  if (runsResp.getResponseCode() !== 200) {
    throw new Error(`GitHub runs check failed (${runsResp.getResponseCode()}): ${runsResp.getContentText()}`);
  }

  const runsPayload = JSON.parse(runsResp.getContentText() || '{}');
  const runs = runsPayload.workflow_runs || [];
  const hasActiveRun = runs.some((run) => run.status === 'queued' || run.status === 'in_progress');

  if (hasActiveRun) {
    console.log(`Skip dispatch: workflow already active at ${new Date().toISOString()}`);
    return;
  }

  const dispatchUrl = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`;
  const response = UrlFetchApp.fetch(dispatchUrl, {
    method: 'post',
    headers: commonHeaders,
    contentType: 'application/json',
    payload: JSON.stringify({ ref }),
    muteHttpExceptions: true,
  });
  const code = response.getResponseCode();
  const body = response.getContentText();

  if (code !== 204) {
    throw new Error(`GitHub dispatch failed (${code}): ${body}`);
  }

  console.log(`Workflow dispatched successfully at ${new Date().toISOString()}`);
}

function createFiveMinuteTrigger() {
  const handler = 'triggerCryptoSignalWorkflow';
  const triggers = ScriptApp.getProjectTriggers();
  for (const trigger of triggers) {
    if (trigger.getHandlerFunction() === handler) {
      ScriptApp.deleteTrigger(trigger);
    }
  }

  ScriptApp.newTrigger(handler)
    .timeBased()
    .everyMinutes(5)
    .create();
}

function deleteWorkflowTriggers() {
  const handler = 'triggerCryptoSignalWorkflow';
  const triggers = ScriptApp.getProjectTriggers();
  for (const trigger of triggers) {
    if (trigger.getHandlerFunction() === handler) {
      ScriptApp.deleteTrigger(trigger);
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// (2026-10-30) تشغيل أكثر من وركفلو معًا — لحل مشكلة أن cron في GitHub لا يلتزم
// بالخمس دقائق (قياس فعلي على مستودع ZigZag: 7 تشغيلات في 14 ساعة، وفجوة تصل 6.5
// ساعة، رغم أن الوركفلو يطلب */5). النتيجة كانت تجمّع الإشارات في دفعات كبيرة.
//
// الاستخدام:
//   1) في Script Properties أضف GITHUB_WORKFLOWS = "zigzag-signal-bot-cron.yml,signal-bot-cron.yml"
//      (بديلًا عن GITHUB_WORKFLOW المفرد — والمفرد يبقى مدعومًا).
//   2) شغّل createFiveMinuteMultiTrigger مرة واحدة ووافق على الصلاحيات.
//   3) لإيقافها: deleteMultiWorkflowTriggers.
// ─────────────────────────────────────────────────────────────────────────────
function dispatchWorkflowIfIdle(workflow) {
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty('GITHUB_TOKEN');
  const owner = props.getProperty('GITHUB_OWNER') || 'turky4500';
  const repo = props.getProperty('GITHUB_REPO') || 'binance-spot-usdt-signal-bot';
  const ref = props.getProperty('GITHUB_REF') || 'main';

  if (!token) {
    throw new Error('Missing Script Property: GITHUB_TOKEN');
  }

  const commonHeaders = {
    Authorization: `Bearer ${token}`,
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
  };

  const runsUrl = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/runs?branch=${encodeURIComponent(ref)}&per_page=5`;
  const runsResp = UrlFetchApp.fetch(runsUrl, { method: 'get', headers: commonHeaders, muteHttpExceptions: true });
  if (runsResp.getResponseCode() !== 200) {
    throw new Error(`GitHub runs check failed (${runsResp.getResponseCode()}): ${runsResp.getContentText()}`);
  }

  const runs = (JSON.parse(runsResp.getContentText() || '{}').workflow_runs) || [];
  const hasActiveRun = runs.some((run) => run.status === 'queued' || run.status === 'in_progress');
  if (hasActiveRun) {
    console.log(`[${workflow}] Skip dispatch: already active at ${new Date().toISOString()}`);
    return false;
  }

  const dispatchUrl = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`;
  const response = UrlFetchApp.fetch(dispatchUrl, {
    method: 'post',
    headers: commonHeaders,
    contentType: 'application/json',
    payload: JSON.stringify({ ref }),
    muteHttpExceptions: true,
  });
  if (response.getResponseCode() !== 204) {
    throw new Error(`[${workflow}] GitHub dispatch failed (${response.getResponseCode()}): ${response.getContentText()}`);
  }
  console.log(`[${workflow}] dispatched at ${new Date().toISOString()}`);
  return true;
}

function triggerConfiguredWorkflows() {
  const props = PropertiesService.getScriptProperties();
  const list = (props.getProperty('GITHUB_WORKFLOWS') || props.getProperty('GITHUB_WORKFLOW') || 'signal-bot-cron.yml')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
  for (const workflow of list) {
    try {
      dispatchWorkflowIfIdle(workflow);
    } catch (error) {
      // فشل وركفلو لا يمنع الباقي
      console.log(String(error));
    }
  }
}

function createFiveMinuteMultiTrigger() {
  const handler = 'triggerConfiguredWorkflows';
  for (const trigger of ScriptApp.getProjectTriggers()) {
    if (trigger.getHandlerFunction() === handler) {
      ScriptApp.deleteTrigger(trigger);
    }
  }
  ScriptApp.newTrigger(handler).timeBased().everyMinutes(5).create();
}

function deleteMultiWorkflowTriggers() {
  const handler = 'triggerConfiguredWorkflows';
  for (const trigger of ScriptApp.getProjectTriggers()) {
    if (trigger.getHandlerFunction() === handler) {
      ScriptApp.deleteTrigger(trigger);
    }
  }
}
