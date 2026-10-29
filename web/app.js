'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const paths = {
  plus:'M12 5v14M5 12h14', search:'M21 21l-5-5M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14',
  more:'M5 12h.01M12 12h.01M19 12h.01',
  trash:'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7',
  history:'M3 3v6h6M3 9a9 9 0 1 1 0 6M12 7v5l3 2',
  download:'M12 3v12m-5-5 5 5 5-5M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4',
  upload:'M12 16V4m-5 5 5-5 5 5M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4',
  calendar:'M8 2v4m8-4v4M3 10h18M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2M7 14h2m6 0h2m-10 4h2',
  layers:'m12 3 10 5-10 5L2 8l10-5Zm-10 9 10 5 10-5M2 16l10 5 10-5',
  settings:'M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1 1-3Zm3 6a3 3 0 1 0 0 6 3 3 0 0 0 0-6',
  moon:'M21 13A9 9 0 0 1 11 3a9 9 0 1 0 10 10Z',
  sun:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5',
  refresh:'M20 7v5h-5M4 17v-5h5M6.1 6.1A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.9 5.9',
  play:'M6 3v18l15-9L6 3Z',
  music:'M9 18V5l12-2v13M9 18a3 3 0 1 1-3-3c1.7 0 3 1 3 3Zm12-2a3 3 0 1 1-3-3c1.7 0 3 1 3 3ZM9 9l12-2',
  file:'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Zm0 0v6h6M8 13h8m-8 4h5',
  image:'M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm-2 14 6-6 4 4 3-3 5 5M8 7h.01',
  folder:'M3 5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v12H3V5Z',
  link:'M10 13a5 5 0 0 0 7 0l4-4a5 5 0 0 0-7-7l-2 2M14 11a5 5 0 0 0-7 0l-4 4a5 5 0 0 0 7 7l2-2',
  'arrow-right':'M4 12h16m-6-6 6 6-6 6',
  'chevron-left':'m15 5-7 7 7 7', 'chevron-right':'m9 5 7 7-7 7',
  activity:'M2 12h5l3-9 4 18 3-9h5', check:'m5 12 4 4L19 6',
  sparkles:'m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3ZM20 2v4m-2-2h4',
  cpu:'M6 6h12v12H6V6Zm4 4h4v4h-4v-4ZM9 2v4m6-4v4m-6 12v4m6-4v4M2 9h4m-4 6h4m12-6h4m-4 6h4',
  external:'M14 3h7v7m0-7L11 13M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5',
  graduation:'m2 9 10-5 10 5-10 5L2 9Zm4 2v6q6 5 12 0v-6M22 9v9',
  info:'M12 8h.01M11 12h1v5M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20',
  x:'m6 6 12 12M6 18 18 6',
  user:'M20 21v-2a7 7 0 0 0-14 0v2M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8',
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.file}"/></svg>`;
function icons(root = document) { $$('[data-icon]', root).forEach(el => { el.outerHTML = icon(el.dataset.icon); }); }
icons();
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let selectedWeek = 0, savedSelectedWeek = 0;
let timetableRevision = 0;
let timetable = null, savedTimetable = null, firstMonday = '', demo = false, connected = false;
let schoolSession = '', schoolAttempt = null, schoolPollTimer;
let toastTimer, polling = false, activePage = '';
let features = {timetable:true,notes:true}, auth = {authenticated:false,user:null,access_password_required:false};
let authMode = 'login', authGeneration = 0, timetableLoaded = false, timetableLoading = false;
const terms = {3:'第一学期',12:'第二学期',16:'第三学期'};
function toast(message, error = false) {
  const el = $('#toast'); el.textContent = message; el.classList.toggle('error', error); el.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; }, 5000);
}
function showDialog(id) {
  const el = $(`#${id}`); if (!el.open) el.showModal();
  if(id==='settings-dialog')loadPublicAsset('/account-sync.css','style').catch(error=>toast(error.message,true));
}
const publicAssetLoads=new Map();
const publicAssetsReady=new Set();
let publicAssetVersions={};
try{publicAssetVersions=JSON.parse($('#asset-manifest')?.textContent||'{}');}catch(_){}
function loadPublicAsset(path,kind='script'){
  if(publicAssetLoads.has(path))return publicAssetLoads.get(path);
  const element=document.createElement(kind==='style'?'link':'script');
  const url=path+(publicAssetVersions[path]?`?v=${publicAssetVersions[path]}`:'');
  if(kind==='style'){element.rel='stylesheet';element.href=url;}else element.src=url;
  const promise=new Promise((resolve,reject)=>{
    element.onload=()=>{publicAssetsReady.add(path);resolve();};
    element.onerror=()=>{publicAssetLoads.delete(path);element.remove();reject(new Error('功能资源加载失败，请重试。'));};
  });
  publicAssetLoads.set(path,promise);document.head.appendChild(element);return promise;
}
function ensureAccountSync(){
  if(typeof loadAccountSync==='function')return Promise.resolve();
  return loadPublicAsset('/draft-store.js').then(()=>loadPublicAsset('/account-sync.js'));
}
function ensureMemos(){
  if(typeof loadMemos==='function')return Promise.resolve();
  return Promise.all([loadPublicAsset('/memos.css','style'),loadPublicAsset('/draft-store.js'),loadPublicAsset('/memo-editor.js')])
    .then(()=>loadPublicAsset('/memos.js'));
}
const featurePreparations=new Map();
function featureLoadStatus(page,text='',retry=false){
  const root=$(`#page-${page}`);let status=$('.feature-loading',root);
  if(!status){status=document.createElement('div');status.className='feature-loading';status.setAttribute('role','status');root.insertBefore(status,root.children[1]||null);}
  status.replaceChildren();status.hidden=!text;
  if(text){const label=document.createElement('span');label.textContent=text;status.append(label);}
  if(retry){const button=document.createElement('button');button.className='button compact';button.textContent='重新加载';button.onclick=()=>prepareFeature(page,true);status.append(button);}
}
async function prepareFeature(page,force=false){
  const root=$(`#page-${page}`),isNotes=page==='notes';
  const ready=isNotes?typeof loadMemos==='function':typeof loadAccountSync==='function'&&publicAssetsReady.has('/account-sync.css');
  if(ready){
    if(isNotes){if(connected)loadMemos(force);return;}
    if(connected)loadAccountSync();return;
  }
  if(featurePreparations.has(page))return featurePreparations.get(page);
  root.setAttribute('data-feature-pending','');root.setAttribute('aria-busy','true');
  featureLoadStatus(page,'正在加载此功能…');
  const task=(async()=>{
    try{
      if(isNotes)await ensureMemos();
      else await Promise.all([ensureAccountSync(),loadPublicAsset('/account-sync.css','style')]);
      root.removeAttribute('data-feature-pending');featureLoadStatus(page);
      if(activePage===page&&features[page]){
        if(isNotes){renderMemoAccess();renderMemoList();if(connected)await loadMemos(true);}
        else if(connected)await loadAccountSync();
      }
    }catch(error){featureLoadStatus(page,error.message,true);}
    finally{root.removeAttribute('aria-busy');featurePreparations.delete(page);}
  })();
  featurePreparations.set(page,task);return task;
}
async function requestAccountSync(){
  if(!auth.user){showDialog('login-dialog');return;}
  const generation=authGeneration;
  try{await ensureAccountSync();if(generation===authGeneration)await loadAccountSync(true);}
  catch(error){if(generation===authGeneration)toast(error.message,true);}
}
async function syncTheme(theme){
  const generation=authGeneration;
  try{await ensureAccountSync();if(generation===authGeneration)await saveCloudTheme(theme);}
  catch(error){if(generation===authGeneration)toast(error.message,true);}
}
async function api(path, body) {
  const generation=authGeneration;
  const options = {headers:{'X-Omini-Client':'web'}, credentials:'same-origin'};
  if (body !== undefined) { options.method='POST'; options.headers['Content-Type']='application/json'; options.body=JSON.stringify(body); }
  const response = await fetch(path, options);
  const value = await response.json();
  if (response.status === 401 && generation===authGeneration && !['/api/login','/api/auth/login','/api/auth/register'].includes(path)) requireLogin();
  if (!response.ok) {const error=new Error(value.error || '请求失败，请重试。');error.status=response.status;throw error;}
  return value;
}
function setConnection(ok) {
  connected = ok;
  $('#connection').classList.toggle('offline', !ok);
  $('#connection').innerHTML = `<i></i>${ok ? '服务已连接' : '服务未连接'}`;
}
function route() {
  const wanted = ['timetable','notes','announcements','chat','trends'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'timetable';
  const page = ['announcements','chat','trends'].includes(wanted) || features[wanted] ? wanted : Object.keys(features).find(key=>features[key]) || 'home';
  const previous=activePage,entering=activePage!==page;activePage=page;
  if (location.hash !== `#${page}`) location.hash = `#${page}`;
  $$('.page').forEach(el => { el.hidden = el.id !== `page-${page}`; });
  $$('[data-page]').forEach(el => el.classList.toggle('active', el.dataset.page === page));
  const title = {timetable:'学校课表',notes:'超级备忘录',announcements:'公告',chat:'ChatGPT 对话',trends:'热搜大全',home:'首页'}[page];
  $('#breadcrumb-page').textContent = title;
  document.title = `${title} · Omini`;
  if (page === 'timetable' && connected && !timetableLoaded) loadTimetable();
  if (page === 'notes') prepareFeature(page,entering);
  document.body.classList.toggle('chat-page-active',page==='chat');
  window.dispatchEvent(new window.CustomEvent('omini-route-change',{detail:{page,previous}}));
}
function applyFeatures(selected = features) {
  for (const key of ['timetable','notes']) {
    if (typeof selected[key] === 'boolean') features[key] = selected[key];
    $(`[data-page="${key}"]`).hidden = !features[key];
    if (!$('#settings-dialog').open) $(`#feature-${key}`).checked = features[key];
  }
  if (!features.timetable) timetableLoaded = false;
  route();
}
window.addEventListener('hashchange', route); route();
const now = new Date();
$('#today-date').textContent = now.toLocaleDateString('zh-CN', {month:'long',day:'numeric',weekday:'long'});
function setTheme(theme, sync = false) {
  if(window.OminiAppearance)window.OminiAppearance.setTheme(theme);
  else document.documentElement.dataset.theme = theme;
  $('#theme-label').textContent = theme === 'dark' ? '切换浅色模式' : '切换深色模式';
  try { localStorage.setItem('omini-theme', theme); } catch (_) {}
  if(sync && auth.user)syncTheme(theme);
}
let theme = 'dark';
try { theme = window.OminiAppearance?.getTheme() || localStorage.getItem('omini-theme') || 'dark'; } catch (_) {}
setTheme(theme);
$('#theme-toggle').addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark', true));
$('#mobile-theme').addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark', true));
$('#mobile-settings').addEventListener('click', () => showDialog('settings-dialog'));
$$('[data-close]').forEach(el => el.addEventListener('click', () => $(`#${el.dataset.close}`).close()));
$$('dialog').forEach(el => el.addEventListener('click', e => {
  const r = el.getBoundingClientRect();
  if (e.target === el && (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom)) el.close();
}));
function setAuth(value) {
  if(auth.user?.id!==value.user?.id||Boolean(auth.authenticated)!==Boolean(value.authenticated))resetWorkspace();
  auth = value;
  const label = auth.user?.display_name || auth.user?.email || (auth.authenticated ? '服务账号' : '登录 / 注册');
  $('#account-label').textContent = label; $('#account-open').title = label;
  $('#sidebar-account-label').textContent = auth.user?.display_name || auth.user?.email || (auth.authenticated ? '账号设置' : '扫码 / 邮箱登录');
  $('#settings-account').textContent = auth.user?.display_name || auth.user?.email || (auth.authenticated ? '已通过服务访问密码登录' : '尚未登录');
  $('#account-logout').hidden = !auth.authenticated;
  $('#access-mode').hidden = !auth.access_password_required;
  if($('#settings-admin'))$('#settings-admin').hidden=auth.user?.role!=='admin';
  window.dispatchEvent(new window.Event('omini-auth-change'));
  setAuthMode(authMode);
  if(typeof renderMemoAccess==='function'){renderMemoAccess();renderMemoList();}
}
function setAuthMode(mode) {
  authMode = mode === 'access' && !auth.access_password_required ? 'login' : mode;
  const register = authMode === 'register', access = authMode === 'access';
  $('#auth-title').textContent = $('#auth-submit').textContent = register ? '邮箱注册' : access ? '访问密码登录' : '邮箱登录';
  $('#email-fields').hidden = access;
  $('#confirm-fields').hidden = !register;
  $('#account-email').required = $('#account-password').required = !access;
  $('#account-confirm').required = register;
  $('#account-password').autocomplete = register ? 'new-password' : 'current-password';
  $('#access-fields').hidden = !(access || register && auth.access_password_required);
  $('#access-password').required = !$('#access-fields').hidden;
  $('#login-error').textContent = '';
  $$('[data-auth-mode]').forEach(button=>{const selected=button.dataset.authMode===authMode;button.classList.toggle('selected',selected);button.setAttribute('aria-selected',String(selected));});
}
$$('[data-auth-mode]').forEach(button=>button.addEventListener('click',()=>setAuthMode(button.dataset.authMode)));
function resetWorkspace() {
  if(typeof resetAccountSync==='function')resetAccountSync();
  if(typeof resetMemos==='function')resetMemos();
  cancelSchoolImport(); schoolSession=''; $('#school-connection-status').textContent='尚未连接教务会话。';
  authGeneration++; timetableRevision=0; selectedWeek=0; savedSelectedWeek=0; timetable=null; savedTimetable=null; firstMonday=''; demo=false; timetableLoaded=false;
  updateWeekOptions(); renderTimetable(); setConnection(false);
  $('#detail-content').textContent=''; $('#school-cookie').value='';
  $('#detail-title').textContent='';
  $('#table-file').disabled=false;$('#table-file').value='';
  $('#import-file').disabled=false;$('#import-file').value='';$('#import-status').textContent='';
  $('#online-submit').disabled=false;$('#online-submit').textContent='在线更新';
}
function requireLogin() {
  setAuth({...auth,authenticated:false,user:null});
  $$('dialog').forEach(dialog=>{if(dialog.id!=='login-dialog'&&dialog.open)dialog.close();});
  restoreGuestFeatures();
  setConnection(true);
}
function restoreGuestFeatures(){
  let selected={timetable:true,notes:true};
  try{const saved=JSON.parse(localStorage.getItem('omini-guest-features')||'{}');for(const key of Object.keys(selected))if(typeof saved?.[key]==='boolean')selected[key]=saved[key];}catch(_){}
  applyFeatures(selected);
}
function openAccount(){if(auth.authenticated)showDialog('settings-dialog');else showDialog('login-dialog');}
$('#account-open').addEventListener('click',openAccount);
$('#account-sidebar').addEventListener('click',openAccount);
$('#choose-features').addEventListener('click',()=>showDialog('settings-dialog'));
$('#account-logout').addEventListener('click',async()=>{
  const button=$('#account-logout');button.disabled=true;
  try{await api('/api/auth/logout',{});requireLogin();setConnection(true);toast('已退出登录，可继续浏览');}
  catch(error){toast(error.message,true);}finally{button.disabled=false;}
});
$('#login-form').addEventListener('submit', async e => {
  e.preventDefault(); const button = $('#auth-submit'); button.disabled = true;
  try {
    if(authMode==='register'&&$('#account-password').value!==$('#account-confirm').value)throw new Error('两次输入的密码不一致。');
    const path=authMode==='access'?'/api/login':`/api/auth/${authMode}`;
    const body=authMode==='access'?{password:$('#access-password').value}:{email:$('#account-email').value.trim(),password:$('#account-password').value,access_password:$('#access-password').value};
    await api(path,body); authGeneration++;
    $('#account-password').value='';$('#account-confirm').value='';$('#access-password').value='';
    $('#login-error').textContent=''; $('#login-dialog').close(); await loadInitial();
  }
  catch (error) { $('#login-error').textContent=error.message; }
  finally { button.disabled=false; }
});
async function refreshState() {
  if (polling||!auth.authenticated) return;
  polling=true;const generation=authGeneration;
  try {const state=await api('/api/state');if(generation!==authGeneration)return;
    setConnection(true);applyFeatures(state.features);$('#version-label').textContent=state.version;
    if(auth.user)ensureAccountSync().then(()=>{if(generation===authGeneration)loadAccountSync();})
      .catch(error=>{if(generation===authGeneration)$('#settings-sync-status').textContent=error.message;});
  }catch(error){if(generation===authGeneration)setConnection(false);}finally{polling=false;}
}
$('#settings-open').addEventListener('click',()=>showDialog('settings-dialog'));
$('#settings-form').addEventListener('submit',async e=>{
  e.preventDefault();const button=$('#settings-save');button.disabled=true;
  try{const selected={timetable:$('#feature-timetable').checked,notes:$('#feature-notes').checked};
    if(!auth.authenticated){
      try{localStorage.setItem('omini-guest-features',JSON.stringify(selected));}catch(_){}
      $('#settings-dialog').close();applyFeatures(selected);toast('浏览偏好已保存在当前浏览器');return;
    }
    const result=await api('/api/settings',{features:selected});
    $('#settings-dialog').close();applyFeatures(result.features||selected);await refreshState();toast('功能加载与设置已保存');
  }catch(error){toast(error.message,true);}finally{button.disabled=false;}
});
$('#chatgpt-reset-open').addEventListener('click',()=>showDialog('chatgpt-reset-dialog'));
$('#settings-sync-now').addEventListener('click',requestAccountSync);

// Timetable dates use local calendar days to avoid DST offset errors.
function localISO(d) { return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }
function atDay(text) { const [y,m,d]=text.split('-').map(Number);return new Date(y,m-1,d,12); }
function dayOffset(start,end) { return Math.round((Date.UTC(end.getFullYear(),end.getMonth(),end.getDate())-Date.UTC(start.getFullYear(),start.getMonth(),start.getDate()))/86400000); }
function currentWeek() { return firstMonday ? Math.floor(dayOffset(atDay(firstMonday),new Date())/7)+1 : null; }
function maxWeeks() { return Math.max(20,...(timetable?.courses||[]).flatMap(c=>c.weeks)); }
function updateWeekOptions() {
  $('#week-select').innerHTML = '<option value="0">全学期</option>'+Array.from({length:maxWeeks()},(_,i)=>`<option value="${i+1}">第 ${i+1} 周</option>`).join('');
  if (selectedWeek>maxWeeks()) selectedWeek=1;
  $('#week-select').value=selectedWeek;
}
function acceptTimetable(data) {
  timetableRevision=data.revision??timetableRevision;
  timetableLoaded=true;
  savedTimetable=data.schedule; timetable=data.schedule; firstMonday=data.first_monday||''; demo=false;
  $('#demo-toggle').textContent='预览示例课表'; $('#first-monday').value=firstMonday;
  if(Number.isInteger(data.selected_week))selectedWeek=data.selected_week;
  savedSelectedWeek=selectedWeek;
  updateWeekOptions();renderTimetable();
}
function splitPeriods(periods) {
  const sorted=[...new Set(periods)].sort((a,b)=>a-b), chunks=[];
  for(const p of sorted){if(chunks.length&&chunks.at(-1).end===p-1)chunks.at(-1).end=p;else chunks.push({start:p,end:p});}return chunks;
}
function renderTimetable() {
  const courses=timetable?.courses||[], current=currentWeek();
  $('#schedule-notice').textContent=demo?'正在预览示例数据，不会覆盖你的课表。':firstMonday?`第一周周一：${firstMonday}${current&&current>0&&current<=maxWeeks()?` · 当前为第 ${current} 周`: ' · 当前日期不在所设学期内'}`:'尚未设置开学日期，可按周次查看；设置后即可定位本周。';
  $('#schedule-updated').textContent=timetable?demo?'示例数据，仅供预览':`更新于 ${timetable.updated_at?.replace('T',' ')||'导入时'}`:'尚未导入课表';
  $('#export-schedule').disabled=!timetable||demo;
  const weekday=(new Date().getDay()+6)%7+1;
  if(!timetable){$('#schedule-grid').innerHTML=`<div class="schedule-empty"><span class="empty-icon">${icon('calendar')}</span><h3>属于你的新学期，还差一张课表</h3><p>通过表格、教务登录或 HAR / JSON 导入课表，课程会自动整理到周视图中。</p><button class="button primary" id="empty-import">${icon('upload')}导入我的课表</button></div>`;$('#empty-import').addEventListener('click',openImport);return;}
  const visible=courses.filter(c=>!selectedWeek||c.weeks.includes(selectedWeek));
  const lastPeriod=Math.max(12,...courses.flatMap(c=>c.periods));
  const names=[...new Set(courses.map(c=>c.name))];
  let grid=`<div class="week-grid"><div class="period-column"><div class="day-header">节次 / 时间</div>${Array.from({length:lastPeriod},(_,i)=>{const t=timetable.period_times?.[i+1];return `<div class="period-label">${String(i+1).padStart(2,'0')}<small>${t?`${esc(t.start)}<br>${esc(t.end)}`:''}</small></div>`;}).join('')}</div>`;
  for(let day=1;day<=7;day++){
    let dateText='';if(firstMonday&&selectedWeek){const d=atDay(firstMonday);d.setDate(d.getDate()+(selectedWeek-1)*7+day-1);dateText=`${d.getMonth()+1}.${String(d.getDate()).padStart(2,'0')}`;}
    const isToday=!demo&&selectedWeek===current&&day===weekday;
    const events=[];
    visible.forEach(c=>{if(c.weekday===day)splitPeriods(c.periods).forEach(chunk=>events.push({...chunk,course:c,index:courses.indexOf(c)}));});
    events.sort((a,b)=>a.start-b.start||a.end-b.end);
    // Partition overlap clusters; concurrent classes remain visible side by side.
    let cluster=[],end=0;
    const place=()=>{const lanes=[];for(const event of cluster){let lane=lanes.findIndex(last=>last<event.start);if(lane<0)lane=lanes.length;lanes[lane]=event.end;event.lane=lane;}cluster.forEach(event=>event.lanes=lanes.length);cluster=[];};
    for(const event of events){if(cluster.length&&event.start>end){place();end=0;}cluster.push(event);end=Math.max(end,event.end);}if(cluster.length)place();
    grid+=`<div class="day-column ${isToday?'is-today':''}" style="height:${57+lastPeriod*76}px"><div class="day-header"><strong>周${'一二三四五六日'[day-1]}</strong><span>${dateText}</span></div>`;
    for(const event of events){const c=event.course,color=names.indexOf(c.name)%6;grid+=`<button class="course-card course-${color}" data-course="${event.index}" style="top:${57+(event.start-1)*76+4}px;height:${(event.end-event.start+1)*76-8}px;left:calc(${event.lane/event.lanes*100}% + 4px);width:calc(${100/event.lanes}% - 8px)"><strong>${esc(c.name)}</strong><small>${esc(c.location||'教室待定')}</small><small>${esc(c.teacher)}</small><em>第 ${event.start}${event.end!==event.start?`–${event.end}`:''} 节</em>${!selectedWeek?`<em>${esc(c.week_text)}</em>`:''}</button>`;}
    grid+='</div>';
  }
  $('#schedule-grid').innerHTML=grid+'</div>';
}
$('#schedule-grid').addEventListener('click',e=>{
  const button=e.target.closest('[data-course]');if(!button)return;
  const c=timetable.courses[Number(button.dataset.course)];
  $('#detail-title').textContent=c.name;
  const pairs=[['上课时间',`周${'一二三四五六日'[c.weekday-1]} · 第 ${c.periods.join('、')} 节`],['教学周次',c.week_text],['上课地点',c.location||'未提供'],['任课教师',c.teacher||'未提供'],['校区',c.campus||'未提供'],['学分',c.credits||'未提供'],['教学班',c.class_name||'未提供']];
  $('#detail-content').innerHTML=`<dl class="course-details">${pairs.map(([k,v])=>`<dt>${k}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`;showDialog('detail-dialog');
});
async function selectWeek(week) {
  selectedWeek=week;$('#week-select').value=week;renderTimetable();
  if(demo||!auth.authenticated)return;
  savedSelectedWeek=week;const generation=authGeneration;
  try {
    if(auth.user){
      await ensureAccountSync();if(generation!==authGeneration)return;
      if(!cloudReady)await loadAccountSync(true);
      if(generation!==authGeneration)return;
      await queueCloudData('schedule',{...cloudData('schedule'),schedule:savedTimetable,first_monday:firstMonday,selected_week:week});
    }else{const data=await api('/api/timetable/week',{selected_week:week});if(generation===authGeneration)acceptTimetable(data);}
  }catch(error){if(generation===authGeneration)toast(error.message,true);}
}
$('#week-select').addEventListener('change',e=>{selectWeek(Number(e.target.value));});
$('#prev-week').addEventListener('click',()=>{selectWeek(Math.max(1,selectedWeek-1));});
$('#next-week').addEventListener('click',()=>{selectWeek(Math.min(maxWeeks(),selectedWeek+1));});
$('#current-week').addEventListener('click',()=>{if(!firstMonday){showDialog('semester-dialog');return;}const current=currentWeek();if(current<1||current>maxWeeks()){toast('当前日期不在所设学期内，请检查开学日期。',true);return;}selectWeek(current);});
$('#semester-open').addEventListener('click',()=>showDialog('semester-dialog'));
$('#semester-form').addEventListener('submit',async e=>{
  e.preventDefault();if(demo){toast('请先退出示例预览，再设置自己的学期。',true);return;}
  if(!auth.authenticated){showDialog('login-dialog');return;}
  const generation=authGeneration;
  try{const data=await api('/api/timetable/semester',{first_monday:$('#first-monday').value,base_revision:timetableRevision});if(generation!==authGeneration)return;acceptTimetable(data);$('#semester-dialog').close();toast('学期日期已保存');}
  catch(error){if(generation===authGeneration)toast(error.message,true);}
});
function openImport(){
  const date=new Date();$('#table-year').value=timetable?.year||(date.getMonth()>=7?date.getFullYear():date.getFullYear()-1);$('#table-term').value=timetable?.term||(date.getMonth()>=7?'3':'12');
  showDialog('import-dialog');
}
$('#import-open').addEventListener('click',openImport);
$$('[data-import-mode]').forEach(button=>button.addEventListener('click',()=>{
  $$('[data-import-mode]').forEach(tab=>{const selected=tab===button;tab.setAttribute('aria-selected',String(selected));$(`#import-${tab.dataset.importMode}-panel`).hidden=!selected;});
  $('#import-status').textContent='';
}));
$('#table-template').addEventListener('click',()=>{
  const content='\ufeff课程名称,星期,节次,周次,教师,教室,校区,学分\r\n高等数学,周一,1-2,1-16周,张老师,A201,广州,3\r\n';
  const url=URL.createObjectURL(new Blob([content],{type:'text/csv;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download='Omini-课表模板.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
$('#table-file').addEventListener('change',async e=>{
  const input=e.target,file=input.files[0],generation=authGeneration,revision=timetableRevision;if(!file)return;
  if(!auth.authenticated){input.value='';showDialog('login-dialog');return;}
  if(file.size>24*1024*1024){toast('请选择小于 24 MB 的文件。',true);input.value='';return;}
  input.disabled=true;$('#import-status').textContent='正在解析表格…';
  try{
    let content;
    if(file.name.toLowerCase().endsWith('.xlsx')){
      const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(let offset=0;offset<bytes.length;offset+=8192)binary+=String.fromCharCode(...bytes.subarray(offset,offset+8192));content=btoa(binary);
    }else content=await file.text();
    if(generation!==authGeneration)return;
    const data=await api('/api/timetable/import',{format:'table',filename:file.name,content,year:$('#table-year').value,term:$('#table-term').value,base_revision:revision});
    if(generation!==authGeneration)return;acceptTimetable(data);$('#import-dialog').close();toast(`已导入 ${data.schedule.courses.length} 条课程安排`);
  }catch(error){if(generation===authGeneration){$('#import-status').textContent=error.message;toast(error.message,true);}}
  finally{if(generation===authGeneration){input.disabled=false;input.value='';}}
});
$('#import-file').addEventListener('change',async e=>{
  const input=e.target,file=input.files[0],generation=authGeneration,revision=timetableRevision;if(!file)return;
  if(!auth.authenticated){input.value='';showDialog('login-dialog');return;}
  if(file.size>24*1024*1024){toast('请选择小于 24 MB 的文件。',true);input.value='';return;}
  input.disabled=true;$('#import-status').textContent='正在解析课表…';
  try{const content=await file.text();if(generation!==authGeneration)return;const data=await api('/api/timetable/import',{content,base_revision:revision});if(generation!==authGeneration)return;acceptTimetable(data);$('#import-dialog').close();toast(`已导入 ${data.schedule.courses.length} 条课程安排`);}
  catch(error){if(generation===authGeneration){$('#import-status').textContent=error.message;toast(error.message,true);}}
  finally{if(generation===authGeneration){input.disabled=false;input.value='';}}
});
function cancelSchoolImport(){
  const attempt=schoolAttempt;schoolAttempt=null;clearTimeout(schoolPollTimer);
  setSchoolImportBusy(false);
  if(attempt?.id&&attempt.generation===authGeneration)api(attempt.signin?'/api/auth/school/cancel':'/api/timetable/school/cancel',{id:attempt.id}).catch(()=>{});
}
function setSchoolImportBusy(busy){
  $('#online-submit').disabled=busy;
  $('#school-year').disabled=busy;$('#school-term').disabled=busy;
}
async function pollSchool(attempt){
  if(schoolAttempt!==attempt||attempt.generation!==authGeneration)return;
  try{
    const result=await api(attempt.signin?'/api/auth/school/poll':'/api/timetable/school/poll',{id:attempt.id});
    if(schoolAttempt!==attempt||attempt.generation!==authGeneration)return;
    if(result.status==='complete'){
      if(attempt.signin){schoolAttempt=null;clearTimeout(schoolPollTimer);setAuth({authenticated:true,user:result.user,access_password_required:auth.access_password_required});setConnection(true);applyFeatures({timetable:true});if($('#login-dialog').open)$('#login-dialog').close();refreshState();}
      acceptTimetable(result.result);location.hash='#timetable';$('#school-qr').hidden=true;$('#online-dialog').close();toast(`已自动保存并导入 ${result.result.schedule.courses.length} 条课程安排`);return;
    }
    if(result.status==='expired')throw new Error('二维码已失效，请重新扫码登录。');
    $('#school-connection-status').textContent=result.status==='scanned'?'已扫码，请在微信中确认；随后自动保存到当前账号并显示课表，无需其他操作。':'请使用已绑定学校账号的微信扫码；确认后自动保存并显示课表，无需手动导入。';
    schoolPollTimer=setTimeout(()=>pollSchool(attempt),1200);
  }catch(error){
    if(schoolAttempt===attempt&&attempt.generation===authGeneration){$('#school-connection-status').textContent=error.message;$('#school-qr').hidden=true;cancelSchoolImport();}
  }
}
async function openSchool(){
  if(!$('#online-dialog').open)openOnline();
  cancelSchoolImport();
  const attempt={id:null,generation:authGeneration,signin:!auth.user};schoolAttempt=attempt;
  setSchoolImportBusy(true);
  $('#school-qr').hidden=true;$('#school-connection-status').textContent='正在直接连接学校统一认证并生成微信登录二维码…';$('#online-login').disabled=true;
  try{
    const result=await api(attempt.signin?'/api/auth/school/start':'/api/timetable/school/start',{year:$('#school-year').value,term:$('#school-term').value,base_revision:timetableRevision});
    if(schoolAttempt!==attempt||attempt.generation!==authGeneration){if(attempt.generation===authGeneration)api(attempt.signin?'/api/auth/school/cancel':'/api/timetable/school/cancel',{id:result.id}).catch(()=>{});return;}
    attempt.id=result.id;$('#school-qr').src=result.qr_image;$('#school-qr').hidden=false;
    $('#school-connection-status').textContent='微信确认登录后，课表自动保存到当前账号并显示，无需下载文件或手动导入。';pollSchool(attempt);
  }catch(error){if(schoolAttempt===attempt&&attempt.generation===authGeneration){$('#school-connection-status').textContent=error.message;cancelSchoolImport();}}
  finally{if(attempt.generation===authGeneration)$('#online-login').disabled=false;}
}
function openOnline(){
  const date=new Date();$('#school-year').value=timetable?.year||(date.getMonth()>=7?date.getFullYear():date.getFullYear()-1);$('#school-term').value=timetable?.term||(date.getMonth()>=7?'3':'12');
  $('#school-cookie').value=schoolSession;$('#school-qr').hidden=true;showDialog('online-dialog');
  if(auth.authenticated){const generation=authGeneration;api('/api/timetable/school/session').then(result=>{if(generation===authGeneration&&!schoolAttempt)$('#school-connection-status').textContent=result.connected?'学校会话已连接，点击在线更新即可直接抓取课表。':'点击微信扫码登录，确认后自动抓取课表；无需安装扩展。';}).catch(()=>{});}
}
$('#school-login').addEventListener('click',()=>{$('#import-dialog').close();openOnline();openSchool();});
$('#school-signin').addEventListener('click',()=>{$('#login-dialog').close();openOnline();openSchool();});
$('#online-login').addEventListener('click',openSchool);
$('#online-open').addEventListener('click',openOnline);
$('#online-dialog').addEventListener('close',()=>{$('#school-cookie').value='';$('#school-qr').hidden=true;$('#school-qr').removeAttribute('src');cancelSchoolImport();});
$('#online-form').addEventListener('submit',async e=>{
  e.preventDefault();if(!auth.authenticated){$('#school-cookie').value='';showDialog('login-dialog');return;}
  schoolSession=$('#school-cookie').value;
  cancelSchoolImport();
  const button=$('#online-submit'),generation=authGeneration;button.disabled=true;button.textContent='正在读取…';
  try{const data=await api('/api/timetable/refresh',{cookie:$('#school-cookie').value,year:$('#school-year').value,term:$('#school-term').value,base_revision:timetableRevision});if(generation!==authGeneration)return;acceptTimetable(data);$('#online-dialog').close();toast('课表已更新');}
  catch(error){if(generation===authGeneration)toast(error.message,true);}
  finally{if(generation===authGeneration){button.disabled=false;button.textContent='在线更新';$('#school-cookie').value='';}}
});
$('#export-schedule').addEventListener('click',()=>{if(!timetable||demo)return;const url=URL.createObjectURL(new Blob([JSON.stringify(timetable,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=`Omini-课表-${timetable.year}-${timetable.term}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('课表 JSON 已导出');});
$('#demo-toggle').addEventListener('click',()=>{
  if(demo){timetable=savedTimetable;selectedWeek=savedSelectedWeek;demo=false;$('#demo-toggle').textContent='预览示例课表';updateWeekOptions();renderTimetable();return;}
  demo=true;
  const rows=[['设计思维与表达','林老师','创意楼 A201',1,1,2],['高等数学','陈老师','教学楼 B302',2,3,4],['大学英语','周老师','教学楼 A405',3,1,2],['数据结构','李老师','实验楼 C203',4,3,4],['数字媒体设计','张老师','创意楼 A102',5,1,3],['体育与健康','王老师','综合运动场',1,7,8],['程序设计实践','刘老师','计算机实验室',3,5,7],['大学英语','周老师','教学楼 A405',5,5,6],['设计思维与表达','林老师','创意楼 A201',2,9,10]];
  timetable={schema:1,year:String(new Date().getFullYear()),term:'3',updated_at:'示例',period_times:{},courses:rows.map(([name,teacher,location,weekday,start,end])=>({name,teacher,location,weekday,periods:Array.from({length:end-start+1},(_,i)=>start+i),weeks:Array.from({length:16},(_,i)=>i+1),week_text:'1-16周 · 示例',campus:'示例校区',credits:'',class_name:''}))};
  selectedWeek=1;$('#demo-toggle').textContent='退出示例预览';updateWeekOptions();renderTimetable();toast('正在预览示例课表，你的数据保持原样');
});
async function loadTimetable(){
  if(timetableLoading||!features.timetable||!connected||!auth.authenticated)return;
  timetableLoading=true;const generation=authGeneration;
  try{const data=await api('/api/timetable');if(generation===authGeneration&&features.timetable)acceptTimetable(data);}
  catch(error){if(generation===authGeneration)toast(error.message,true);}finally{timetableLoading=false;}
}
async function loadInitial(){
  const generation=authGeneration;
  try{const session=await api('/api/auth/session');if(generation!==authGeneration)return;setAuth(session);if(!auth.authenticated){setConnection(true);restoreGuestFeatures();return;}await refreshState();}
  catch(error){setConnection(false);toast(error.message,true);}
}
updateWeekOptions();renderTimetable();setAuth(auth);loadInitial();
setInterval(()=>{if(!document.hidden&&auth.authenticated&&!$('#login-dialog').open)refreshState();},2000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&auth.authenticated&&!$('#login-dialog').open)refreshState();});

// Announcements are public and remain available when optional tools are disabled.
let announcementsLoading=false;
async function loadAnnouncements(){
  if(announcementsLoading)return;
  announcementsLoading=true;$('#announcements-refresh').disabled=true;
  try{
    const data=await api('/api/announcements'),items=data.announcements;
    const list=$('#announcements-list');list.replaceChildren();
    for(const item of items){
      const article=document.createElement('article');article.className='card announcement';
      const title=document.createElement('h2');title.textContent=item.title;
      const time=document.createElement('time');time.dateTime=new Date(item.updated_at*1000).toISOString();time.textContent=new Date(item.updated_at*1000).toLocaleString('zh-CN');
      const content=document.createElement('p');content.className='announcement-content';content.textContent=item.content;
      article.append(title,time,content);list.append(article);
    }
    $('#announcements-status').textContent=items.length?'':'暂无公告。新消息会在这里发布。';
    $('#announcement-banner').hidden=!items.length;
    $('#announcement-banner-title').textContent=items[0]?.title||'';
  }catch(error){$('#announcements-status').textContent=`公告读取失败，请点击刷新重试。${error.message}`;}
  finally{announcementsLoading=false;$('#announcements-refresh').disabled=false;}
}
$('#announcements-refresh').addEventListener('click',loadAnnouncements);
loadAnnouncements();
setInterval(()=>{if(!document.hidden)loadAnnouncements();},60000);

// Keep the navigation preference on this device.
function setSidebarCollapsed(collapsed){
  document.body.classList.toggle('sidebar-collapsed',collapsed);
  const button=$('#sidebar-toggle');button.setAttribute('aria-expanded',String(!collapsed));button.setAttribute('aria-label',collapsed?'展开菜单':'折叠菜单');button.title=collapsed?'展开菜单':'折叠菜单';
  try{localStorage.setItem('omini-sidebar-collapsed',String(collapsed));}catch(_){}
}
let sidebarCollapsed=false;try{sidebarCollapsed=localStorage.getItem('omini-sidebar-collapsed')==='true';}catch(_){}
setSidebarCollapsed(sidebarCollapsed);
$('#sidebar-toggle').addEventListener('click',()=>setSidebarCollapsed(!document.body.classList.contains('sidebar-collapsed')));
$$('#sidebar-menu .nav-item, .sidebar-bottom .nav-item').forEach(item=>{item.title=item.textContent.trim();if(!item.hasAttribute('aria-label'))item.setAttribute('aria-label',item.textContent.trim());});
