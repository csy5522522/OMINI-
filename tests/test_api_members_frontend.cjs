/* Exercise API member controls against the real cloud workspace controller. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {JSDOM}=require('../scripts/memo-editor/node_modules/jsdom');
const {indexedDB}=require('../.cache/web-test/node_modules/fake-indexeddb');
const {webcrypto}=require('node:crypto');
const root=path.join(__dirname,'..');
const dom=new JSDOM(fs.readFileSync(path.join(root,'web/index.html'),'utf8'),{url:'http://localhost/#ai_assistants',runScripts:'outside-only'});
const {window}=dom,{document}=window,$=selector=>document.querySelector(selector);
for(const dialog of document.querySelectorAll('dialog')){
  dialog.showModal=()=>{dialog.open=true;};
  dialog.close=()=>{dialog.open=false;dialog.dispatchEvent(new window.Event('close'));};
}
const resources={preferences:{revision:1,data:{theme:'浅色'}},schedule:{revision:0,data:{}},assistants:{revision:1,data:{members:{'codex-fixture.json':{type:'codex',email:'fixture@example.test',access_token:'fixture-access',refresh_token:'fixture-refresh'}}}}};
const writes=[];
let account={id:'fixture-user',email:'fixture@example.test'};
Object.assign(window,{console,indexedDB,TextEncoder,TextDecoder,matchMedia:()=>({matches:false}),confirm:()=>true,
  setTimeout:()=>1,clearTimeout:()=>{},setInterval:()=>1,
  fetch:async(url,options={})=>{
    const response=data=>({ok:true,status:200,json:async()=>structuredClone(data)});
    if(url==='/api/auth/session')return response({authenticated:true,user:account});
    if(url==='/api/state')return response({features:{timetable:true,notes:false,ai_assistants:true},version:'fixture'});
    if(url.startsWith('/api/sync?'))return response({resources,conflicts:[]});
    if(url.startsWith('/api/sync/')&&options.method==='POST'){
      const name=url.split('/').at(-1),body=JSON.parse(options.body);
      writes.push({name,body});resources[name]={revision:body.revision+1,data:body.data};return response(resources[name]);
    }
    return response({ok:true});
  }});
Object.defineProperty(window,'crypto',{value:webcrypto});
const context=dom.getInternalVMContext(),run=code=>vm.runInContext(code,context);
const tick=async()=>{for(let i=0;i<8;i++)await new Promise(setImmediate);};
async function until(test){for(let i=0;i<250&&!test();i++)await tick();assert.ok(test());}
function submit(){ $('#cloud-api-form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})); }
async function synced(){await until(()=>!run('cloudBusy')&&run('Object.keys(cloudPending).length')===0);}
(async()=>{
  for(const file of ['web/draft-store.js','web/app.js','web/account-sync.js'])run(fs.readFileSync(path.join(root,file),'utf8'));
  await until(()=>run('cloudReady&&!cloudBusy'));
  $('#cloud-add-api').click();
  assert.ok($('#cloud-api-dialog').open);
  assert.equal($('#cloud-api-protocol').options.length,8);
  $('#cloud-api-label').value='API <fixture>';
  $('#cloud-api-url').value='https://fixture.example.test/v1';
  $('#cloud-api-key').value='fixture-private-key';
  $('#cloud-api-models').value='upstream-model => public-model';
  $('#cloud-api-headers').value='[]';
  submit();await tick();
  assert.ok($('#cloud-api-error').textContent);
  assert.equal(writes.length,0);
  $('#cloud-api-headers').value='{"X-Fixture":"fixture-private-header"}';
  $('#cloud-api-models').value='model a';submit();await tick();
  assert.equal(writes.length,0);
  $('#cloud-api-models').value='upstream-model => public-model\nother-model => public-model';submit();await tick();
  assert.equal(writes.length,0);
  $('#cloud-api-models').value='upstream-model => public-model';
  submit();await synced();await tick();
  const name=Object.keys(resources.assistants.data.pool.api_members)[0];
  assert.match(name,/^api-[a-f0-9]{32}$/);
  assert.equal(resources.assistants.data.pool.api_members[name].api_key,'fixture-private-key');
  assert.equal(resources.assistants.data.pool.api_members[name].models[0].alias,'public-model');
  assert.equal($('#cloud-api-key').value,'');
  assert.ok(!$('#cloud-api-dialog').open);
  const list=$('#cloud-members');
  assert.equal(list.querySelectorAll('[data-member]').length,2,'API and OAuth members share the list');
  assert.ok(!list.innerHTML.includes('fixture-private-key'));
  assert.ok(!list.innerHTML.includes('fixture-private-header'));
  assert.ok(list.textContent.includes('API <fixture>'));
  assert.equal(list.querySelector('fixture'),null,'Member labels are escaped');
  const row=()=>list.querySelector(`[data-member="${name}"]`);
  row().querySelector('[data-api-edit]').click();
  assert.equal($('#cloud-api-key').value,'fixture-private-key');
  $('#cloud-api-key').value='fixture-rotated-key';
  $('#cloud-api-protocol').value='responses';
  submit();await synced();await tick();
  assert.equal(Object.keys(resources.assistants.data.pool.api_members).length,1);
  assert.equal(resources.assistants.data.pool.api_members[name].protocol,'responses');
  assert.equal(resources.assistants.data.pool.api_members[name].api_key,'fixture-rotated-key');
  row().querySelector('[data-member-toggle]').click();await synced();
  assert.equal(resources.assistants.data.pool.api_members[name].disabled,true);
  const weight=row().querySelector('[data-rule="weight"]');weight.value='3';weight.dispatchEvent(new window.Event('change',{bubbles:true}));await synced();
  assert.equal(resources.assistants.data.pool.member_rules[name].weight,3);
  row().querySelector('[data-api-edit]').click();
  run('requireLogin()');
  assert.ok(!$('#cloud-api-dialog').open);
  assert.equal($('#cloud-api-key').value,'');
  account={id:'fixture-user',email:'fixture@example.test'};run(`setAuth({authenticated:true,user:${JSON.stringify(account)}})`);
  await run('loadAccountSync(true)');
  row().querySelector('[data-member-delete]').click();await synced();
  assert.equal(Object.keys(resources.assistants.data.pool.api_members).length,0);
  assert.ok(resources.assistants.data.members['codex-fixture.json']);
  assert.equal(resources.assistants.data.pool.member_rules[name],undefined);
  const rebased=run(`rebaseCloudData({pool:{api_members:{'api-one':{api_key:'old',base_url:'old'}}}},{pool:{api_members:{'api-one':{api_key:'local',base_url:'old'}}}},{pool:{api_members:{'api-one':{api_key:'old',base_url:'remote'}}}})`);
  assert.equal(rebased.pool.api_members['api-one'].api_key,'local');
  assert.equal(rebased.pool.api_members['api-one'].base_url,'old','A key and endpoint remain atomic');
  console.log('PASS: eight protocols, validation, API/OAuth mixed list, escaped labels, edit, toggle, weight, delete, account isolation and atomic rebase');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>dom.window.close());
