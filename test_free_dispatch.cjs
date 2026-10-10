const {test} = require('node:test');
const assert = require('node:assert/strict');
const {SIG_FREE, freeGitHubRequest, freeDispatchApi, freeDispatchTick} = require('./free_dispatch.js');
const at = Date.parse('2026-10-07T18:30:00Z');
test('diagnostics distinguish bounded HTTP codes and known validation failures', () => {
  const {freeFailureCategory} = require('./free_dispatch.js');
  assert.equal(freeFailureCategory({httpCode:403}), 'GITHUB_HTTP_403');
  assert.equal(freeFailureCategory(new Error('CHECKPOINT_DISABLED_OR_INVALID')), 'CHECKPOINT_DISABLED_OR_INVALID');
});
test('diagnostics never expose arbitrary exception content or unbounded status', () => {
  const {freeFailureCategory} = require('./free_dispatch.js');
  for (const error of [null, new Error('private-value'), {httpCode:'private-value'},
    {httpCode:999, message:'private-value'}, {httpCode:200.5}]) {
    assert.equal(freeFailureCategory(error), 'READ_OR_CONFIGURATION_UNAVAILABLE');
  }
});
function fixture(record = null) {
  const f = {record, reads: 0, posts: 0, found: false, busy: false, state: {}, error: null};
  f.api = {find: () => f.found, status: () => { f.reads++; return {state:f.state, busy:f.busy}; },
    dispatch: () => { f.posts++; if (f.error) throw f.error; }};
  f.journal = {read: () => f.record, write: r => { f.record = {...r}; }};
  f.tick = (mode='dry-run',time=at) => freeDispatchTick(f.api,f.journal,mode,time,()=> 'uuid');
  return f;
}
test('durable unknown record exists before single POST', () => {
  const f=fixture(); f.api.dispatch=()=>{assert.equal(f.record.status,'unknown');f.posts++;};
  assert.equal(f.tick(),'DISPATCH_ACCEPTED'); assert.equal(f.posts,1);
  assert.equal(f.record.mode,'dry-run'); assert.equal(f.record.status,'accepted');
});
test('ambiguous POST never replayed on a later invocation', () => {
  const f=fixture();f.error=new Error('fake transport secret');
  assert.equal(f.tick(),'UNKNOWN_DISPATCH_REQUIRES_REVIEW');
  assert.equal(f.tick('dry-run',at+7200000),'UNKNOWN_DISPATCH_REQUIRES_REVIEW');
  assert.equal(f.posts,1);
});
test('unknown HTTP server/redirect responses stop dispatch', () => {
  for(const httpCode of [301,429,500,503]) {
    const f=fixture();f.error=Object.assign(new Error(),{httpCode});
    assert.equal(f.tick(),'UNKNOWN_DISPATCH_REQUIRES_REVIEW');assert.equal(f.record.status,'unknown');
  }
});
test('unknown acceptance recovered using exact correlation ID, without POST', () => {
  const f=fixture({id:'known',at,mode:'dry-run',status:'unknown'});f.found=true;
  f.api.find=id=>{assert.equal(id,'known');return true;};
  assert.equal(f.tick(),'DISPATCH_OBSERVED_AFTER_UNKNOWN');assert.equal(f.posts,0);
});
test('definitive rejection persists cooldown across reconstructed scheduler', () => {
  const f=fixture();f.error=Object.assign(new Error(),{httpCode:403});
  assert.equal(f.tick(),'DISPATCH_REJECTED');
  const g=fixture(f.record);assert.equal(g.tick('dry-run',at+1000),'COOLDOWN');assert.equal(g.posts,0);
});
test('journal persistence failure blocks dispatch', () => {
  const f=fixture();f.journal.write=()=>{throw new Error('disk');};
  assert.throws(()=>f.tick());assert.equal(f.posts,0);
});
test('post-success persistence failure retains unknown and prevents replay', () => {
  const f=fixture();f.journal.write=r=>{if(r.status==='accepted')throw new Error();f.record={...r};};
  assert.throws(()=>f.tick());assert.equal(f.record.status,'unknown');
  assert.equal(f.tick(),'UNKNOWN_DISPATCH_REQUIRES_REVIEW');assert.equal(f.posts,1);
});
test('busy executor prevents POST', () => {const f=fixture();f.busy=true;assert.equal(f.tick(),'EXECUTOR_BUSY');assert.equal(f.posts,0);});
test('matching fresh heartbeat prevents POST, dry-run is not live evidence', () => {
  const f=fixture();f.state.last_successful_sweep={at:new Date(at-1000).toISOString(),mode:'dry-run',status:'success'};
  assert.equal(f.tick(),'SWEEP_FRESH');assert.equal(f.posts,0);
  assert.equal(f.tick('live'),'DISPATCH_ACCEPTED');
});
test('future heartbeat and malformed journal fail closed', () => {
  const f=fixture();f.state.last_successful_live_sweep={at:new Date(at+1).toISOString(),mode:'live',status:'success'};
  assert.throws(()=>f.tick('live'));assert.equal(f.posts,0);
  assert.throws(()=>fixture({status:'accepted'}).tick());
});
test('cutoff performs no network action', () => {
  const f=fixture();assert.equal(f.tick('live',SIG_FREE.cutoff),'CUTOFF');assert.equal(f.reads,0);assert.equal(f.posts,0);
});
test('fixed repository and single fetch, redirects prohibited', () => {
  let count=0;
  freeGitHubRequest('POST','/actions/workflows/sig-management.yml/dispatches',{},'FAKE_TEST_TOKEN',(url,opts)=>{
    count++;assert.equal(url,'https://api.github.com/repos/lekemog/sig-predictions-bot/actions/workflows/sig-management.yml/dispatches');
    assert.equal(opts.followRedirects,false);return {getResponseCode:()=>204,getContentText:()=>''};
  });assert.equal(count,1);
  assert.throws(()=>freeGitHubRequest('POST','/issues',{},'FAKE_TEST_TOKEN',()=>count++));assert.equal(count,1);
});
test('incomplete active-run listing and disabled checkpoint fail closed', () => {
  let state={version:1,enabled:true,jobs:[]};
  const api=freeDispatchApi((method,path)=>{
    if(path.includes('/contents/'))return {encoding:'base64',content:Buffer.from(JSON.stringify(state)).toString('base64')};
    if(path.includes('/actions/runs?'))return {total_count:101,workflow_runs:[]};return {state:'active'};
  },s=>Buffer.from(s,'base64').toString());
  assert.throws(()=>api.status());state.enabled=false;assert.throws(()=>api.status());
});
test('bounded latest run search requires exact title', () => {
  const api=freeDispatchApi(()=>({workflow_runs:[{display_title:'SIG sweep [exact]'}]}));
  assert.equal(api.find('exact'),true);assert.equal(api.find('ex'),false);
});

test('owner diagnostics perform only reads and redact credential and remote error text', () => {
  const vm = require('node:vm');
  const fs = require('node:fs');
  const logs = [], requests = [];
  const context = {
    console: {log: x => logs.push(x)},
    PropertiesService: {getScriptProperties: () => ({getProperty: k => k === 'GITHUB_DISPATCH_TOKEN' ? 'fixture-private' : null})},
    UrlFetchApp: {fetch: (url, options) => {
      requests.push(options.method);
      return {getResponseCode: () => 403, getContentText: () => 'fixture-private'};
    }}, Utilities: {}
  };
  vm.runInNewContext(fs.readFileSync('free_dispatch.js','utf8') + '\ndiagnoseFreeScheduler();', context);
  assert.deepEqual(requests,['get']);
  assert.equal(logs[0],'{"phase":"workflow","category":"GITHUB_HTTP_403"}');
  assert.equal(logs.some(x => x.includes('fixture-private')),false);
});
