/* DOM behavior tests without controlling a browser. Test dependency only:
 * npm install --prefix .cache/web-test --no-audit --no-fund linkedom@0.18.13
 * node tests/test_web_frontend.cjs
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {parseHTML} = require('../.cache/web-test/node_modules/linkedom');
const root = path.join(__dirname, '..');
const {window, document} = parseHTML(fs.readFileSync(path.join(root, 'web/index.html'), 'utf8'));
const $ = s => document.querySelector(s);
const requests = [];
const intervals = [];
const timers = [];
let exported = false;
let failImport = false;
let failLogin = false;
const authState = {authenticated:true,user:{id:'fixture-user',email:'member@example.test'},access_password_required:false};
const fixtureTask = {id:'fixture-task',type:'file',source:'https://example.test/file.bin',title:'Sample file',status:'queued',progress:0,speed:'—',eta:'—',error:''};
const state = {version:'1.2.0-web',tasks:[],download_dir:'C:/Omini/downloads',engines:{'yt-dlp':true,aria2c:true,ffmpeg:true,'gallery-dl':false,spotdl:true},concurrency:2,features:{downloads:true,timetable:true}};
const stored = new Map();
const scheduleData = {schedule:null,first_monday:''};
for (const dialog of document.querySelectorAll('dialog')) {
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; dialog.dispatchEvent(new window.Event('close')); };
}
// linkedom doesn't implement the standard select.value setter or file inputs.
for (const select of document.querySelectorAll('select')) {
  Object.defineProperty(select, 'value', {value:select.querySelector('option')?.getAttribute('value') || select.querySelector('option')?.textContent || '', writable:true});
}
for (const checkbox of document.querySelectorAll('input[type="checkbox"]')) {
  Object.defineProperty(checkbox,'checked',{value:checkbox.hasAttribute('checked'),writable:true});
}
document.hidden = false;
window.open = () => {};
window.URL = {createObjectURL:()=>{exported=true;return 'blob:test';},revokeObjectURL:()=>{}};
const context = vm.createContext({
  document,window,console,location:{hash:'#downloads'},Date,Blob,URL:window.URL,
  matchMedia:()=>({matches:false}),localStorage:{getItem:k=>stored.get(k),setItem:(k,v)=>stored.set(k,v)},
  setTimeout:(fn,ms)=>{const t=setTimeout(fn,ms);timers.push(t);return t;},clearTimeout,
  setInterval:fn=>{intervals.push(fn);return intervals.length;},
  fetch: async (url, options={}) => {
    const data=options.body ? JSON.parse(options.body) : null;
    requests.push({url,data});
    let status=200, result={ok:true};
    if(url==='/api/auth/session')result=authState;
    else if(url==='/api/auth/logout'){authState.authenticated=false;authState.user=null;}
    else if(['/api/auth/login','/api/auth/register','/api/login'].includes(url)){
      if(failLogin){status=401;result={error:'邮箱或密码不正确。'};}
      else{authState.authenticated=true;authState.user={id:'fixture-user',email:(data.email||'member@example.test').trim().toLowerCase()};result={user:authState.user};}
    }
    else if(url==='/api/state')result=state;
    else if(url==='/api/timetable')result=scheduleData;
    else if(url==='/api/tasks'){state.tasks.unshift({...fixtureTask});result={task:fixtureTask};}
    else if(url==='/api/settings'){state.concurrency=data.concurrency;state.features={...state.features,...data.features};result={ok:true,features:state.features};}
    else if(url==='/api/timetable/import'){
      if(failImport){status=400;result={error:'测试导入失败'};}
      else{scheduleData.schedule=JSON.parse(data.content);result=scheduleData;}
    }
    else if(url==='/api/timetable/semester'){scheduleData.first_monday=data.first_monday;result=scheduleData;}
    else if(url.endsWith('/log'))result={log:'<img src=x onerror=alert(1)>\nDownload complete'};
    else if(url.endsWith('/files'))result={complete:true,files:[{name:'fixture.bin',bytes:512,url:'/api/files/fixture-task/fixture.bin'}]};
    return {ok:status===200,status,json:async()=>JSON.parse(JSON.stringify(result))};
  }
});
const run = code => vm.runInContext(code,context);
const tick = async () => { for(let i=0;i<8;i++)await new Promise(setImmediate); };
const click = selector => { const target=$(selector);assert.ok(target,`Missing control ${selector}`);target.dispatchEvent(new window.Event('click',{bubbles:true})); };
const submit = selector => $(selector).dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));
const sampleSchedule = {schema:1,year:'2026',term:'3',updated_at:'2026-10-20T12:00:00',period_times:{'1':{start:'08:00',end:'08:45'}},courses:[
  {name:'数据结构 <img src=x>',teacher:'张老师',location:'A201',weekday:1,periods:[1,2,5],weeks:[1,3],week_text:'1-4周(单)',campus:'广州',credits:'3',class_name:'示例'},
  {name:'重叠课程',teacher:'李老师',location:'A202',weekday:1,periods:[2,3],weeks:[1,3],week_text:'1-4周(单)'},
  {name:'双周课程',teacher:'陈老师',location:'B201',weekday:2,periods:[3,4],weeks:[2,4],week_text:'2-4周(双)'}
]};

(async()=>{
  try {
    run(fs.readFileSync(path.join(root,'web/app.js'),'utf8'));
    await tick();
    assert.match($('#connection').textContent,/服务已连接/);
    assert.match($('#task-list').textContent,/下一份精彩/);
    assert.equal($('#download-dir').textContent,state.download_dir);
    assert.equal(document.querySelectorAll('#engine-list .engine-row').length,5);
    assert.equal($('#page-timetable').hidden,true);
    assert.equal($('#submit-download').disabled,false);
    assert.equal($('#account-label').textContent,'member@example.test');
    assert.equal(requests.some(r=>r.url==='/api/timetable'),false);
    console.log('PASS initial render, service status, engines and empty queue');

    click('#mobile-theme');assert.equal(document.documentElement.dataset.theme,'dark');
    click('#theme-toggle');assert.equal(document.documentElement.dataset.theme,'light');
    click('#mobile-settings');assert.equal($('#settings-dialog').open,true);
    $('#concurrency').value='3';submit('#settings-form');await tick();
    assert.equal(state.concurrency,3);assert.equal($('#settings-dialog').open,false);
    console.log('PASS mobile theme/settings and preference save');

    click('[data-type="file"]');assert.equal($('#filename-field').hidden,false);
    assert.equal($('#quality-field').hidden,true);
    $('#source').value=fixtureTask.source;$('#filename').value='sample.bin';submit('#download-form');await tick();
    assert.equal(requests.find(r=>r.url==='/api/tasks').data.type,'file');
    assert.equal($('#stat-total').textContent,'1');assert.equal($('#source').value,'');
    state.tasks[0].title='<img src=x onerror=alert(1)>';
    state.tasks[0].status='running';state.tasks[0].progress=42;
    await run('refreshTasks()');
    assert.equal(document.querySelectorAll('#task-list img').length,0);
    assert.match($('#task-list').textContent,/<img src=x/);
    assert.equal($('.progress-fill').style.width,'42%');
    click('[data-filter="completed"]');assert.equal(document.querySelectorAll('.task-row').length,0);
    click('[data-filter="all"]');assert.equal(document.querySelectorAll('.task-row').length,1);
    click('[data-action="log"]');await tick();assert.equal(document.querySelectorAll('#detail-content img').length,0);
    assert.match($('#detail-content').textContent,/<img src=x/);$('#detail-dialog').close();
    click('[data-action="files"]');await tick();assert.equal($('.file-link').getAttribute('href'),'/api/files/fixture-task/fixture.bin');$('#detail-dialog').close();
    console.log('PASS task submission, progress, filters, logs, file links and HTML escaping');

    context.fixture=sampleSchedule;
    run('acceptTimetable({schedule:fixture,first_monday:""});location.hash="#timetable";route()');
    assert.equal($('#page-downloads').hidden,true);assert.equal($('#page-timetable').hidden,false);
    assert.equal(document.querySelectorAll('.course-card').length,3); // discontinuous course becomes two cards
    assert.equal(document.querySelectorAll('#schedule-grid img').length,0);
    assert.ok($$('.course-card').some(el=>el.style.width.includes('50%')));
    click('[data-course="0"]');assert.match($('#detail-title').textContent,/数据结构/);assert.match($('#detail-content').textContent,/1、2、5/);$('#detail-dialog').close();
    $('#week-select').value='2';$('#week-select').dispatchEvent(new window.Event('change'));
    assert.equal(document.querySelectorAll('.course-card').length,1);assert.match($('#schedule-grid').textContent,/双周课程/);
    $('#week-select').value='0';$('#week-select').dispatchEvent(new window.Event('change'));
    assert.equal(document.querySelectorAll('.course-card').length,4);
    assert.equal(run('dayOffset(atDay("2026-03-02"),atDay("2026-03-09"))'),7);
    console.log('PASS weekly/all-semester view, split periods, overlap layout and course detail');

    click('#demo-toggle');assert.match($('#semester-label').textContent,/示例预览/);
    assert.equal($('#export-schedule').disabled,true);assert.equal(document.querySelectorAll('.course-card').length,9);
    click('#demo-toggle');assert.match($('#semester-label').textContent,/2026/);
    assert.ok(run('timetable === savedTimetable'));
    $('#first-monday').value='2026-09-07';submit('#semester-form');await tick();
    assert.equal(scheduleData.first_monday,'2026-09-07');
    console.log('PASS demo never changes saved data and semester settings persist');

    const input=$('#import-file');Object.defineProperty(input,'files',{value:[{size:100,text:async()=>JSON.stringify(sampleSchedule)}],writable:true});
    input.dispatchEvent(new window.Event('change'));await tick();
    assert.equal(scheduleData.schedule.courses.length,3);
    click('#export-schedule');assert.equal(exported,true);
    failImport=true;input.dispatchEvent(new window.Event('change'));await tick();
    assert.equal(run('timetable.courses.length'),3);assert.match($('#import-status').textContent,/测试导入失败/);
    $('#school-cookie').value='TEST_SESSION';$('#online-dialog').close();assert.equal($('#school-cookie').value,'');
    console.log('PASS import/export, failed import preserves timetable and closing clears session input');

    click('#settings-open');$('#feature-timetable').checked=false;submit('#settings-form');await tick();
    assert.equal($('[data-page="timetable"]').hidden,true);assert.equal($('.timetable-shortcut').hidden,true);
    assert.equal($('#page-downloads').hidden,false);assert.equal($('#page-timetable').hidden,true);
    run('location.hash="#timetable";route()');assert.equal($('#page-timetable').hidden,true);
    click('#settings-open');$('#feature-downloads').checked=false;$('#feature-notes').checked=false;$('#feature-ai_assistants').checked=false;submit('#settings-form');await tick();
    assert.equal($('#page-workspace').hidden,false);assert.equal($('[data-page="downloads"]').hidden,true);
    assert.equal($('#submit-download').disabled,true);
    click('#choose-features');assert.equal($('#settings-dialog').open,true);
    $('#feature-timetable').checked=true;submit('#settings-form');await tick();
    assert.equal($('#page-timetable').hidden,false);assert.equal($('[data-page="timetable"]').hidden,false);
    const scheduleReads=requests.filter(r=>r.url==='/api/timetable').length;
    await run('refreshTasks()');await tick();
    assert.equal(requests.filter(r=>r.url==='/api/timetable').length,scheduleReads);
    console.log('PASS optional features hide navigation, guard routes, keep settings reachable and load timetable on demand');

    click('#account-open');assert.equal($('#settings-dialog').open,true);click('#account-logout');await tick();
    assert.equal($('#login-dialog').open,true);assert.equal($('#settings-dialog').open,false);
    assert.equal(run('tasks.length'),0);assert.equal(run('timetable'),null);
    assert.equal($('#account-label').textContent,'邮箱登录 / 注册');
    const beforeRegistration=requests.filter(r=>r.url==='/api/auth/register').length;
    click('[data-auth-mode="register"]');assert.equal($('#confirm-fields').hidden,false);
    $('#account-email').value='new@example.test';$('#account-password').value='password-123';$('#account-confirm').value='different';
    submit('#login-form');await tick();assert.match($('#login-error').textContent,/不一致/);
    assert.equal(requests.filter(r=>r.url==='/api/auth/register').length,beforeRegistration);
    $('#account-confirm').value='password-123';submit('#login-form');await tick();
    assert.equal($('#login-dialog').open,false);assert.equal($('#account-password').value,'');assert.equal($('#account-confirm').value,'');
    assert.equal($('#account-label').textContent,'new@example.test');
    click('#account-open');click('#account-logout');await tick();click('[data-auth-mode="login"]');
    failLogin=true;$('#account-email').value='member@example.test';$('#account-password').value='wrong-password';submit('#login-form');await tick();
    assert.equal($('#login-dialog').open,true);assert.match($('#login-error').textContent,/邮箱或密码/);
    failLogin=false;$('#account-password').value='password-123';submit('#login-form');await tick();
    assert.equal($('#login-dialog').open,false);assert.equal($('#settings-account').textContent,'member@example.test');
    run('setAuth({...auth,access_password_required:true});setAuthMode("access")');
    assert.equal($('#email-fields').hidden,true);assert.equal($('#access-fields').hidden,false);assert.equal($('#account-email').required,false);
    console.log('PASS email registration/login/logout, confirmation, failed login and private view reset');
  } finally { timers.forEach(clearTimeout); }
})().catch(error=>{console.error(error);process.exitCode=1;});

function $$(selector){return [...document.querySelectorAll(selector)];}
