/* Retired web assistant UI must never replay old credential drafts. */
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
    if(url==='/api/announcements')return response({announcements:[]});
    if(url==='/api/auth/session')return response({authenticated:true,user:account});
    if(url==='/api/state')return response({features:{timetable:true,notes:true},version:'fixture'});
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
  run(fs.readFileSync(path.join(root,'web/draft-store.js'),'utf8'));
  await run("cloudDraftStore.save('account-a',{assistants:{revision:0,data:{members:{'old.json':{access_token:'fixture-private-token'}}}},preferences:{revision:1,data:{theme:'深色',concurrency:2}}})");
  run(fs.readFileSync(path.join(root,'web/app.js'),'utf8'));
  run(fs.readFileSync(path.join(root,'web/account-sync.js'),'utf8'));
  await until(()=>run('cloudReady&&!cloudBusy'));
  assert.equal($('#page-ai_assistants'),null);
  assert.equal($('#cloud-api-form'),null);
  assert.equal($('[data-page="ai_assistants"]'),null);
  assert.equal(run('cloudPending.assistants'),undefined);
  assert.equal(run('cloudResources.assistants'),undefined);
  assert.equal(writes.some(write=>write.url==='/api/sync/assistants'),false);
  assert.equal(resources.preferences.data.theme,'深色','Non-assistant drafts still upload');
  run('location.hash="#ai_assistants";route()');
  assert.equal(run('activePage'),'timetable');
  await assert.rejects(run("queueCloudData('assistants',{members:{}})"),/同步资源有误/);
  run('requireLogin()');
  assert.equal(run('Object.keys(cloudResources).length'),0);
  assert.equal(writes.some(write=>JSON.stringify(write.body).includes('fixture-private-token')),false);
  console.log('PASS removed assistant UI, old bookmarks, encrypted legacy drafts and logout never transfer assistant credentials');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>dom.window.close());
