/* Real page DOM and memo UI behavior with controlled HTTP responses. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {parseHTML}=require('../.cache/web-test/node_modules/linkedom');
const root=path.join(__dirname,'..'),{window,document}=parseHTML(fs.readFileSync(path.join(root,'web/index.html'),'utf8'));
const $=s=>document.querySelector(s),stored=new Map();
for(const dialog of document.querySelectorAll('dialog')){dialog.showModal=()=>{dialog.open=true;};dialog.close=()=>{dialog.open=false;};}
for(const select of document.querySelectorAll('select'))Object.defineProperty(select,'value',{value:select.querySelector('option')?.getAttribute('value')||'',writable:true});
for(const field of document.querySelectorAll('input[type="checkbox"]'))Object.defineProperty(field,'checked',{value:true,writable:true});
const image='a'.repeat(64),id='b'.repeat(32);
let current={id,title:'云端记录',content:'第一稿',version:1,deleted:false,updated:1700000000};
let failSave=false;
const context=vm.createContext({document,window,console,location:{hash:'#notes'},Date,Blob,URL,matchMedia:()=>({matches:false}),confirm:()=>true,
 localStorage:{getItem:k=>stored.get(k),setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},
 setTimeout:()=>1,clearTimeout:()=>{},setInterval:()=>1,
 fetch:async(url,options={})=>{
  const body=options.body?JSON.parse(options.body):null;let result={ok:true},status=200;
  if(url==='/api/auth/session')result={authenticated:true,user:{id:'alice',email:'alice@example.test'}};
  else if(url==='/api/state')result={features:{downloads:true,timetable:true,notes:true},tasks:[],engines:{},concurrency:2,version:'1.2.0',download_dir:'/downloads'};
  else if(url==='/api/timetable')result={schedule:null,first_monday:''};
  else if(url===`/api/memos/assets/${image}`)result={id:image,mime:'image/png',data:'iVBORw0KGgo='};
  else if(url==='/api/memos'&&!body)result={memos:[current]};
  else if(url==='/api/memos'&&body){
   if(failSave){status=503;result={error:'暂时离线'};}
   else if(body.base_version!==current.version){status=409;result={error:'已有更新版本',conflict:true};}
   else{current={...current,...body,version:current.version+1};result={memo:current};}
  }
  return {ok:status===200,status,json:async()=>JSON.parse(JSON.stringify(result))};
 }
});
const run=code=>vm.runInContext(code,context),tick=async()=>{for(let i=0;i<8;i++)await new Promise(setImmediate);};
(async()=>{
 run(fs.readFileSync(path.join(root,'web/app.js'),'utf8'));run(fs.readFileSync(path.join(root,'web/memos.js'),'utf8'));await tick();
 await run('loadMemos(true)');run('showMemo(memos[0])');
 assert.equal($('#memo-title').value,'云端记录');
 $('#memo-content').value=`<img src=x onerror=alert(1)>\n![图片](omini-asset://${image})`;run('editedMemo()');await tick();
 assert.equal($('#memo-preview').querySelectorAll('img').length,1);
 assert.match($('#memo-preview').textContent,/<img src=x/);
 assert.match($('#memo-preview img').getAttribute('src'),/^data:image\/png;base64,/);
 assert.ok(stored.has('omini-memo-draft:alice'));
 assert.equal(await run('saveMemo()'),true);assert.equal(current.version,2);assert.equal(stored.has('omini-memo-draft:alice'),false);
 console.log('PASS memo text/image preview escapes HTML and writes versioned content');
 $('#memo-content').value='本机离线草稿';run('editedMemo()');failSave=true;
 assert.equal(await run('saveMemo()'),false);assert.match($('#memo-status').textContent,/暂存在本机/);assert.match(stored.get('omini-memo-draft:alice'),/本机离线草稿/);
 failSave=false;current={...current,version:3,content:'另一设备更新'};
 assert.equal(await run('saveMemo()'),false);assert.equal($('#memo-save-copy').hidden,false);assert.equal($('#memo-content').value,'本机离线草稿');
 run('requireLogin()');assert.equal($('#memo-content').value,'');assert.match(stored.get('omini-memo-draft:alice'),/本机离线草稿/);assert.equal(stored.has('omini-memo-draft:guest'),false);
 console.log('PASS network failure and concurrent edits retain account-scoped drafts on logout');
 run("setAuth({authenticated:true,user:{id:'alice',email:'alice@example.test'}})");await run('loadMemos(true)');
 assert.equal($('#memo-content').value,'本机离线草稿');assert.equal(run('memoCurrent.version'),2);assert.equal($('#memo-save-copy').hidden,false);
 assert.equal(await run('saveMemo()'),false);assert.equal(current.content,'另一设备更新');
 console.log('PASS reloaded stale draft preserves its base version and never overwrites newer cloud content');
})().catch(e=>{console.error(e);process.exitCode=1;});
