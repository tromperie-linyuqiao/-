// node --test tests/ui.test.cjs (Node 20+; no additional dependencies)
// The production inline scripts execute against a deliberately small fake DOM.
// These regressions cover state/races; browser layout/auth is a separate check.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');

function scripts(page) {
  const html = fs.readFileSync(path.join(root, 'pages', page, 'index.html'), 'utf8');
  return [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)].map(m=>m[1]).filter(s=>s.trim());
}
function pageRuntime(page) {
  const nodes = new Map();
  const timers = [];
  const notices = [];
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {textContent:'', innerHTML:'', value:'', style:{},
      options:[], disabled:false, dataset:{}, classList:{toggle(){}},
      setAttribute(){}, removeAttribute(){}, querySelectorAll(){return [];}});
    return nodes.get(id);
  }
  const context = vm.createContext({URL, setTimeout:fn=>{timers.push(fn);return timers.length;},
    clearTimeout(){}, setInterval(){}, console,
    window:{AstrBotPluginPage:{ready:async()=>({}), apiGet:async()=>({}), apiPost:async()=>({})}},
    document:{hidden:false, readyState:'loading',getElementById:node,
      querySelectorAll:()=>[], addEventListener(){},removeEventListener(){}},
    FileReader:class { readAsText(file){this.result=file.content;this.onload();} }
  });
  // Only boot requests are suppressed. All production declarations and event
  // registration execute unchanged, and tests call the real handlers below.
  const code = scripts(page).at(-1)
    .replace(/^load\(\);.*$/gm, '')
    .replace(/^loadAll\(\);.*$/gm, '')
    .replace(/^refreshAll\(\);.*$/gm, '')
    .replace(/^switchTab\(CUR_TAB\);.*$/gm, '');
  vm.runInContext(code, context, {filename:`${page}/index.html`});
  context.notices = notices;
  vm.runInContext('toast = (message) => notices.push(message);', context);
  return {context,node,timers,notices,run:code=>vm.runInContext(code,context)};
}
const rule={id:'sample', enabled:true, signal:'presence',conditions:[{field:'presence.room_changed',op:'changed',value:null}],
  priority:3, cooldown_minutes:30, template_hint:'sample'};
const profile={id:'sample',parts:[{id:'shoulder',label:'肩部',sens:5,half_life:1200}],action_words:{},
  params:{gain:.75},tiers:['notice','react','lose','overload'].map((key,i)=>({key,min:[.1,.3,.5,.9][i]}))};

