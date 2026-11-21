'use strict';
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');
const workflow=JSON.parse(fs.readFileSync(path.join(__dirname,'..','energygrid_local_failure_monitor.workflow.json'),'utf8'));
const find=(name)=>workflow.nodes.find(n=>n.name===name);
const run=(name,items,state,when)=>{
  const script=find(name).parameters.jsCode;
  class FakeDate extends Date {constructor(...args){super(...(args.length?args:[when]));}static now(){return when;}}
  const ctx={ $input:{ all:()=>items,first:()=>items[0] }, $getWorkflowStaticData:(key)=>{assert.equal(key,'global');return state;}, Date:FakeDate, Intl, Number, Math, Object, String, Error, RegExp, Array};
  return vm.runInNewContext('(function(){'+script+'\n})()',ctx);
};
const at=(iso)=>Date.parse(iso);
const payload=(observedAt,eb,tenant)=>({schema:'xb.energygrid.monitor.v1',run_id:'12345678-1234-1234-1234-123456789abc', observed_at_utc:observedAt,
  streams:{EB_BILL:eb,TENANT_BILL:tenant}});
const good=()=>({result:'OK',support_ref:null,incident_ref:null});
const bad=(ref,incident=null)=>({result:'FAILURE',support_ref:ref,incident_ref:incident});
const report=(state,iso,eb,ten)=>run('Validate Both Streams and Classify',[{json:{body:payload(iso,eb,ten)}}],state,at(iso));
// Safe and quiet when both streams succeed.
const s={};
const pend=(ref='EG_MONITOR_PENDING_AFTER_RUN',incident=null)=>({result:'PENDING',support_ref:ref,incident_ref:incident});
assert.equal(report(s,'2026-10-16T03:45:00Z',good(),good()).length,0);
// FAILURE is the Core's email to send: n8n records the incident but never alerts for it.
assert.equal(report(s,'2026-10-16T03:48:00Z',bad('EG_NATIVE_DRIVE_NAME_OCCUPIED'),good()).length,0);
assert.equal(s.open_incidents.EB_BILL.signature,'FAILURE|EG_NATIVE_DRIVE_NAME_OCCUPIED|');
assert.equal(report(s,'2026-10-16T03:49:00Z',good(),bad('EG_TENANT_AUTH_FAILED')).length,0);
// A bill still PENDING after a finished run is what the Core cannot report: one alert per incident.
let o=report(s,'2026-10-16T03:50:00Z',pend(),good());
assert.equal(o.length,1);assert.equal(o[0].json.dedupe_key,'eg-monitor:EB_BILL:2');
assert.deepEqual(Object.keys(o[0].json),['dedupe_key','severity','key','short','lines']);
assert.equal(o[0].json.severity,'WARN');assert.equal(o[0].json.key,'bill_pending:EB_BILL');assert.equal(o[0].json.short,'bill_pending:EB_BILL');
assert.deepEqual(o[0].json.lines,['EnergyGrid needs attention: EB Bill is still PENDING after a finished run.',
  'Reference: EG_MONITOR_PENDING_AFTER_RUN','Run: 12345678-1234-1234-1234-123456789abc','Observed (UTC): 2026-10-16T03:50:00Z',
  "Review EnergyGrid's local status. Do not retry uncertain uploads or emails."]);
