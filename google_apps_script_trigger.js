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

  const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`;
  const options = {
    method: 'post',
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    contentType: 'application/json',
    payload: JSON.stringify({ ref }),
    muteHttpExceptions: true,
  };

  const response = UrlFetchApp.fetch(url, options);
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
