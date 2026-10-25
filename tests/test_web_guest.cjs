/* Guest browsing, opt-in login and synced memo visibility with the real controllers. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {JSDOM}=require('../scripts/memo-editor/node_modules/jsdom');
const {indexedDB}=require('../.cache/web-test/node_modules/fake-indexeddb');
const {webcrypto}=require('node:crypto');
const root=path.join(__dirname,'..');
const dom=new JSDOM(fs.readFileSync(path.join(root,'web/index.html'),'utf8'),{url:'http://localhost/#notes',runScripts:'outside-only',pretendToBeVisual:true});
const {window}=dom,{document}=window,$=selector=>document.querySelector(selector),requests=[];
const user={id:'fixture-user',email:'fixture@example.test'};
let signedIn=false,failRead=false,failSave=false;
let memo={id:'b'.repeat(32),title:'已同步的内容',content:'客户端的正文',version:1,deleted:false,updated:1700000000};
for(const dialog of document.querySelectorAll('dialog')){dialog.showModal=()=>{dialog.open=true;};dialog.close=()=>{dialog.open=false;dialog.dispatchEvent(new window.Event('close'));};}
Object.assign(window,{console,indexedDB,TextEncoder,TextDecoder,matchMedia:()=>({matches:false}),confirm:()=>true,
  setTimeout:()=>1,clearTimeout:()=>{},setInterval:()=>1,
  fetch:async(url,options={})=>{
    const body=options.body?JSON.parse(options.body):null;requests.push({url,body});
    const response=(value,status=200)=>({ok:status===200,status,json:async()=>structuredClone(value)});
    if(url==='/api/auth/session')return response({authenticated:signedIn,user:signedIn?user:null,access_password_required:false});
    if(url==='/api/auth/login'){signedIn=true;return response({user});}
    if(url==='/api/auth/logout'){signedIn=false;return response({ok:true});}
    if(!signedIn)return response({error:'PRIVATE API REQUIRES LOGIN'},401);
    if(url==='/api/state')return response({features:{timetable:true,notes:true,ai_assistants:true},version:'fixture'});
    if(url==='/api/timetable')return response({schedule:null,first_monday:'',selected_week:0});
    if(url.startsWith('/api/sync?'))return response({resources:{preferences:{revision:0,data:{}},schedule:{revision:0,data:{}},assistants:{revision:0,data:{}}},conflicts:[]});
    if(url==='/api/memos'){
      if(body){if(failSave)return response({error:'Fixture: save failed'},503);memo={...memo,...body,version:memo.version+1};return response({memo});}
      if(failRead){failRead=false;return response({error:'Fixture: read failed'},503);}
      return response({memos:[memo]});
    }
    return response({ok:true});
  }});
Object.defineProperty(window,'crypto',{value:webcrypto});
const context=dom.getInternalVMContext(),run=code=>vm.runInContext(code,context);
const tick=async()=>{for(let i=0;i<10;i++)await new Promise(setImmediate);};
async function until(test){for(let i=0;i<250&&!test();i++)await tick();assert.ok(test());}
function submit(id){$(id).dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));}
(async()=>{
  for(const file of ['web/draft-store.js','web/app.js','web/memo-editor.js','web/memos.js','web/account-sync.js'])run(fs.readFileSync(path.join(root,file),'utf8'));
  await until(()=>run('connected'));
  assert.ok(!$('#login-dialog').open);
  assert.deepEqual(requests.map(r=>r.url),['/api/auth/session']);
  assert.match($('#memo-empty').textContent,/登录/);
  assert.equal($('#memo-list').querySelectorAll('.memo-list-empty').length,1);
  assert.equal($('#memo-title').disabled,true);
  await run('refreshState()');run('route()');await tick();assert.equal(requests.length,1);
  $('#account-open').click();assert.equal($('#login-dialog').open,true);
  const cancel=new window.Event('cancel',{cancelable:true});$('#login-dialog').dispatchEvent(cancel);assert.equal(cancel.defaultPrevented,false);
  $('[data-close="login-dialog"]').click();assert.equal($('#login-dialog').open,false);
  $('#memo-empty-new').click();assert.equal($('#login-dialog').open,true);$('[data-close="login-dialog"]').click();
  $('#chatgpt-reset-open').click();assert.equal($('#chatgpt-reset-dialog').open,true);
  assert.match($('#chatgpt-reset-dialog').textContent,/即将上线/);
  assert.equal(requests.length,1,'Placeholder must have no side effects');
  const link=$('#api-relay-link');assert.equal(link.href,'https://api.omini.cn/');assert.equal(link.target,'_blank');assert.match(link.rel,/noopener/);
  $('[data-close="chatgpt-reset-dialog"]').click();
  $('#settings-open').click();$('#feature-ai_assistants').checked=false;submit('#settings-form');await tick();
  assert.equal(requests.length,1,'Guest settings stay in this browser');
  assert.equal(run('features.ai_assistants'),false);
  console.log('PASS guest browsing avoids private requests; login is dismissible; navigation and local preferences work');

  failRead=true;$('#account-open').click();$('#account-email').value=user.email;$('#account-password').value='FixturePassword-123';submit('#login-form');
  await until(()=>run('auth.user&&!memoLoading&&cloudReady&&!cloudBusy'));
  assert.ok(!$('#login-dialog').open);assert.match($('#memo-notice').textContent,/read failed/);
  $('#memo-refresh').click();await until(()=>run('memoLoaded&&!memoLoading'));
  assert.equal($('#memo-title').value,memo.title);assert.equal(run('memoEditor.getContent()'),memo.content);
  assert.equal($('#memo-list').querySelectorAll('.memo-item').length,1);
  run('location.hash="#timetable";route()');await tick();
  memo={...memo,content:'另一台客户端更新的内容',version:2};
  run('location.hash="#notes";route()');await until(()=>run('memoCurrent?.version===2&&!memoLoading'));
  assert.equal(run('memoEditor.getContent()'),memo.content);
  run('memoEditor.loadContent("尚未保存的编辑");editedMemo()');failSave=true;
  const reads=requests.filter(r=>r.url==='/api/memos'&&!r.body).length;
  $('#memo-refresh').click();await tick();
  assert.equal(run('memoEditor.getContent()'),'尚未保存的编辑');assert.equal(run('memoDirty'),true);
  assert.equal(requests.filter(r=>r.url==='/api/memos'&&!r.body).length,reads);
  failSave=false;$('#memo-refresh').click();await until(()=>run('!memoDirty&&!memoLoading&&!memoBusy'));
  assert.equal(memo.content,'尚未保存的编辑');
  run('memoNotice("测试冲突副本",true)');$('#memo-save-copy').click();await until(()=>run('!memoBusy'));
  assert.equal($('#memo-refresh').disabled,false,'Refresh remains available after saving a conflict copy');
  console.log('PASS login loads synced content; refresh retries errors, fetches new remote data and preserves unsaved edits');
  $('#account-open').click();$('#account-logout').click();await until(()=>!run('auth.authenticated'));
  assert.ok(!$('#login-dialog').open);assert.equal(run('memoEditor.getContent()'),'');assert.equal($('#memo-list').querySelectorAll('.memo-item').length,0);
  assert.equal($('#memo-list').textContent,'登录后显示你的备忘录');
  assert.equal(run('features.ai_assistants'),false);
  console.log('PASS logout clears private views and returns to guest preferences without forcing login');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>dom.window.close());