// Unresolved repeat keeps the same key, so Remove Duplicates drops it.
assert.equal(report(s,'2026-10-16T03:55:00Z',pend(),good())[0].json.dedupe_key,o[0].json.dedupe_key);
assert.equal(report(s,'2026-10-16T04:00:00Z',good(),good()).length,0);
// Recovery then a new incident: a new key.
assert.equal(report(s,'2026-10-16T04:05:00Z',pend(),good())[0].json.dedupe_key,'eg-monitor:EB_BILL:3');
assert.equal(report(s,'2026-10-16T04:10:00Z',pend('EG_MONITOR_PENDING_AFTER_RUN','bill-abcdef123456abcdef123456'),good())[0].json.dedupe_key,'eg-monitor:EB_BILL:4');
// Tenant alone, then both streams: independently identifiable incidents.
o=report(s,'2026-10-16T04:15:00Z',good(),pend());
assert.equal(o.length,1);assert.equal(o[0].json.dedupe_key,'eg-monitor:TENANT_BILL:2');
o=report(s,'2026-10-16T04:16:00Z',pend(),pend('EG_MONITOR_PENDING_AFTER_RUN','bill-0123456789abcdef01234567'));
assert.deepEqual(o.map(x=>x.json.dedupe_key),['eg-monitor:EB_BILL:5','eg-monitor:TENANT_BILL:3']);
const malicious=payload('2026-10-16T04:20:00Z',bad('EG_NATIVE_DRIVE_NAME_OCCUPIED'),good());malicious.toEmail='attacker@example.com';
assert.throws(()=>run('Validate Both Streams and Classify',[{json:{body:malicious}}],s,at('2026-10-16T04:20:00Z')),/EG_MONITOR_REPORT_SHAPE/);
const stale=payload('2026-10-15T04:20:00Z',good(),good());
assert.throws(()=>run('Validate Both Streams and Classify',[{json:{body:stale}}],s,at('2026-10-16T04:20:00Z')),/EG_MONITOR_OLD_REPORT/);
const inj=payload('2026-10-16T04:20:00Z',bad('EG_FAIL\nBCC:OTHER'),good());
assert.throws(()=>run('Validate Both Streams and Classify',[{json:{body:inj}}],s,at('2026-10-16T04:20:00Z')),/EG_MONITOR_REF/);
// Watchdog: no missing-run on PC/n8n booted after usual task time.
const w={};let event=run('Check Missing Run (PC-On Only)',[{json:{}}],w,at('2026-10-16T06:10:00Z'));
assert.equal(event.length,0);
// Monitor continuously online before the usual scheduled check (11:10 SGT == 03:10 UTC).
const armed={};for(let m=0;m<=180;m+=5){const tm=at('2026-10-16T03:10:00Z')+m*60*1000;event=run('Check Missing Run (PC-On Only)',[{json:{}}],armed,tm);if(m<170) assert.equal(event.length,0);}
assert.equal(event.length,1);assert.equal(event[0].json.dedupe_key,'eg-monitor:missed:2026-10-16');
assert.deepEqual(Object.keys(event[0].json),['dedupe_key','severity','key','short','lines']);
assert.equal(event[0].json.severity,'ERROR');assert.equal(event[0].json.key,'run_report_missing');assert.equal(event[0].json.short,'run_report_missing');
assert.deepEqual(event[0].json.lines,['EnergyGrid did not submit a completed EB + Tenant run report by 14:00 SGT on 2026-10-16.',
  'The daily native task (11:30) did not run, stopped early, withheld its report, or the report was refused.',
  'n8n was running before the usual check, so this is not a PC-off case.',
  'Next: check Task Scheduler and the run logs (runbook: energygrid-bill-downloader/docs/native_daily_claude_free.md, section 5).',
  'If the PC was shut down briefly, this may be a false alarm. Do not replay bill effects.']);
