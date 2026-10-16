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
assert.match(o[0].json.subject,/^EnergyGrid: EB Bill incomplete \(EG_MONITOR_PENDING_AFTER_RUN\)$/);
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
assert.match(event[0].json.message,/did not run, stopped early, or its report was refused/);
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
const sender=find('Send Owner Telegram Alert');
assert.equal(sender.type,'n8n-nodes-base.telegram');assert.equal(sender.retryOnFail,false);assert.equal(sender.continueOnFail,true);
assert.equal(sender.parameters.chatId,'REPLACE_WITH_OWNER_TELEGRAM_CHAT_ID');assert.equal(sender.parameters.additionalFields.appendAttribution,false);
assert.ok(!workflow.nodes.some(n=>n.credentials),'no credentials in the source export');
assert.equal(workflow.connections['One Attempt Per Incident'].main[0][0].node,'Send Owner Telegram Alert');
assert.equal(workflow.settings.availableInMCP,false);
console.log('PASS: n8n Code nodes, PENDING-only alerts, incident lifecycle, missed-run PC-off policy, injection, and disabled alert gate');
