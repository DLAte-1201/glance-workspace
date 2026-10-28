const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const projection=require('./app.js');
const manifest=JSON.parse(fs.readFileSync(path.join(__dirname,'characters.json'),'utf8'));

test('the three requested roles map to their verified original illustrations',()=>{
  assert.deepEqual(Object.keys(projection.characters),['codex','claude','gemini']);
  for(const character of manifest.characters){
    assert.equal(projection.characters[character.id].asset,character.asset);
    assert.equal(projection.characters[character.id].name,character.display_name);
    const deployed=fs.readFileSync(path.join(__dirname,character.asset.slice(1)));
    const original=fs.readFileSync(character.source_path);
    assert.ok(deployed.equals(original),'the deployed image must remain byte-identical');
    assert.equal(crypto.createHash('sha256').update(deployed).digest('hex'),character.sha256);
  }
});

test('only a running record for the corresponding provider activates its character',()=>{
  const running=[{provider:'antigravity',role:'reviewer',status:'running',run_kind:'local_fixture',provenance:'synthetic_fixture'}];
  assert.equal(projection.characterActive('gemini',running),true);
  assert.equal(projection.characterActive('claude',running),false);
  assert.equal(projection.characterActive('codex',running),false);
  for(const status of ['queued','succeeded','failed','cancelled','awaiting_approval'])assert.equal(projection.characterActive('gemini',[{...running[0],status}]),false);
});

test('a provider match takes priority over an inconsistent generic role',()=>{
  const runs=[{provider:'claude',role:'primary_author',status:'running'}];
  assert.equal(projection.characterActive('claude',runs),true);
  assert.equal(projection.characterActive('codex',runs),false);
});

test('history, prepared plans and scripted fixture claims do not imply active execution',()=>{
  for(const run of [
    {provider:'codex',status:'running',run_kind:'prepared_live'},
    {provider:'codex',status:'running',run_kind:'historical_import'},
    {provider:'codex',status:'running',provenance:'historical_external_execution'},
    {provider:'codex',status:'running',provenance:'synthetic_fixture'}
  ])assert.equal(projection.characterActive('codex',[run]),false);
});

test('repeated animation projection leaves events, runs and completion keys unchanged',()=>{
  const runs=[{id:'run-1',provider:'claude',status:'running'}],events=[{id:'event-1',payload:{completion_key:'done',validation_status:'confirmed',evidence_refs:['test']}}],evidence=[{id:'test'}];
  const before=JSON.stringify({runs,events,evidence});
  for(let i=0;i<100;i++){projection.characterActive('claude',runs);projection.confirmedCompletions(events,evidence);}
  assert.equal(JSON.stringify({runs,events,evidence}),before);
  assert.equal(projection.confirmedCompletions(events,evidence).length,1);
});

test('reduced motion disables all character animation layers',()=>{
  const css=fs.readFileSync(path.join(__dirname,'style.css'),'utf8');
  assert.match(css,/@media\(prefers-reduced-motion:reduce\)\{\.character-motion,\.character-leg,\.character-arm\{animation:none!important;transform:none!important\}\}/);
});


test('visible actor names are exactly ChatGPT, Claude and Gemini while aliases remain metadata',()=>{
  assert.deepEqual(Object.values(projection.characters).map(c=>c.actor_name),['ChatGPT','Claude','Gemini']);
  for(const character of manifest.characters)assert.equal(projection.characters[character.id].name,character.display_name);
  const source=fs.readFileSync(path.join(__dirname,'app.js'),'utf8');
  assert.match(source,/characterFor\(a\)\.actor_name/);
  assert.doesNotMatch(source,/esc\(characterFor\(a\)\.name\)/);
});
