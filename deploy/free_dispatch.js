/* Google Apps Script V8: free GitHub-only sweep trigger. No SIG credentials.
 * One private script project only. Keep its properties and dispatch journal.
 * Paste this file into Code.gs; credentials belong in Script Properties.
 */
const SIG_FREE = Object.freeze({
  repo: 'lekemog/sig-predictions-bot', workflow: 'sig-management.yml',
  cutoff: Date.parse('2026-11-04T17:00:00Z'), interval: 600000,
  active: ['queued', 'in_progress', 'waiting', 'pending', 'requested']
});

function freeGitHubRequest(method, path, payload, token, fetcher) {
  token = typeof token === 'string' ? token.trim() : '';
  if (!token || !['GET', 'POST'].includes(method) || !path.startsWith('/') ||
      path.includes('..') || (method === 'POST' &&
      path !== '/actions/workflows/' + SIG_FREE.workflow + '/dispatches')) {
    throw new Error('INVALID_GITHUB_REQUEST');
  }
  const options = {method: method.toLowerCase(), followRedirects: false,
    muteHttpExceptions: true, headers: {
      Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    }};
  if (payload !== undefined) {
    options.contentType = 'application/json'; options.payload = JSON.stringify(payload);
  }
  // Exactly one fetch, including POST; never print exception text or responses.
  const response = fetcher('https://api.github.com/repos/' + SIG_FREE.repo + path, options);
  const code = response.getResponseCode();
  if (code < 200 || code >= 300) {
    const error = new Error('GITHUB_HTTP_FAILURE'); error.httpCode = code; throw error;
  }
  const body = response.getContentText();
  return body ? JSON.parse(body) : {};
}

function freeDispatchApi(request, decode) {
  return {
    find: function(id) {
      const data = request('GET', '/actions/workflows/' + SIG_FREE.workflow +
        '/runs?event=workflow_dispatch&per_page=100');
      if (!Array.isArray(data.workflow_runs)) throw new Error('INVALID_RUN_LIST');
      return data.workflow_runs.some(r => r.display_title === 'SIG sweep [' + id + ']');
    },
    status: function() {
      const workflow = request('GET', '/actions/workflows/' + SIG_FREE.workflow);
      if (workflow.state !== 'active') throw new Error('WORKFLOW_DISABLED');
      const file = request('GET', '/contents/.bot/state.json?ref=main');
      if (file.encoding !== 'base64' || typeof file.content !== 'string') {
        throw new Error('INVALID_CHECKPOINT_ENCODING');
      }
      const state = JSON.parse(decode(file.content));
      if (state.version !== 1 || state.enabled !== true || !Array.isArray(state.jobs)) {
        throw new Error('CHECKPOINT_DISABLED_OR_INVALID');
      }
      for (const status of SIG_FREE.active) {
        const data = request('GET', '/actions/runs?status=' + status + '&per_page=100');
        if (!Array.isArray(data.workflow_runs) || !Number.isInteger(data.total_count) ||
            data.total_count < data.workflow_runs.length ||
            data.total_count > data.workflow_runs.length) {
          throw new Error('ACTIVE_RUN_LIST_INCOMPLETE');
        }
        if (data.workflow_runs.length) return {state: state, busy: true};
      }
      return {state: state, busy: false};
    },
    dispatch: function(mode, id) {
      return request('POST', '/actions/workflows/' + SIG_FREE.workflow + '/dispatches',
        {ref: 'main', inputs: {mode: mode, trigger_id: id}});
    }
  };
}