test('all five production pages have valid inline JavaScript',()=>{
  for(const page of ['home','status','manor','materials','mood'])
    for(const script of scripts(page)) new vm.Script(script,{filename:page});
});
test('automatic rules refresh preserves an open unsaved editor',async()=>{
  const p=pageRuntime('mood'); p.context.seed=rule;
  p.run('RULES=[seed];RULES_SNAPSHOT=JSON.stringify(RULES);EDIT_IDX=0;EDIT_COPY={...seed,template_hint:"draft"};');
  let reads=0;p.context.apiGet=async()=>{reads++;return {rules:[rule]};};
  await p.context.loadRules(false);
  assert.equal(p.run('EDIT_COPY.template_hint'),'draft');assert.equal(reads,0);
});
test('an in-flight rules refresh cannot erase an editor opened after request',async()=>{
  const p=pageRuntime('mood');p.context.seed=rule;
  p.run('RULES=[seed];RULES_SNAPSHOT=JSON.stringify(RULES);');
  let resolve;p.context.apiGet=()=>new Promise(r=>{resolve=r;});
  const pending=p.context.loadRules(false);
  p.run('EDIT_IDX=0;EDIT_COPY={...seed,template_hint:"new draft"};');
  resolve({rules:[rule]});await pending;
  assert.equal(p.run('EDIT_COPY.template_hint'),'new draft');
});
test('saving rules requires completing the visible editor first',()=>{
  const p=pageRuntime('mood');p.run('EDIT_IDX=0;');
  let posts=0;p.context.apiPost=async()=>{posts++;};p.context.saveRules();
  assert.equal(posts,0);assert.match(p.notices[0],/完成或取消/);
});
test('profile import submits explicit params and all four tiers',async()=>{
  const p=pageRuntime('status');p.node('edit-char').options=[{value:'sample'}];
  p.run('renderEditor=()=>{};');
  p.context.importProfile({files:[{content:JSON.stringify(profile),size:100}],value:'selected'});
  const payload=JSON.parse(JSON.stringify(await p.context.buildSavePayload()));
  assert.deepEqual(payload.params,profile.params);assert.deepEqual(payload.tiers,profile.tiers);
});
test('partial legacy import does not overwrite omitted server params or tiers',async()=>{
  const p=pageRuntime('status');p.node('edit-char').options=[{value:'sample'}];p.run('renderEditor=()=>{};');
  const legacy={id:'sample',parts:profile.parts};
  p.context.importProfile({files:[{content:JSON.stringify(legacy),size:100}],value:'selected'});
  const payload=await p.context.buildSavePayload();
  assert.equal(Object.hasOwn(payload,'params'),false);assert.equal(Object.hasOwn(payload,'tiers'),false);
});
test('invalid import leaves the existing editor untouched',()=>{
  const p=pageRuntime('status');p.node('edit-char').options=[{value:'sample'}];p.context.seed=profile;
  p.run('EDIT=seed;renderEditor=()=>{};');
  for(const change of [{id:'unknown'},{params:[]},{tiers:[]},{parts:[null]}]){
    p.context.importProfile({files:[{content:JSON.stringify({...profile,...change}),size:100}],value:'selected'});
    assert.equal(p.run('EDIT'),p.context.seed);
  }
});
test('slow profile save locks other save buttons until success and refreshes normalized data',async()=>{
  const p=pageRuntime('status');p.context.seed=profile;p.run('EDIT=seed;renderEditor=()=>{};');
  let resolve,posts=0,forced=false;p.context.apiPost=()=>{posts++;return new Promise(r=>{resolve=r;});};
  p.context.loadEditor=async force=>{forced=force;};p.context.load=()=>{};
  const first={disabled:false},second={disabled:false};
  const pending=p.context.saveProfile(first);await Promise.resolve();await Promise.resolve();
  for(const fn of p.timers) fn(); // advancing UI timers must not release the request lock
  await p.context.saveProfile(second);assert.equal(posts,1);assert.equal(first.disabled,true);
  resolve({ok:true});await pending;
  assert.equal(forced,true);assert.equal(first.disabled,false);assert.equal(p.run('EDIT_SAVE_PENDING'),false);
});
test('failed profile save preserves edits and releases lock for retry',async()=>{
  const p=pageRuntime('status');p.context.seed=profile;p.run('EDIT=seed;');
  p.context.apiPost=async()=>{throw new Error('synthetic network failure');};const btn={disabled:false};
  await p.context.saveProfile(btn);
  assert.equal(p.run('EDIT'),p.context.seed);assert.equal(btn.disabled,false);
  assert.equal(p.run('EDIT_SAVE_PENDING'),false);assert.match(p.node('save-state').textContent,/编辑内容已保留/);
});
test('successful save refresh can replace a same-role draft with normalized server ids',async()=>{
  const p=pageRuntime('status');p.context.seed={...profile,parts:[{id:'new_part_1'}]};
  p.node('edit-char').value='sample';p.node('edit-char').options=[{value:'sample'}];
  p.run('EDIT=seed;renderEditor=()=>{};renderSensLegend=()=>{};closeRatioPopup=()=>{};');
  p.context.apiGet=async()=>({profiles:{sample:profile},names:{},sens_scale:[]});
  await p.context.loadEditor(true);
  assert.equal(p.run('EDIT.parts[0].id'),'shoulder');
});
test('material references reject active protocols and credentials',()=>{
  const p=pageRuntime('materials');
  for(const url of ['javascript:alert(1)','data:text/html,test','file:///tmp/test','https://user:pass@example.org','invalid'])
    assert.equal(p.context.safeReferenceUrl(url),'');
  assert.equal(p.context.safeReferenceUrl('https://example.org/paper'),'https://example.org/paper');
});
test('repeated mood refresh shares the active request window',async()=>{
  const p=pageRuntime('mood');let resolve,calls=0;
  p.context.loadSignals=()=>{calls++;return new Promise(r=>{resolve=r;});};
  p.context.loadTriggers=async()=>{};p.context.loadHistory=async()=>{};p.context.loadRules=async()=>{};
  const first=p.context.refreshAll();await p.context.refreshAll();assert.equal(calls,1);
  resolve();await first;assert.equal(p.run('REFRESH_PENDING'),false);
});