assert.ok(!/Claude/.test(find('Check Missing Run (PC-On Only)').parameters.jsCode),'no stale Claude-task wording');
// User instruction: no missed-run alert when the PC/n8n was off for 20 min across the run.
const down={};run('Check Missing Run (PC-On Only)',[{json:{}}],down,at('2026-10-16T03:15:00Z'));
run('Check Missing Run (PC-On Only)',[{json:{}}],down,at('2026-10-16T03:40:00Z'));
for(let m=0;m<=140;m+=5){event=run('Check Missing Run (PC-On Only)',[{json:{}}],down,at('2026-10-16T03:45:00Z')+m*60*1000);assert.equal(event.length,0);}
// A legitimate report suppresses missing-run alerts.
const done={};for(let m=0;m<=170;m+=5){const time=at('2026-10-16T03:10:00Z')+m*60*1000;
  if(m===60)report(done,new Date(time).toISOString().replace('.000Z','Z'),good(),good());
  event=run('Check Missing Run (PC-On Only)',[{json:{}}],done,time);
  assert.equal(event.length,0);
}
// Explicit send gate OFF even when connected (no real emails possible from this artifact).
assert.equal(run('Alerts Disabled Until Qualified',[{json:{message:'fail'}}],{},at('2026-10-16T04:00:00Z')).length,0);
assert.equal(workflow.active,false);
// #288 T1: alerts go by email through the shared XB Ops Alert Email sender, never Telegram.
assert.ok(!workflow.nodes.some(n=>n.type==='n8n-nodes-base.telegram'),'no Telegram node');
assert.doesNotMatch(JSON.stringify(workflow),/telegram|chat_?id/i);
const sender=find('Send Owner Alert Email');
assert.equal(sender.type,'n8n-nodes-base.executeWorkflow');assert.equal(sender.typeVersion,1.2);
assert.equal(sender.retryOnFail,false);assert.equal(sender.onError,'continueRegularOutput');
for(const key of ['continueOnFail','maxTries','waitBetweenTries','alwaysOutputData']) assert.ok(!(key in sender),key);
assert.deepEqual(sender.parameters.workflowId,{__rl:true,value:'REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID',mode:'id'});
assert.equal(sender.parameters.source,'database');
// One sender execution per alert: the sender refuses more than one item, and a report can raise two.
assert.equal(sender.parameters.mode,'each');assert.equal(sender.parameters.options.waitForSubWorkflow,true);
const inputs=sender.parameters.workflowInputs;
assert.deepEqual(inputs.value,{system:'ENERGYGRID',severity:'={{ $json.severity }}',key:'={{ $json.key }}',short:'={{ $json.short }}',lines:'={{ $json.lines }}'});
assert.equal(inputs.convertFieldsToString,false,'lines must stay an array');
const opsPath=path.join(__dirname,'..','ops_alert_email.workflow.json');
const ops=JSON.parse(fs.readFileSync(opsPath,'utf8'));
const opsTrigger=ops.nodes.find(n=>n.type==='n8n-nodes-base.executeWorkflowTrigger');
assert.deepEqual(inputs.schema.map(f=>[f.id,f.type]),opsTrigger.parameters.workflowInputs.values.map(v=>[v.name,v.type]));
assert.ok(!workflow.nodes.some(n=>n.credentials),'no credentials in the source export');
// #333 LATER L1: Remove Duplicates keys on the per-incident dedupe_key, never on `key`, which is constant
// per alert kind (a slip to $json.key would silently drop every later alert of that kind). Workflow scope
// keeps the seen keys across executions; the history size bounds that store.
const dedupe=find('One Attempt Per Incident');
assert.equal(dedupe.type,'n8n-nodes-base.removeDuplicates');assert.equal(dedupe.typeVersion,2);
assert.deepEqual(dedupe.parameters,{operation:'removeItemsSeenInPreviousExecutions',dedupeValue:'={{ $json.dedupe_key }}',
  options:{scope:'workflow',historySize:10000}});
