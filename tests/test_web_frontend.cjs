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
const state = {version:'1.2.2-web',features:{timetable:true,notes:true}};
const stored = new Map();
const scheduleData = {schedule:null,first_monday:'',selected_week:0};
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
window.location={origin:'http://127.0.0.1'};
window.postMessage=()=>{};
window.URL = {createObjectURL:()=>{exported=true;return 'blob:test';},revokeObjectURL:()=>{}};
const context = vm.createContext({
  document,window,console,location:{hash:'#timetable'},Date,Blob,URL:window.URL,
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
    else if(url==='/api/announcements')result={announcements:[]};
    else if(url==='/api/state')result=state;
    else if(url==='/api/timetable')result=scheduleData;
    else if(url==='/api/tasks'){state.tasks.unshift({...fixtureTask});result={task:fixtureTask};}
    else if(url==='/api/settings'){state.features={...state.features,...data.features};result={ok:true,features:state.features};}
    else if(url==='/api/timetable/import'){
      if(failImport){status=400;result={error:'测试导入失败'};}
      else{scheduleData.schedule=JSON.parse(data.content);result=scheduleData;}
    }
    else if(url==='/api/timetable/week'){scheduleData.selected_week=data.selected_week;result=scheduleData;}
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
    assert.equal($('#page-downloads'),null);assert.equal($('#feature-downloads'),null);
    assert.equal($('#page-timetable').hidden,false);
    assert.equal($('#account-label').textContent,'member@example.test');
    console.log('PASS timetable is the default page; download controls are absent');

    const initialTheme=document.documentElement.dataset.theme;
    click('#mobile-theme');assert.notEqual(document.documentElement.dataset.theme,initialTheme);
    click('#theme-toggle');assert.equal(document.documentElement.dataset.theme,initialTheme);
    click('#mobile-settings');assert.equal($('#settings-dialog').open,true);
    submit('#settings-form');await tick();
    assert.equal($('#settings-dialog').open,false);
    console.log('PASS mobile theme/settings and preference save');

    scheduleData.schedule=sampleSchedule;context.fixture=sampleSchedule;
    run('acceptTimetable({schedule:fixture,first_monday:"",selected_week:1});location.hash="#timetable";route()');
    assert.equal($('#page-downloads'),null);assert.equal($('#page-timetable').hidden,false);
    assert.equal(document.querySelectorAll('.course-card').length,3); // discontinuous course becomes two cards
    assert.equal(document.querySelectorAll('#schedule-grid img').length,0);
    assert.ok($$('.course-card').some(el=>el.style.width.includes('50%')));
    click('[data-course="0"]');assert.match($('#detail-title').textContent,/数据结构/);assert.match($('#detail-content').textContent,/1、2、5/);$('#detail-dialog').close();
    run('auth.user=null');
    $('#week-select').value='2';$('#week-select').dispatchEvent(new window.Event('change'));
    await tick();assert.equal(scheduleData.selected_week,2);
    assert.equal(document.querySelectorAll('.course-card').length,1);assert.match($('#schedule-grid').textContent,/双周课程/);
    $('#week-select').value='0';$('#week-select').dispatchEvent(new window.Event('change'));
    await tick();assert.equal(scheduleData.selected_week,0);
    assert.equal(document.querySelectorAll('.course-card').length,4);
    assert.equal(run('dayOffset(atDay("2026-03-02"),atDay("2026-03-09"))'),7);
    console.log('PASS weekly/all-semester view, split periods, overlap layout and course detail');

    click('#demo-toggle');assert.equal(run('demo'),true);assert.match($('#schedule-notice').textContent,/示例数据/);
    assert.equal($('#export-schedule').disabled,true);assert.equal(document.querySelectorAll('.course-card').length,9);
    click('#demo-toggle');assert.equal(run('demo'),false);
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
    assert.equal($('[data-page="timetable"]').hidden,true);
    assert.equal($('#page-notes').hidden,false);assert.equal($('#page-timetable').hidden,true);
    run('location.hash="#timetable";route()');assert.equal($('#page-timetable').hidden,true);
    click('#settings-open');$('#feature-notes').checked=false;submit('#settings-form');await tick();
    assert.equal($('#page-home').hidden,false);assert.equal($('[data-page="downloads"]'),null);
    click('#choose-features');assert.equal($('#settings-dialog').open,true);
    $('#feature-timetable').checked=true;submit('#settings-form');await tick();
    assert.equal($('#page-timetable').hidden,false);assert.equal($('[data-page="timetable"]').hidden,false);
    const scheduleReads=requests.filter(r=>r.url==='/api/timetable').length;
    await run('refreshState()');await tick();
    assert.equal(requests.filter(r=>r.url==='/api/timetable').length,scheduleReads);
    console.log('PASS optional features hide navigation, guard routes, keep settings reachable and load timetable on demand');

    click('#account-open');assert.equal($('#settings-dialog').open,true);click('#account-logout');await tick();
    assert.ok(!$('#login-dialog').open);assert.equal($('#settings-dialog').open,false);
    assert.equal(run('timetable'),null);
    assert.equal($('#account-label').textContent,'登录 / 注册');
    click('#account-open');assert.equal($('#login-dialog').open,true);
    const beforeRegistration=requests.filter(r=>r.url==='/api/auth/register').length;
    click('[data-auth-mode="register"]');assert.equal($('#confirm-fields').hidden,false);
    $('#account-email').value='new@example.test';$('#account-password').value='password-123';$('#account-confirm').value='different';
    submit('#login-form');await tick();assert.match($('#login-error').textContent,/不一致/);
    assert.equal(requests.filter(r=>r.url==='/api/auth/register').length,beforeRegistration);
    $('#account-confirm').value='password-123';submit('#login-form');await tick();
    assert.equal($('#login-dialog').open,false);assert.equal($('#account-password').value,'');assert.equal($('#account-confirm').value,'');
    assert.equal($('#account-label').textContent,'new@example.test');
    click('#account-open');click('#account-logout');await tick();click('#account-open');click('[data-auth-mode="login"]');
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
