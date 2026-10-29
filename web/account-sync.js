'use strict';
let cloudResources = {}, cloudPending = {}, cloudBusy = false, cloudReady = false;
let cloudOwner = '', cloudDraftLoaded = false;
let cloudConflicts = [];
let cloudDraftSaving = 0;
const cloudClone = value => JSON.parse(JSON.stringify(value));

// Replay only edits made after a request started, on top of its acknowledged result.
// Timetables and individual OAuth members stay atomic, as on the server.
function rebaseCloudData(base, latest, result, path=[]) {
  if(JSON.stringify(base)===JSON.stringify(latest))return result;
  const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
  const atomic=path.length===1&&path[0]==='schedule'||path.length===2&&path[0]==='members'||path.length===3&&path[0]==='pool'&&path[1]==='api_members';
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
  $('#settings-sync-status').textContent=text;
  $('#settings-sync-status').classList.toggle('form-error',error);
}
function resetAccountSync() {
  cloudResources={};cloudPending={};cloudReady=false;cloudOwner='';cloudDraftLoaded=false;
  cloudConflicts=[];$('#cloud-conflicts').textContent='';
  cloudStatus('登录邮箱账号后自动同步。');
}
function cloudData(name) { return cloudClone(cloudPending[name]?.data||cloudResources[name]?.data||{}); }
async function queueCloudData(name,data) {
  if(!['preferences','schedule'].includes(name))throw new Error('同步资源有误。');
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
      if(!valid())return;cloudPending=Object.fromEntries(Object.entries(draft).filter(([name])=>['preferences','schedule'].includes(name)));cloudDraftLoaded=true;
    }
    const known=Object.fromEntries(Object.entries(cloudResources).map(([name,value])=>[name,value.revision]));
    const remote=await api('/api/sync?resources=preferences,schedule&revisions='+encodeURIComponent(JSON.stringify(known)));if(!valid())return;
    for(const [name,value] of Object.entries(remote.resources))if(['preferences','schedule'].includes(name)&&!value.unchanged)cloudResources[name]=value;
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
    cloudStatus(Object.keys(cloudPending).length?'本机新编辑已加密保存，正在继续同步…':`已同步 · ${new Date().toLocaleTimeString()}${cloudConflicts.length?' · 有并发修改备份':''}`);
    completed=true;
    loadAccountSync.last=Date.now();
  }catch(error){if(valid())cloudStatus(`同步待重试 · ${error.message}；当前修改已保留。`,true);}
  finally{cloudBusy=false;if(completed&&valid()&&Object.keys(cloudPending).length)setTimeout(()=>loadAccountSync(true),250);}
}
function renderCloudWorkspace() {
  $('#cloud-conflicts').innerHTML=cloudConflicts.filter(c=>['schedule','preferences'].includes(c.name)).slice(0,20).map(c=>`<div><button class="text-button" data-cloud-backup="${esc(c.id)}">下载 ${esc({schedule:'课表',preferences:'设置'}[c.name])} 冲突备份 · ${esc(new Date(c.created_at*1000).toLocaleString())}</button> <button class="button compact" data-cloud-restore="${esc(c.id)}">恢复冲突字段</button></div>`).join('');
}
$('#cloud-conflicts').addEventListener('click',async event=>{
  const button=event.target.closest('[data-cloud-backup],[data-cloud-restore]');if(!button)return;
  const generation=authGeneration;
  try{const value=await api(`/api/sync/conflicts/${button.dataset.cloudBackup||button.dataset.cloudRestore}`);if(generation!==authGeneration)return;
    if(!['preferences','schedule'].includes(value.name))throw new Error('此备份请在电脑客户端查看。');
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