function freeDispatchTick(api, journal, mode, at, newId) {
  if (!['dry-run', 'live'].includes(mode) || !Number.isFinite(at)) {
    throw new Error('INVALID_CONFIGURATION');
  }
  if (at >= SIG_FREE.cutoff) return 'CUTOFF';
  const last = journal.read();
  if (last) {
    if (typeof last.id !== 'string' || !Number.isFinite(last.at) ||
        !['dry-run', 'live'].includes(last.mode) ||
        !['unknown', 'accepted', 'rejected'].includes(last.status)) {
      throw new Error('INVALID_DISPATCH_JOURNAL');
    }
    if (last.status === 'unknown') {
      if (api.find(last.id)) {
        journal.write(Object.assign({}, last, {status: 'accepted'}));
        return 'DISPATCH_OBSERVED_AFTER_UNKNOWN';
      }
      return 'UNKNOWN_DISPATCH_REQUIRES_REVIEW';
    }
    if (at < last.at) throw new Error('CLOCK_MOVED_BACKWARDS');
    if (at - last.at < SIG_FREE.interval) return 'COOLDOWN';
  }
  const result = api.status();
  if (result.busy) return 'EXECUTOR_BUSY';
  const marker = result.state[mode === 'live' ? 'last_successful_live_sweep' : 'last_successful_sweep'];
  if (marker && marker.mode === mode && marker.status === 'success') {
    const heartbeat = Date.parse(marker.at);
    if (!Number.isFinite(heartbeat) || heartbeat > at) throw new Error('INVALID_HEARTBEAT');
    if (at - heartbeat < SIG_FREE.interval) return 'SWEEP_FRESH';
  }
  const attempt = {id: 'sig-free-' + newId() + '-' + at, at: at, mode: mode, status: 'unknown'};
  // A property-write failure prevents POST. A crash after POST leaves unknown.
  journal.write(attempt);
  try { api.dispatch(mode, attempt.id); }
  catch (error) {
    if ([400, 401, 403, 404, 422].includes(error.httpCode)) {
      journal.write(Object.assign({}, attempt, {status: 'rejected'}));
      return 'DISPATCH_REJECTED';
    }
    return 'UNKNOWN_DISPATCH_REQUIRES_REVIEW';
  }
  journal.write(Object.assign({}, attempt, {status: 'accepted'}));
  return 'DISPATCH_ACCEPTED'; // Acceptance is not successful screening.
}

// Return only bounded, known categories; never log remote bodies or exception text.
function freeFailureCategory(error) {
  if (error && Number.isInteger(error.httpCode) && error.httpCode >= 100 && error.httpCode <= 599) {
    return 'GITHUB_HTTP_' + error.httpCode;
  }
  const allowed = ['INVALID_GITHUB_REQUEST', 'INVALID_RUN_LIST', 'WORKFLOW_DISABLED',
    'INVALID_CHECKPOINT_ENCODING', 'CHECKPOINT_DISABLED_OR_INVALID',
    'ACTIVE_RUN_LIST_INCOMPLETE', 'INVALID_CONFIGURATION', 'INVALID_DISPATCH_JOURNAL',
    'CLOCK_MOVED_BACKWARDS', 'INVALID_HEARTBEAT'];
  return error && allowed.includes(error.message) ? error.message : 'READ_OR_CONFIGURATION_UNAVAILABLE';
}

// Owner-invoked diagnostics: no dispatch, property writes or credential output.
function diagnoseFreeScheduler() {
  let phase = 'properties';
  try {
    const properties = PropertiesService.getScriptProperties();
    const token = properties.getProperty('GITHUB_DISPATCH_TOKEN');
    const api = freeDispatchApi((method, path, payload) => {
      if (method !== 'GET' || payload !== undefined) throw new Error('INVALID_GITHUB_REQUEST');
      phase = path.startsWith('/contents/') ? 'checkpoint' :
        path.startsWith('/actions/runs?') ? 'active_runs' : 'workflow';
      return freeGitHubRequest(method, path, payload, token,
        (url, options) => UrlFetchApp.fetch(url, options));
    }, text => Utilities.newBlob(Utilities.base64Decode(text)).getDataAsString('UTF-8'));
    const result = api.status();
    console.log(JSON.stringify({category: 'READS_VERIFIED', busy: result.busy,
      mode: properties.getProperty('DISPATCH_MODE') || 'dry-run',
      enabled: properties.getProperty('RUN_ENABLED') === 'true'}));
  } catch (error) {
    console.log(JSON.stringify({phase: phase, category: freeFailureCategory(error)}));
  }
}

