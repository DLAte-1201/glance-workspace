const test = require('node:test');
const assert = require('node:assert/strict');
const projection = require('./app.js');

test('installation and unknown authentication do not imply connected', () => {
  assert.equal(projection.mode({detected:true,authenticated:'not_checked'}),'unconnected');
  assert.equal(projection.mode({connected:true}),'connected');
});

test('past receipts retain historical status even with successful transport', () => {
  assert.equal(projection.mode({run_kind:'historical_import',status:'succeeded',connected:true}),'historical');
  assert.equal(projection.mode({provenance:'historical_external_execution'}),'historical');
});

test('local child fixtures and saved live plans are distinct modes', () => {
  assert.equal(projection.mode({run_kind:'local_fixture',status:'succeeded'}),'synthetic');
  assert.equal(projection.mode({run_kind:'prepared_live',status:'live_pending'}),'live_pending');
  assert.equal(projection.mode({status:'pending_specific_approval'}),'live_pending');
});

test('usage remains per receipt and no aggregate total is exposed', () => {
  const first={id:'past-1',run_kind:'historical_import',receipt:{conversation_id:'same',usage:{total_tokens:100}}};
  const latest={id:'past-2',run_kind:'historical_import',receipt:{conversation_id:'same',usage:{total_tokens:150}}};
  assert.equal(projection.reportedUsage(latest),150);
  assert.notEqual(projection.reportedUsage(latest),250);
  assert.equal(projection.reportedUsage(first),100);
  assert.equal(projection.usageTotal,undefined);
});

test('synthetic tokens, invalid numbers, and missing usage are not reported as actual usage', () => {
  for(const record of [
    {run_kind:'local_fixture',receipt:{usage:{total_tokens:9000}}},
    {run_kind:'historical_import',receipt:{usage:{total_tokens:-1}}},
    {run_kind:'historical_import',receipt:{usage:{total_tokens:NaN}}},
    {run_kind:'historical_import',receipt:{usage:{total_tokens:'123'}}},
    {run_kind:'historical_import',receipt:{usage:{input_tokens:100}}}
  ])assert.equal(projection.reportedUsage(record),null);
});

test('only unique confirmed completions with existing evidence add construction', () => {
  const event = {id:'event-1',provenance:'synthetic_fixture',payload:{completion_key:'phase-tested',validation_status:'confirmed',evidence_refs:['test-1']}};
  assert.equal(projection.confirmedCompletions([event,event,{...event,payload:{...event.payload,completion_key:'missing',evidence_refs:['missing']}}],[{id:'test-1'}]).length,1);
});

test('historical receipts and local transport success cannot add construction', () => {
  const payload = {completion_key:'not-current-verification',validation_status:'confirmed',evidence_refs:['receipt-1']};
  assert.equal(projection.confirmedCompletions([
    {provenance:'historical_external_execution',payload},
    {run_kind:'local_fixture',status:'succeeded',payload}
  ],[{id:'receipt-1'}]).length,0);
});

test('tokens, elapsed time and running state create no construction', () => {
  assert.equal(projection.confirmedCompletions([{status:'running',payload:{total_tokens:999999,elapsed_seconds:2000}}],[]).length,0);
});
