'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const paths = {
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
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.file}"/></svg>`;
function icons(root = document) { $$('[data-icon]', root).forEach(el => { el.outerHTML = icon(el.dataset.icon); }); }
icons();
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let tasks = [], currentType = 'video', currentFilter = 'all', selectedWeek = 1;
let timetable = null, savedTimetable = null, firstMonday = '', demo = false, connected = false;
let toastTimer, polling = false;
const terms = {3:'第一学期',12:'第二学期',16:'第三学期'};
const statuses = {queued:'等待中',running:'下载中',completed:'已完成',paused:'已暂停',failed:'失败',cancelled:'已取消'};
const typeNames = {video:'视频',audio:'音频',file:'文件',gallery:'图库',m3u8:'M3U8',torrent:'磁力链接',subtitle:'字幕',spotify:'Spotify 音频'};
const typeIcons = {video:'play',audio:'music',file:'file',gallery:'image',m3u8:'play',torrent:'layers',subtitle:'file',spotify:'music'};
const schoolLogin = 'https://sso.hualixy.edu.cn/heer_cas/#/login?service=https%3A%2F%2Fjw.hualixy.edu.cn%2Fsso%2Fddlogin';
function toast(message, error = false) {
  const el = $('#toast'); el.textContent = message; el.classList.toggle('error', error); el.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; }, 5000);
}
function showDialog(id) { const el = $(`#${id}`); if (!el.open) el.showModal(); }
async function api(path, body) {
  const options = {headers:{'X-Omini-Client':'web'}, credentials:'same-origin'};
  if (body !== undefined) { options.method='POST'; options.headers['Content-Type']='application/json'; options.body=JSON.stringify(body); }
  const response = await fetch(path, options);
  const value = await response.json();
  if (response.status === 401 && path !== '/api/login') showDialog('login-dialog');
  if (!response.ok) throw new Error(value.error || '请求失败，请重试。');
  return value;
}
function setConnection(ok) {
  connected = ok;
  $('#connection').classList.toggle('offline', !ok);
  $('#connection').innerHTML = `<i></i>${ok ? '服务已连接' : '服务未连接'}`;
  $('#submit-download').disabled = !ok;
}
function route() {
  const page = location.hash === '#timetable' ? 'timetable' : 'downloads';
  $$('.page').forEach(el => { el.hidden = el.id !== `page-${page}`; });
  $$('.nav-item[data-page]').forEach(el => el.classList.toggle('active', el.dataset.page === page));
  $('#breadcrumb-page').textContent = page === 'timetable' ? '学校课表' : '下载中心';
  document.title = `${page === 'timetable' ? '学校课表' : '下载中心'} · Omini`;
}
window.addEventListener('hashchange', route); route();
const now = new Date();
$('#today-date').textContent = now.toLocaleDateString('zh-CN', {month:'long',day:'numeric',weekday:'long'});
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $('#theme-label').textContent = theme === 'dark' ? '切换浅色模式' : '切换深色模式';
  try { localStorage.setItem('omini-theme', theme); } catch (_) {}
}
let theme = 'light';
try { theme = localStorage.getItem('omini-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); } catch (_) {}
setTheme(theme);
$('#theme-toggle').addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
$('#mobile-theme').addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
$('#mobile-settings').addEventListener('click', () => showDialog('settings-dialog'));
$$('[data-close]').forEach(el => el.addEventListener('click', () => $(`#${el.dataset.close}`).close()));
$$('dialog').forEach(el => el.addEventListener('click', e => {
  if (el.id === 'login-dialog') return;
  const r = el.getBoundingClientRect();
  if (e.target === el && (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom)) el.close();
}));
$('#login-dialog').addEventListener('cancel', e => e.preventDefault());
$('#login-form').addEventListener('submit', async e => {
  e.preventDefault(); const button = $('button', e.target); button.disabled = true;
  try { await api('/api/login', {password:$('#access-password').value}); $('#access-password').value=''; $('#login-error').textContent=''; $('#login-dialog').close(); await loadInitial(); }
  catch (error) { $('#login-error').textContent=error.message; }
  finally { button.disabled=false; }
});
function setType(type) {
  currentType=type;
  $$('.type-tab').forEach(el => el.classList.toggle('selected', el.dataset.type === type));
  $('#more-type').value = ['m3u8','torrent','subtitle','spotify'].includes(type) ? type : '';
  $('#quality-field').hidden = type !== 'video';
  $('#format-field').hidden = type !== 'audio';
  $('#filename-field').hidden = !['file','m3u8'].includes(type);
  $('#language-field').hidden = type !== 'subtitle';
  $('#source').placeholder = type === 'subtitle' ? '粘贴视频链接，或输入字幕搜索关键词…' : type === 'torrent' ? '粘贴 magnet 磁力链接或 Torrent 文件直链…' : '粘贴视频、文件链接或分享文本…';
  $('#type-hint').textContent = type === 'spotify' ? '由 spotDL 处理，输出格式由引擎选择。' : type === 'm3u8' ? '下载 HLS 分片，由 FFmpeg 合并为 MP4。' : '支持公开链接；需要登录的内容请使用桌面版。';
}
$$('.type-tab').forEach(el => el.addEventListener('click', () => setType(el.dataset.type)));
$('#more-type').addEventListener('change', e => { if (e.target.value) setType(e.target.value); });
$('#download-form').addEventListener('submit', async e => {
  e.preventDefault(); const button = $('#submit-download'); button.disabled=true;
  try {
    await api('/api/tasks', {type:currentType, source:$('#source').value, options:{quality:$('#quality').value, format:$('#audio-format').value, filename:['file','m3u8'].includes(currentType) ? $('#filename').value : '', language:$('#language').value,metadata:true}});
    $('#source').value=''; toast('任务已加入队列'); await refreshTasks();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled=!connected; }
});
function size(bytes) { if (bytes < 1024) return `${bytes} B`; const i=Math.min(3,Math.floor(Math.log(bytes)/Math.log(1024))); return `${(bytes/1024**i).toFixed(1)} ${['B','KB','MB','GB'][i]}`; }
function renderTasks() {
  const active = tasks.filter(t => ['running','queued'].includes(t.status)).length;
  $('#stat-active').textContent=active; $('#stat-completed').textContent=tasks.filter(t => t.status==='completed').length;
  $('#stat-total').textContent=tasks.length; $('#queue-count').textContent=tasks.length; $('#nav-count').textContent=active;
  const query = $('#task-search').value.toLowerCase();
  const list = tasks.filter(t => (currentFilter==='all' || currentFilter==='active' && ['running','queued'].includes(t.status) || currentFilter===t.status) && `${t.title} ${t.source}`.toLowerCase().includes(query));
  if (!list.length) {
    $('#task-list').innerHTML = `<div class="empty-state"><span class="empty-icon">${icon('download')}</span><h3>${tasks.length ? '这里暂时没有匹配的任务' : '下一份精彩，从这里开始'}</h3><p>${tasks.length ? '试试其他筛选条件或搜索关键词。' : '粘贴一个链接，开始你的第一次下载。'}</p></div>`;
    return;
  }
  $('#task-list').innerHTML = list.map(t => {
    const progress = Math.max(0,Math.min(100,Number(t.progress)||0));
    const buttons = [];
    if (['running','queued'].includes(t.status)) buttons.push(['pause','暂停'],['cancel','取消']);
    if (['paused','failed','cancelled'].includes(t.status)) buttons.push([t.status==='failed'?'retry':'resume',t.status==='failed'?'重试':'继续']);
    buttons.push(['files','文件'],['log','日志']);
    return `<article class="task-row"><span class="task-type-icon">${icon(typeIcons[t.type])}</span><div class="task-info"><div class="task-title-line"><span class="task-title" title="${esc(t.title)}">${esc(t.title)}</span><span class="task-status ${esc(t.status)}">${esc(statuses[t.status]||t.status)}</span></div><div class="task-source" title="${esc(t.source)}">${esc(t.source)}</div><div class="progress-track"><div class="progress-fill" style="width:${progress}%"></div></div><div class="task-meta"><span>${progress.toFixed(0)}%</span><span>${esc(typeNames[t.type])}</span>${t.status==='running'?`<span>${esc(t.speed)} · ${esc(t.eta)}</span>`:''}<div class="task-actions">${buttons.map(([action,label])=>`<button class="text-button" data-task="${esc(t.id)}" data-action="${action}">${label}</button>`).join('')}</div></div>${t.error?`<p class="micro form-error">${esc(t.error)}</p>`:''}</div></article>`;
  }).join('');
}
$('#task-list').addEventListener('click', async e => {
  const button=e.target.closest('[data-action]'); if (!button) return;
  const {task:id, action}=button.dataset; button.disabled=true;
  try {
    if (['files','log'].includes(action)) {
      const result = await api(`/api/tasks/${id}/${action}`);
      $('#detail-title').textContent=action==='files'?'下载文件':'任务日志';
      $('#detail-content').innerHTML=action==='files' ? `<p class="muted">${result.complete?'点击文件，保存到当前设备。':'任务尚未完成，下方列出目前生成的文件。'}</p>${result.files.length?result.files.map(f=>`<a class="file-link" href="${esc(f.url)}" download><span>${esc(f.name)}</span><small>${size(f.bytes)} ↓</small></a>`).join(''):'<div class="empty-state"><p>暂时没有可取回的文件。</p></div>'}` : `<pre class="log-view">${esc(result.log||'暂无日志。')}</pre>`;
      showDialog('detail-dialog');
    } else { await api(`/api/tasks/${id}/${action}`, {}); await refreshTasks(); }
  } catch (error) { toast(error.message,true); }
  finally { button.disabled=false; }
});
$$('[data-filter]').forEach(el=>el.addEventListener('click',()=> { currentFilter=el.dataset.filter; $$('[data-filter]').forEach(b=>b.classList.toggle('active',b===el)); renderTasks(); }));
$('#task-search').addEventListener('input',renderTasks);
async function refreshTasks() {
  if (polling) return;
  polling=true;
  try {
    const state=await api('/api/state'); tasks=state.tasks; setConnection(true); renderTasks();
    $('#download-dir').textContent=state.download_dir; $('#download-dir').title=state.download_dir;
    $('#settings-dir').textContent=state.download_dir; $('#version-label').textContent=state.version;
    if (!$('#settings-dialog').open) $('#concurrency').value=state.concurrency;
    $('#engine-list').innerHTML=Object.entries(state.engines).map(([name,ready])=>`<div class="engine-row"><span>${esc(name)}</span><span class="engine-status ${ready?'':'missing'}"><i></i>${ready?'就绪':'未安装'}</span></div>`).join('');
  } catch (error) { setConnection(false); }
  finally { polling=false; }
}
$('#refresh-tasks').addEventListener('click',async()=>{await refreshTasks();toast(connected?'队列已刷新':'连接失败，请确认 Web 服务正在运行。',!connected);});
$('#settings-open').addEventListener('click',()=>showDialog('settings-dialog'));
$('#settings-form').addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/settings',{concurrency:Number($('#concurrency').value)});$('#settings-dialog').close();toast('偏好设置已保存');}catch(error){toast(error.message,true);}});

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
  savedTimetable=data.schedule; timetable=data.schedule; firstMonday=data.first_monday||''; demo=false;
  $('#demo-toggle').textContent='预览示例课表'; $('#first-monday').value=firstMonday;
  const current=currentWeek(); selectedWeek=current&&current>0&&current<=maxWeeks()?current:1;
  updateWeekOptions();renderTimetable();
}
function splitPeriods(periods) {
  const sorted=[...new Set(periods)].sort((a,b)=>a-b), chunks=[];
  for(const p of sorted){if(chunks.length&&chunks.at(-1).end===p-1)chunks.at(-1).end=p;else chunks.push({start:p,end:p});}return chunks;
}
function renderTimetable() {
  const courses=timetable?.courses||[], current=currentWeek();
  $('#semester-label').textContent=timetable?`${timetable.year} — ${Number(timetable.year)+1} 学年 · ${terms[timetable.term]||''}${demo?' · 示例预览':''}`:'导入你的课表，开启新学期。';
  $('#schedule-notice').textContent=demo?'正在预览示例数据，不会覆盖你的课表。':firstMonday?`第一周周一：${firstMonday}${current&&current>0&&current<=maxWeeks()?` · 当前为第 ${current} 周`: ' · 当前日期不在所设学期内'}`:'尚未设置开学日期，可按周次查看；设置后即可定位本周和今日课程。';
  $('#schedule-updated').textContent=timetable?demo?'示例数据，仅供预览':`更新于 ${timetable.updated_at?.replace('T',' ')||'导入时'}`:'尚未导入课表';
  $('#export-schedule').disabled=!timetable||demo;
  const weekday=(new Date().getDay()+6)%7+1;
  const today=!demo&&current&&current>0 ? courses.filter(c=>c.weekday===weekday&&c.weeks.includes(current)).sort((a,b)=>a.periods[0]-b.periods[0]) : [];
  $('#today-count').textContent=today.length;
  $('#today-courses').innerHTML=today.length?today.map(c=>`<div class="today-course">${esc(c.name)}<small>第 ${esc(c.periods.join('、'))} 节 · ${esc(c.location||'教室待定')}</small></div>`).join(''):`<p class="today-empty">${!timetable?'先导入课表，今天的安排就会出现在这里。':demo?'示例课表仅用于预览周视图。':!firstMonday?'设置第一周日期，即可查看今日课程。':'今天没有课程安排，留点时间给自己。'}</p>`;
  if(!timetable){$('#schedule-grid').innerHTML=`<div class="schedule-empty"><span class="empty-icon">${icon('calendar')}</span><h3>属于你的新学期，还差一张课表</h3><p>导入学校 HAR 或课表 JSON，课程会自动整理到周视图中。</p><button class="button primary" id="empty-import">${icon('upload')}导入我的课表</button></div>`;$('#empty-import').addEventListener('click',()=>showDialog('import-dialog'));return;}
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
$('#week-select').addEventListener('change',e=>{selectedWeek=Number(e.target.value);renderTimetable();});
$('#prev-week').addEventListener('click',()=>{selectedWeek=Math.max(1,selectedWeek-1);$('#week-select').value=selectedWeek;renderTimetable();});
$('#next-week').addEventListener('click',()=>{selectedWeek=Math.min(maxWeeks(),selectedWeek+1);$('#week-select').value=selectedWeek;renderTimetable();});
$('#current-week').addEventListener('click',()=>{if(!firstMonday){showDialog('semester-dialog');return;}const current=currentWeek();if(current<1||current>maxWeeks()){toast('当前日期不在所设学期内，请检查开学日期。',true);return;}selectedWeek=current;$('#week-select').value=current;renderTimetable();});
$('#semester-open').addEventListener('click',()=>showDialog('semester-dialog'));
$('#semester-form').addEventListener('submit',async e=>{e.preventDefault();if(demo){toast('请先退出示例预览，再设置自己的学期。',true);return;}try{acceptTimetable(await api('/api/timetable/semester',{first_monday:$('#first-monday').value}));$('#semester-dialog').close();toast('学期日期已保存');}catch(error){toast(error.message,true);}});
$('#import-open').addEventListener('click',()=>showDialog('import-dialog'));
$('#import-file').addEventListener('change',async e=>{
  const input=e.target,file=input.files[0];if(!file)return;
  if(file.size>24*1024*1024){toast('请选择小于 24 MB 的文件。',true);input.value='';return;}
  input.disabled=true;$('#import-status').textContent='正在解析课表…';
  try{const data=await api('/api/timetable/import',{content:await file.text()});acceptTimetable(data);$('#import-dialog').close();toast(`已导入 ${data.schedule.courses.length} 条课程安排`);}
  catch(error){$('#import-status').textContent=error.message;toast(error.message,true);}
  finally{input.disabled=false;input.value='';}
});
function openSchool(){window.open(schoolLogin,'_blank','noopener,noreferrer');}
$('#school-login').addEventListener('click',openSchool);$('#online-login').addEventListener('click',openSchool);
$('#online-open').addEventListener('click',()=>{$('#school-year').value=timetable?.year||new Date().getFullYear();$('#school-term').value=timetable?.term||'3';showDialog('online-dialog');});
$('#online-dialog').addEventListener('close',()=>{$('#school-cookie').value='';});
$('#online-form').addEventListener('submit',async e=>{
  e.preventDefault();const button=$('#online-submit');button.disabled=true;button.textContent='正在读取…';
  try{const data=await api('/api/timetable/refresh',{cookie:$('#school-cookie').value,year:$('#school-year').value,term:$('#school-term').value});acceptTimetable(data);$('#online-dialog').close();toast('课表已更新');}
  catch(error){toast(error.message,true);}
  finally{button.disabled=false;button.textContent='读取课表';$('#school-cookie').value='';}
});
$('#export-schedule').addEventListener('click',()=>{if(!timetable||demo)return;const url=URL.createObjectURL(new Blob([JSON.stringify(timetable,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=`Omini-课表-${timetable.year}-${timetable.term}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('课表 JSON 已导出');});
$('#demo-toggle').addEventListener('click',()=>{
  if(demo){timetable=savedTimetable;demo=false;$('#demo-toggle').textContent='预览示例课表';updateWeekOptions();renderTimetable();return;}
  demo=true;
  const rows=[['设计思维与表达','林老师','创意楼 A201',1,1,2],['高等数学','陈老师','教学楼 B302',2,3,4],['大学英语','周老师','教学楼 A405',3,1,2],['数据结构','李老师','实验楼 C203',4,3,4],['数字媒体设计','张老师','创意楼 A102',5,1,3],['体育与健康','王老师','综合运动场',1,7,8],['程序设计实践','刘老师','计算机实验室',3,5,7],['大学英语','周老师','教学楼 A405',5,5,6],['设计思维与表达','林老师','创意楼 A201',2,9,10]];
  timetable={schema:1,year:String(new Date().getFullYear()),term:'3',updated_at:'示例',period_times:{},courses:rows.map(([name,teacher,location,weekday,start,end])=>({name,teacher,location,weekday,periods:Array.from({length:end-start+1},(_,i)=>start+i),weeks:Array.from({length:16},(_,i)=>i+1),week_text:'1-16周 · 示例',campus:'示例校区',credits:'',class_name:''}))};
  selectedWeek=1;$('#demo-toggle').textContent='退出示例预览';updateWeekOptions();renderTimetable();toast('正在预览示例课表，你的数据保持原样');
});
async function loadInitial(){await refreshTasks();if(connected){try{acceptTimetable(await api('/api/timetable'));}catch(error){toast(error.message,true);}}}
renderTasks();updateWeekOptions();renderTimetable();loadInitial();
setInterval(()=>{if(!document.hidden&&!$('#login-dialog').open)refreshTasks();},2000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!$('#login-dialog').open)refreshTasks();});
