/* Real ProseMirror state and memo controller with isolated, deterministic HTTP. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {JSDOM}=require('../scripts/memo-editor/node_modules/jsdom');
const root=path.join(__dirname,'..');
const dom=new JSDOM(fs.readFileSync(path.join(root,'web/index.html'),'utf8'),{url:'http://localhost/#notes',runScripts:'outside-only',pretendToBeVisual:true});
const {window}=dom,{document}=window,$=s=>document.querySelector(s),stored=new Map();
for(const dialog of document.querySelectorAll('dialog')){dialog.showModal=()=>{dialog.open=true;};dialog.close=()=>{dialog.open=false;};}
const rect=()=>({left:0,right:0,top:0,bottom:0,width:0,height:0});
window.Range.prototype.getClientRects=()=>[];window.Range.prototype.getBoundingClientRect=rect;
window.HTMLElement.prototype.getClientRects=()=>[];window.HTMLElement.prototype.getBoundingClientRect=rect;
const image='a'.repeat(64),id='b'.repeat(32),token=`![图片](omini-asset://${image})`;
let current={id,title:'云端记录',content:'第一稿',version:1,deleted:false,updated:1700000000};
let failSave=false;
Object.assign(window,{
  console,matchMedia:()=>({matches:false}),confirm:()=>true,
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
Object.defineProperty(window,'localStorage',{value:{getItem:k=>stored.get(k),setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)}});
const context=dom.getInternalVMContext(),run=code=>vm.runInContext(code,context),tick=async()=>{for(let i=0;i<8;i++)await new Promise(setImmediate);};
function replaceContent(text){run(`memoEditor.loadContent(${JSON.stringify(text)});editedMemo()`);}
(async()=>{
  for(const file of ['web/app.js','web/memo-editor.js','web/memos.js'])run(fs.readFileSync(path.join(root,file),'utf8'));
  await tick();await run('loadMemos(true)');run('showMemo(memos[0])');
  assert.equal($('#memo-title').value,'云端记录');assert.equal($('#memo-preview'),null);
  const content=`\n<img src=x onerror=alert(1)>\n\n😀\u00a0文字${token}\n\n`;
  replaceContent(content);await tick();
  assert.equal($('#memo-content').querySelectorAll('.memo-inline-image img').length,1);
  assert.match($('#memo-content').textContent,/<img src=x/);
  assert.equal(run('memoEditor.getContent()'),content);
  assert.match($('#memo-content .memo-inline-image img').getAttribute('src'),/^data:image\/png;base64,/);
  assert.ok(stored.has('omini-memo-draft:alice'));
  assert.equal(await run('saveMemo()'),true);assert.equal(current.version,2);assert.equal(stored.has('omini-memo-draft:alice'),false);
  console.log('PASS unified editor preserves text, blank lines, emoji and image tokens without interpreting HTML');

  replaceContent('本机离线草稿');failSave=true;
  assert.equal(await run('saveMemo()'),false);assert.equal($('#memo-notice').hidden,false);assert.match($('#memo-notice').textContent,/暂存在本机/);
  assert.match(stored.get('omini-memo-draft:alice'),/本机离线草稿/);
  failSave=false;current={...current,version:3,content:'另一设备更新'};
  assert.equal(await run('saveMemo()'),false);assert.equal($('#memo-save-copy').hidden,false);assert.equal(run('memoEditor.getContent()'),'本机离线草稿');
  run('requireLogin()');assert.equal(run('memoEditor.getContent()'),'');assert.match(stored.get('omini-memo-draft:alice'),/本机离线草稿/);assert.equal(stored.has('omini-memo-draft:guest'),false);
  run("setAuth({authenticated:true,user:{id:'alice',email:'alice@example.test'}})");await run('loadMemos(true)');
  assert.equal(run('memoEditor.getContent()'),'本机离线草稿');assert.equal(run('memoCurrent.version'),2);assert.equal($('#memo-save-copy').hidden,false);
  assert.equal(await run('saveMemo()'),false);assert.equal(current.content,'另一设备更新');
  console.log('PASS offline drafts, logout isolation and stale base versions retain edits instead of overwriting cloud content');

  run('memoComposing=true');assert.equal(await run('saveMemo()'),false);run('memoComposing=false');
  run('showMemo({...memos[0],deleted:true})');assert.equal($('#memo-content .ProseMirror').getAttribute('contenteditable'),'false');assert.equal($('#memo-restore').hidden,false);assert.equal($('#memo-image-button').disabled,true);
  run('showMemo({...memos[0],deleted:false})');$('#memo-more').click();assert.equal($('#memo-menu').hidden,false);
  document.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));assert.equal($('#memo-menu').hidden,true);
  assert.doesNotMatch($('#memo-list').textContent,/v3/);
  console.log('PASS composition guards, trash read-only state and accessible bottom menu');
  dom.window.close();
})().catch(error=>{console.error(error);dom.window.close();process.exitCode=1;});
