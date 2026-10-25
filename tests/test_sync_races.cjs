/* Delayed responses and file reads against the real browser controllers. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {JSDOM}=require('../scripts/memo-editor/node_modules/jsdom');
const {indexedDB}=require('../.cache/web-test/node_modules/fake-indexeddb');
const {webcrypto}=require('node:crypto');
const root=path.join(__dirname,'..');
const dom=new JSDOM(fs.readFileSync(path.join(root,'web/index.html'),'utf8'),{url:'http://localhost/#timetable',runScripts:'outside-only'});
const {window}=dom,{document}=window,$=selector=>document.querySelector(selector);
for(const dialog of document.querySelectorAll('dialog')){dialog.showModal=()=>{dialog.open=true;};dialog.close=()=>{dialog.open=false;};}
let account={id:'account-a',email:'a@example.test'},blocked='',release=null;
const writes=[];
const privateSchedule={schema:1,year:'2026',term:'3',updated_at:'fixture',period_times:{},courses:[{name:'ACCOUNT A PRIVATE',teacher:'fixture',location:'fixture',weekday:1,periods:[1],weeks:[1],week_text:'1',campus:'',credits:'',class_name:''}]};
const resources={preferences:{revision:1,data:{theme:'浅色',concurrency:2}},schedule:{revision:0,data:{}},assistants:{revision:0,data:{}}};
Object.assign(window,{console,indexedDB,TextEncoder,TextDecoder,matchMedia:()=>({matches:false}),confirm:()=>true,
  setTimeout:()=>1,clearTimeout:()=>{},setInterval:()=>1,
  fetch:async(url,options={})=>{
    const body=options.body?JSON.parse(options.body):null;
    if(body)writes.push({url,body});
    const response=value=>({ok:true,status:200,json:async()=>structuredClone(value)});
    if(url===blocked){blocked='';await new Promise(resolve=>{release=resolve;});}
    if(url==='/api/auth/session')return response({authenticated:true,user:account});
    if(url==='/api/state')return response({features:{timetable:true,notes:true,ai_assistants:true},version:'fixture'});
    if(url==='/api/timetable')return response({schedule:null,first_monday:'',selected_week:0});
    if(url.startsWith('/api/timetable/'))return response({schedule:privateSchedule,first_monday:'2026-10-19',revision:2});
    if(url.startsWith('/api/sync?'))return response({resources,conflicts:[]});
    if(url==='/api/sync/preferences'){
      // Stale same-field edits are retained as server-side conflicts.
      if(body.revision===resources.preferences.revision)resources.preferences={revision:body.revision+1,data:body.data};
      return response(resources.preferences);
    }
    return response({ok:true});
  }});
Object.defineProperty(window,'crypto',{value:webcrypto});
const context=dom.getInternalVMContext(),run=code=>vm.runInContext(code,context);
const tick=async()=>{for(let i=0;i<8;i++)await new Promise(setImmediate);};
async function until(test){for(let i=0;i<250&&!test();i++)await tick();assert.ok(test());}
function switchAccount(){run('requireLogin()');account={id:'account-b',email:'b@example.test'};run(`setAuth({authenticated:true,user:${JSON.stringify(account)}})`);}
function submit(id){$(id).dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));}
async function beginA(){
  run('requireLogin()');account={id:'account-a',email:'a@example.test'};run(`setAuth({authenticated:true,user:${JSON.stringify(account)}})`);
  await run('loadAccountSync(true)');
}
(async()=>{
  for(const file of ['web/app.js','web/account-sync.js'])run(fs.readFileSync(path.join(root,file),'utf8'));
  await until(()=>run('cloudReady&&!cloudBusy'));
  for(const [endpoint,start] of [
    ['/api/timetable/semester',()=>submit('#semester-form')],
    ['/api/timetable/refresh',()=>submit('#online-form')],
    ['/api/timetable/import',()=>{
      Object.defineProperty($('#import-file'),'files',{configurable:true,value:[{size:10,text:async()=>'fixture'}]});
      $('#import-file').dispatchEvent(new window.Event('change'));
    }]
  ]){
    await beginA();blocked=endpoint;release=null;start();await until(()=>release);
    switchAccount();$('#school-cookie').value='NEW ACCOUNT COOKIE';$('#import-status').textContent='NEW ACCOUNT STATUS';
    $('#online-submit').disabled=true; // New-account operation owns these controls now.
    release();await tick();
    assert.equal(run('savedTimetable'),null,endpoint);
    assert.equal($('#school-cookie').value,'NEW ACCOUNT COOKIE');
    assert.equal($('#import-status').textContent,'NEW ACCOUNT STATUS');
    assert.equal($('#online-submit').disabled,true);
  }
  await beginA();let finishRead;
  Object.defineProperty($('#import-file'),'files',{configurable:true,value:[{size:10,text:()=>new Promise(resolve=>{finishRead=resolve;})}]});
  $('#import-file').dispatchEvent(new window.Event('change'));
  await until(()=>finishRead);const count=writes.length;switchAccount();finishRead('PRIVATE FILE');await tick();
  assert.equal(writes.length,count,'Old-account file must not be submitted using a new session');
  assert.equal(run('savedTimetable'),null);

  await beginA();blocked='/api/sync/preferences';release=null;
  const first=run("queueCloudData('preferences',{theme:'深色',concurrency:2})");
  await until(()=>release);
  await run("queueCloudData('preferences',{theme:'浅色',concurrency:2})");
  release();await first;
  assert.equal(run('cloudPending.preferences.revision'),2);
  await run('loadAccountSync(true)');
  assert.equal(resources.preferences.data.theme,'浅色');
  assert.equal(run('Object.keys(cloudPending).length'),0);
  // Nested remote changes survive; local removals and atomic OAuth edits survive too.
  const rebase=(b,l,r)=>JSON.parse(run(`JSON.stringify(rebaseCloudData(${JSON.stringify(b)},${JSON.stringify(l)},${JSON.stringify(r)}))`));
  assert.deepEqual(rebase({theme:'深色',features:{notes:true}},{theme:'浅色',features:{notes:true}},{theme:'深色',features:{notes:false}}),{theme:'浅色',features:{notes:false}});
  assert.deepEqual(rebase({pool:{member_rules:{a:{priority:0,weight:1}}}},{pool:{member_rules:{a:{priority:5,weight:1}}}},{pool:{member_rules:{a:{priority:0,weight:4}}}}),{pool:{member_rules:{a:{priority:5,weight:4}}}});
  assert.deepEqual(rebase({members:{a:{access_token:'old',refresh_token:'old'}}},{members:{}},{members:{a:{access_token:'remote',refresh_token:'remote'},b:{access_token:'b',refresh_token:'b'}}}),{members:{b:{access_token:'b',refresh_token:'b'}}});
  assert.deepEqual(rebase({selected_week:1,schedule:{year:'old'}},{selected_week:2,schedule:{year:'old'}},{selected_week:1,schedule:{year:'remote'}}),{selected_week:2,schedule:{year:'remote'}});
  console.log('PASS: stale timetable responses, delayed import reads, latest queued edit, independent fields and removals');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>dom.window.close());
