'use strict';
let cloudResources = {}, cloudPending = {}, cloudBusy = false, cloudReady = false;
let cloudOwner = '', cloudMemberSignature = '', cloudDraftLoaded = false;
let cloudConflicts = [];
let cloudDraftSaving = 0;
const cloudClone = value => JSON.parse(JSON.stringify(value));

// Replay only edits made after a request started, on top of its acknowledged result.
// Timetables and individual OAuth members stay atomic, as on the server.
function rebaseCloudData(base, latest, result, path=[]) {
  if(JSON.stringify(base)===JSON.stringify(latest))return result;
  const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
  const atomic=path.length===1&&path[0]==='schedule'||path.length===2&&path[0]==='members';
  if(!atomic&&object(latest)&&object(result)&&(base===undefined||object(base))){
    const rebased={};
    for(const key of new Set([...Object.keys(base||{}),...Object.keys(latest),...Object.keys(result)])){
      const value=rebaseCloudData(base?.[key],latest[key],result[key],[...path,key]);
      if(value!==undefined)rebased[key]=value;
    }
    return rebased;
  }
  return latest;
}

let cloudDraftQueue=Promise.resolve();
function persistCloudDraft(owner=cloudOwner,value=cloudPending) {
  const snapshot=cloudClone(value);
  cloudDraftQueue=cloudDraftQueue.catch(()=>{}).then(()=>cloudDraftStore.save(owner,snapshot));
  return cloudDraftQueue;
}
function cloudStatus(text,error=false) {
  $('#cloud-status').textContent=text;$('#settings-sync-status').textContent=text;
  $('#cloud-status').classList.toggle('form-error',error);
}
function resetAccountSync() {
  cloudResources={};cloudPending={};cloudReady=false;cloudOwner='';cloudDraftLoaded=false;
  cloudMemberSignature='';cloudConflicts=[];
  $('#cloud-members').textContent='';$('#cloud-profiles').textContent='';$('#cloud-history').textContent='';$('#cloud-conflicts').textContent='';
  $('#cloud-model').value='';$('#cloud-import').value='';
  cloudStatus('登录邮箱账号后自动同步。');
}
function cloudData(name) { return cloudClone(cloudPending[name]?.data||cloudResources[name]?.data||{}); }
async function queueCloudData(name,data) {
  if(!auth.user||!cloudReady)throw new Error('请先登录并完成首次账号同步。');
  const owner=cloudOwner,generation=authGeneration;
  const revision=cloudPending[name]?.revision??cloudResources[name]?.revision??0;
  cloudPending[name]={revision,data:cloudClone(data)};
  cloudDraftSaving++;
  try{await persistCloudDraft();}catch(_){cloudStatus('加密草稿存储未完成，请保持此页面打开并重试同步。',true);}
  finally{cloudDraftSaving--;}
  if(owner!==cloudOwner||generation!==authGeneration)return;
  renderCloudWorkspace();await loadAccountSync(true);
}
async function saveCloudTheme(theme) {
  try {
    if(!auth.user)return;
    if(!cloudReady)await loadAccountSync(true);
    if(cloudReady)await queueCloudData('preferences',{...cloudData('preferences'),theme:theme==='dark'?'深色':'浅色'});
  } catch(error){cloudStatus(error.message,true);}
}
async function loadAccountSync(force=false) {
  if(!auth.user||cloudBusy)return;
  if(!force&&cloudReady&&Date.now()-(loadAccountSync.last||0)<10000)return;
  cloudBusy=true;
  const generation=authGeneration,owner=auth.user.id;
  let completed=false;
  const valid=()=>generation===authGeneration&&auth.user?.id===owner;
  try {
    if(cloudOwner!==owner){cloudResources={};cloudPending={};cloudDraftLoaded=false;cloudOwner=owner;cloudReady=false;}
    if(!cloudDraftLoaded){
      let draft={};try{draft=await cloudDraftStore.load(owner);}catch(_){/* Online sync still works when browser storage is disabled. */}
      if(!valid())return;cloudPending=draft;cloudDraftLoaded=true;
    }
    const known=Object.fromEntries(Object.entries(cloudResources).map(([name,value])=>[name,value.revision]));
    const remote=await api('/api/sync?revisions='+encodeURIComponent(JSON.stringify(known)));if(!valid())return;
    for(const [name,value] of Object.entries(remote.resources))if(!value.unchanged)cloudResources[name]=value;
    cloudConflicts=remote.conflicts||[];cloudReady=true;
    for(const name of Object.keys(cloudPending)){
      const queued=cloudPending[name];
      const result=await api(`/api/sync/${name}`,queued);if(!valid())return;
      cloudResources[name]=result;
      if(cloudPending[name]===queued)delete cloudPending[name];
      else if(cloudPending[name])cloudPending[name]={revision:result.revision,
        data:rebaseCloudData(queued.data,cloudPending[name].data,result.data)};
      if(result.conflict_id)cloudConflicts.unshift({id:result.conflict_id,name,created_at:Date.now()/1000});
    }
    try{await persistCloudDraft(owner,cloudPending);}catch(_){/* Retained drafts are idempotent against their original base version. */}
    if(!valid())return;
    const prefs=cloudData('preferences');
    if(prefs.theme)setTheme(prefs.theme==='深色'?'dark':'light');
    if(prefs.features)applyFeatures(prefs.features);
    const schedule=cloudData('schedule');
    if(features.timetable&&'schedule' in schedule&&!demo&&!$('#semester-dialog').open&&!$('#online-dialog').open&&!$('#import-dialog').open){
      if(JSON.stringify(schedule.schedule)!==JSON.stringify(savedTimetable)||schedule.first_monday!==firstMonday||schedule.selected_week!==selectedWeek)acceptTimetable({...schedule,revision:cloudResources.schedule.revision});
    }
    renderCloudWorkspace();
    cloudStatus(Object.keys(cloudPending).length?'本机新编辑已加密保存，正在继续同步…':`已同步 · ${new Date().toLocaleTimeString()} · ${Object.keys(cloudData('assistants').members||{}).length} 个 Codex 成员${cloudConflicts.length?' · 有并发修改备份':''}`);
    completed=true;
    loadAccountSync.last=Date.now();
  }catch(error){if(valid())cloudStatus(`同步待重试 · ${error.message}；当前修改已保留。`,true);}
  finally{cloudBusy=false;if(completed&&valid()&&Object.keys(cloudPending).length)setTimeout(()=>loadAccountSync(true),250);}
}
function renderCloudWorkspace() {
  const data=cloudData('assistants'),members=data.members||{},pool=data.pool||{};
  const signature=JSON.stringify({members,quotas:data.quotas,rules:pool.member_rules});
  if(signature!==cloudMemberSignature){
    cloudMemberSignature=signature;
    $('#cloud-members').innerHTML=Object.entries(members).map(([name,m])=>{
      const rule=pool.member_rules?.[name]||{},quota=data.quotas?.[name];
      const remaining=key=>Number.isFinite(quota?.[key]?.remaining_percent)?`${quota[key].remaining_percent.toFixed(0)}%`:'待查询';
      const quotaText=quota?`额度快照 · 5 小时 ${remaining('five_hour')} · 每周 ${remaining('weekly')}`:'等待客户端查询额度';
      return `<div class="cloud-member" data-member="${esc(name)}"><span class="avatar">C</span><div class="cloud-member-info"><strong>${esc(m.email||m.label||name)}</strong><small>${m.disabled?'已停用':'已启用'} · ${esc(quotaText)}</small></div><label>优先级<input type="number" data-rule="priority" min="-1000000" max="1000000" value="${Number(rule.priority??m.priority??0)}"></label><label>权重<input type="number" data-rule="weight" min="1" max="1000000" value="${Number(rule.weight??m.weight??1)}"></label><button class="button compact" data-member-toggle>${m.disabled?'启用':'停用'}</button><button class="button compact" data-member-delete>移出</button></div>`;
    }).join('')||'<p class="muted">还没有成员。在此导入凭据，或在客户端添加后同步。</p>';
  }
  if(!$('#cloud-pool-form').contains(document.activeElement)){
    $('#cloud-strategy').value=pool.strategy||'round-robin';$('#cloud-model').value=pool.model||'';$('#cloud-affinity').checked=pool.session_affinity!==false;
  }
  if(!$('#cloud-profiles').contains(document.activeElement)){
    const titles={codex:'Codex',gemini:'Gemini CLI',antigravity:'Antigravity',claude:'Claude Code'};
    $('#cloud-profiles').innerHTML=Object.entries(data.profiles||{}).map(([name,p])=>`<form class="cloud-profile" data-profile="${esc(name)}"><strong>${esc(titles[name]||name)}</strong><select name="mode" aria-label="连接模式"><option value="custom"${p.mode==='custom'?' selected':''}>指定代理</option><option value="direct"${p.mode==='direct'?' selected':''}>直接连接</option><option value="inherit"${p.mode==='inherit'?' selected':''}>继承环境</option></select><input name="proxy" aria-label="代理地址" placeholder="http://127.0.0.1:7890" value="${esc(p.proxy||'')}"><button class="button compact" type="submit">保存</button></form>`).join('')||'<p class="muted">在客户端配置助手后，这里会显示同步的连接设置。</p>';
  }
  $('#cloud-history').textContent=Object.values(data.history||{}).sort((a,b)=>String(a.time).localeCompare(String(b.time))).slice(-200).map(r=>`${r.time||''} ${r.message||''}`).join('\n')||'暂无操作记录。';
  $('#cloud-conflicts').innerHTML=cloudConflicts.slice(0,20).map(c=>`<div><button class="text-button" data-cloud-backup="${esc(c.id)}">下载 ${esc({assistants:'助手',schedule:'课表',preferences:'偏好'}[c.name]||c.name)} 冲突备份 · ${esc(new Date(c.created_at*1000).toLocaleString())}</button> <button class="button compact" data-cloud-restore="${esc(c.id)}">恢复冲突字段</button></div>`).join('');
}
$('#cloud-pool-form').addEventListener('submit',async event=>{
  event.preventDefault();
  try{const data=cloudData('assistants');data.pool={...data.pool,strategy:$('#cloud-strategy').value,model:$('#cloud-model').value,session_affinity:$('#cloud-affinity').checked};await queueCloudData('assistants',data);}
  catch(error){cloudStatus(error.message,true);}
});
$('#cloud-profiles').addEventListener('submit',async event=>{
  event.preventDefault();const form=event.target.closest('[data-profile]');if(!form)return;
  try{const data=cloudData('assistants'),name=form.dataset.profile;data.profiles[name]={...data.profiles[name],mode:form.elements.mode.value,proxy:form.elements.proxy.value.trim()};await queueCloudData('assistants',data);}
  catch(error){cloudStatus(error.message,true);}
});
$('#cloud-members').addEventListener('click',async event=>{
  const row=event.target.closest('[data-member]');if(!row)return;
  const toggle=event.target.closest('[data-member-toggle]'),remove=event.target.closest('[data-member-delete]');
  if(!toggle&&!remove)return;
  const data=cloudData('assistants'),name=row.dataset.member;if(!data.members?.[name])return;
  if(remove&&!confirm('从此账号的所有设备移出这个 Codex 成员？已有同步版本与冲突备份会保留。'))return;
  if(remove){delete data.members[name];if(data.pool?.member_rules)delete data.pool.member_rules[name];if(data.quotas)delete data.quotas[name];}
  else data.members[name].disabled=!data.members[name].disabled;
  try{await queueCloudData('assistants',data);}catch(error){cloudStatus(error.message,true);}
});
$('#cloud-members').addEventListener('change',async event=>{
  const input=event.target.closest('[data-rule]'),row=event.target.closest('[data-member]');if(!input||!row)return;
  const value=Number(input.value),key=input.dataset.rule;
  if(!Number.isInteger(value)||value<(key==='weight'?1:-1000000)||value>1000000){cloudStatus('优先级或权重超出范围。',true);return;}
  const data=cloudData('assistants');data.pool??={};data.pool.member_rules??={};data.pool.member_rules[row.dataset.member]??={priority:0,weight:1};data.pool.member_rules[row.dataset.member][key]=value;
  try{await queueCloudData('assistants',data);}catch(error){cloudStatus(error.message,true);}
});
async function normalizeCloudCredential(value) {
  const tokens=value?.tokens||value;
  if(!tokens||value.type&&value.type!=='codex'||!['access_token','refresh_token'].every(k=>typeof tokens[k]==='string'&&tokens[k].trim()))throw new Error('请选择包含访问与刷新令牌的 Codex JSON。');
  const normalized={type:'codex',disabled:Boolean(value.disabled)};
  for(const key of ['access_token','refresh_token','id_token','account_id'])if(key in tokens)normalized[key]=tokens[key];
  for(const key of ['email','expired','last_refresh','proxy_url'])if(key in value)normalized[key]=value[key];
  try{const part=(tokens.id_token||tokens.access_token).split('.')[1].replace(/-/g,'+').replace(/_/g,'/');const claims=JSON.parse(atob(part));if(!normalized.email&&claims.email)normalized.email=claims.email;}catch(_){}
  const identity=normalized.account_id||normalized.email||normalized.refresh_token;
  const hash=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(identity));
  const name='codex-'+[...new Uint8Array(hash)].map(x=>x.toString(16).padStart(2,'0')).join('').slice(0,20)+'.json';
  return [name,normalized];
}
$('#cloud-import').addEventListener('change',async event=>{
  const files=[...event.target.files],generation=authGeneration;event.target.value='';
  try{
    if(files.length>20)throw new Error('每次最多导入 20 个成员文件。');
    if(!cloudReady)throw new Error('请先完成首次同步。');
    const data=cloudData('assistants');data.members??={};
    for(const file of files){if(file.size>2*1024*1024)throw new Error('成员文件最多 2 MB。');const [name,value]=await normalizeCloudCredential(JSON.parse(await file.text()));if(generation!==authGeneration)return;data.members[name]=value;}
    await queueCloudData('assistants',data);
  }catch(error){if(generation===authGeneration)cloudStatus(error.message,true);}
});
$('#cloud-conflicts').addEventListener('click',async event=>{
  const button=event.target.closest('[data-cloud-backup],[data-cloud-restore]');if(!button)return;
  const generation=authGeneration;
  try{const value=await api(`/api/sync/conflicts/${button.dataset.cloudBackup||button.dataset.cloudRestore}`);if(generation!==authGeneration)return;
    if(button.dataset.cloudRestore){
      if(!confirm('恢复备份中的冲突字段并同步到所有设备？其他字段保持当前内容。'))return;
      await loadAccountSync(true);if(generation!==authGeneration)return;
      let restored=cloudData(value.name);
      for(const path of value.paths){
        if(path==='document'){restored=cloudClone(value.incoming);break;}
        const keys=path.split('/');let source=value.incoming,target=restored;
        for(const key of keys.slice(0,-1)){source=source[key]||{};target[key]??={};target=target[key];}
        const key=keys.at(-1);if(key in source)target[key]=cloudClone(source[key]);else delete target[key];
      }
      await queueCloudData(value.name,restored);return;
    }
    const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=`Omini-${value.name}-conflict.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  catch(error){if(generation===authGeneration)cloudStatus(error.message,true);}
});
setInterval(()=>{if(!document.hidden&&auth.user)loadAccountSync();},15000);
window.addEventListener('beforeunload',event=>{if(cloudDraftSaving){event.preventDefault();event.returnValue='';}});