// #333 LATER L2: the source export carries no runtime state (alert series, open incidents, report day).
assert.strictEqual(workflow.staticData,null);
assert.equal(workflow.connections['One Attempt Per Incident'].main[0][0].node,'Send Owner Alert Email');
assert.equal(Object.keys(workflow.connections).length,6);
assert.ok(!workflow.connections['Send Owner Alert Email'],'the email call is the last node');
// Cross-contract: every alert the Code nodes can emit passes the sender's own Validate Alert Request code,
// mapped exactly as the call node maps it, and names no other system.
const validateCode=ops.nodes.find(n=>n.name==='Validate Alert Request').parameters.jsCode;
const senderAccepts=(alert)=>{
  const call={system:'ENERGYGRID',severity:alert.severity,key:alert.key,short:alert.short,lines:alert.lines};
  const out=vm.runInNewContext('(function(){'+validateCode+'\n})()',{$input:{all:()=>[{json:call}],first:()=>({json:call})},Array,Object,String,RegExp,Number,JSON});
  assert.equal(out.length,1);return out[0].json;
};
const senderSystems=vm.runInNewContext(validateCode.match(/const SYSTEMS = (\[[^\]]*\]);/)[1]);
assert.ok(senderSystems.includes('ENERGYGRID'),'the sender accepts system ENERGYGRID');
const otherSystems=senderSystems.filter(s=>s!=='ENERGYGRID');
// Superset, not an exact list: other workflows may add their own system to the sender (#341 adds
// KIT-RETAG) without an EnergyGrid change. The known ones must stay, and every alert below is still
// checked against all of the sender's other systems, a new one included.
for(const known of ['AC2-ARAP','AC2-STOCK','HEALTH','MEMBER-SYNC']) assert.ok(otherSystems.includes(known),known);
assert.equal(new Set(otherSystems).size,otherSystems.length,'no duplicate system');
// Which reference can reach an alert: only a PENDING row alerts, and the reporter writes exactly one
// fixed reference on a PENDING row. FAILURE references come from the Core and are recorded, never alerted.
const reporterSource=fs.readFileSync(path.join(__dirname,'energygrid_monitor_report.py'),'utf8');
assert.deepEqual([...reporterSource.matchAll(/"PENDING",\s*([^\n]+)/g)].map(m=>m[1].trim()),['"EG_MONITOR_PENDING_AFTER_RUN"']);
// Every EG_ reference the reporter or the Core can put in a report.
const repoRoot=path.join(__dirname,'..','..');
const sources=[path.join(__dirname,'energygrid_monitor_report.py')];
const corePkg=path.join(repoRoot,'energygrid-bill-downloader','energygrid_bill_downloader');
for(const name of fs.readdirSync(corePkg)) if(name.endsWith('.py')) sources.push(path.join(corePkg,name));
const scanned=new Set(['EG_MONITOR_PENDING_AFTER_RUN']);
for(const file of sources) for(const m of fs.readFileSync(file,'utf8').matchAll(/\bEG_[A-Z0-9_]+\b/g)) if(/^EG_[A-Z0-9_]{4,72}$/.test(m[0])) scanned.add(m[0]);
assert.ok(scanned.size>50,'the reference scan found the Core references');
// A reference naming another system would be refused by the sender (OPS_ALERT_OTHER_SYSTEM): no email.
// The one known Core reference of that kind is a run-level FAILURE reason ("UNHEALTHY" contains HEALTH),
// which never reaches an alert. A new one fails here so it is reviewed before it ships.
const namesOther=(text)=>otherSystems.some(other=>text.includes(other));
assert.deepEqual([...scanned].filter(namesOther),['EG_DAILY_N8N_UNHEALTHY']);
assert.ok(!namesOther('EG_MONITOR_PENDING_AFTER_RUN'));
assert.equal(report({},'2026-10-16T04:29:00Z',bad('EG_DAILY_N8N_UNHEALTHY'),bad('EG_DAILY_N8N_UNHEALTHY')).length,0);
// Defence in depth: every other reference, even used as a PENDING reference, still makes a valid email.
const refs=new Set([...scanned].filter(ref=>!namesOther(ref)));
const emitted=[];
let refIndex=0;
for(const ref of refs){
  const st={};const iso=new Date(at('2026-10-16T04:30:00Z')+(refIndex++)*1000).toISOString().replace('.000Z','Z');
  emitted.push(...run('Validate Both Streams and Classify',[{json:{body:payload(iso,pend(ref,'bill-0123456789abcdef01234567'),pend(ref))}}],st,at(iso)).map(x=>x.json));
}
let missed=[];const tick={};for(let m=0;m<=180;m+=5){missed=run('Check Missing Run (PC-On Only)',[{json:{}}],tick,at('2026-12-31T03:10:00Z')+m*60*1000);}
emitted.push(missed[0].json);
assert.equal(emitted.length,refs.size*2+1);
for(const alert of emitted){
  const verdict=senderAccepts(alert);
  assert.equal(verdict.valid,true,JSON.stringify({alert,verdict}));
  assert.equal(verdict.system,'ENERGYGRID');
  for(const other of otherSystems) assert.ok(![alert.key,alert.short,...alert.lines].join('\n').includes(other));
}
// The sender's refusal is real: the same alert naming another system is refused.
assert.equal(senderAccepts({...emitted[0],lines:[...emitted[0].lines,'see HEALTH']}).reason,'OPS_ALERT_OTHER_SYSTEM');
assert.equal(workflow.settings.availableInMCP,false);
console.log('PASS: n8n Code nodes, PENDING-only alerts, incident lifecycle, missed-run PC-off policy, injection, disabled alert gate, and the XB Ops Alert Email contract');