function sigFreeTick() {
  const at = Date.now();
  if (at >= SIG_FREE.cutoff) { console.log('CUTOFF'); return; }
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) { console.log('SCHEDULER_BUSY'); return; }
  try {
    const properties = PropertiesService.getScriptProperties();
    if (properties.getProperty('RUN_ENABLED') !== 'true') { console.log('DISABLED'); return; }
    const mode = properties.getProperty('DISPATCH_MODE') || 'dry-run';
    const token = properties.getProperty('GITHUB_DISPATCH_TOKEN');
    const api = freeDispatchApi(
      (method, path, payload) => freeGitHubRequest(method, path, payload, token,
        (url, options) => UrlFetchApp.fetch(url, options)),
      text => Utilities.newBlob(Utilities.base64Decode(text)).getDataAsString('UTF-8'));
    const journal = {
      read: () => { const data = properties.getProperty('LAST_DISPATCH');
        return data === null ? null : JSON.parse(data); },
      write: record => properties.setProperty('LAST_DISPATCH', JSON.stringify(record))
    };
    console.log(freeDispatchTick(api, journal, mode, at, () => Utilities.getUuid()));
  } catch (error) { console.log(freeFailureCategory(error)); }
  finally { lock.releaseLock(); }
}

// Owner runs this once after adding the token. It cannot activate live mode.
// Installs at most one timer per account/project and never erases a journal.
function installDryRunTrigger() {
  if (Date.now() >= SIG_FREE.cutoff) throw new Error('CUTOFF');
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) throw new Error('SCHEDULER_BUSY');
  try {
    const properties = PropertiesService.getScriptProperties();
    if (!properties.getProperty('GITHUB_DISPATCH_TOKEN')) throw new Error('CREDENTIAL_MISSING');
    if (properties.getProperty('DISPATCH_MODE') === 'live') throw new Error('ALREADY_LIVE');
    const existing = ScriptApp.getProjectTriggers().filter(t => t.getHandlerFunction() === 'sigFreeTick');
    if (existing.length > 1) throw new Error('DUPLICATE_TIMERS_REQUIRE_REVIEW');
    properties.setProperty('DISPATCH_MODE', 'dry-run');
    properties.setProperty('RUN_ENABLED', 'true');
    if (!existing.length) ScriptApp.newTrigger('sigFreeTick').timeBased().everyMinutes(10).create();
  } finally { lock.releaseLock(); }
}

// Owner must run this button manually. It activates the existing timer only;
// no trading runner, timer or GitHub dispatch is created here.
function activateVerifiedLiveScheduler() {
  if (Date.now() >= SIG_FREE.cutoff) throw new Error('CUTOFF');
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) throw new Error('SCHEDULER_BUSY');
  try {
    const properties = PropertiesService.getScriptProperties();
    if (properties.getProperty('RUN_ENABLED') !== 'true') throw new Error('DISABLED');
    const timers = ScriptApp.getProjectTriggers().filter(t => t.getHandlerFunction() === 'sigFreeTick');
    if (timers.length !== 1) throw new Error('EXACTLY_ONE_EXISTING_TIMER_REQUIRED');
    const last = properties.getProperty('LAST_DISPATCH');
    if (last !== null && !['accepted', 'rejected'].includes(JSON.parse(last).status)) {
      throw new Error('UNKNOWN_DISPATCH_REQUIRES_REVIEW');
    }
    const api = freeDispatchApi((method, path, payload) => {
      if (method !== 'GET' || payload !== undefined) throw new Error('INVALID_GITHUB_REQUEST');
      return freeGitHubRequest(method, path, payload, properties.getProperty('GITHUB_DISPATCH_TOKEN'),
        (url, options) => UrlFetchApp.fetch(url, options));
    }, text => Utilities.newBlob(Utilities.base64Decode(text)).getDataAsString('UTF-8'));
    const result = api.status();
    if (result.busy) throw new Error('EXECUTOR_BUSY');
    if (result.state.jobs.some(j => !['done', 'dry_run'].includes(j.status))) {
      throw new Error('PENDING_EXECUTION_REQUIRES_RECONCILIATION');
    }
    properties.setProperty('DISPATCH_MODE', 'live');
    console.log('LIVE_TIMER_ENABLED_GITHUB_REMAINS_SOLE_EXECUTOR');
  } finally { lock.releaseLock(); }
}

if (typeof module !== 'undefined') module.exports = {SIG_FREE, freeGitHubRequest,
  freeDispatchApi, freeDispatchTick, freeFailureCategory};