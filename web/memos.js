'use strict';
let memos=[], memoCurrent=null, memoDirty=false, memoTimer, memoBusy=false, memoTrash=false, memoLoading=false, memoLoaded=false;
const memoAssets=new Map();
const memoEl=id=>document.getElementById(id);
const memoStatus=text=>{memoEl('memo-status').textContent=text;};
function memoDraftKey(){return `omini-memo-draft:${auth.user?.id||'guest'}`;}
function persistMemoDraft(){
  if(!memoCurrent||!memoDirty)return;
  try{localStorage.setItem(memoDraftKey(),JSON.stringify({...memoCurrent,title:memoEl('memo-title').value,content:memoEl('memo-content').value}));}catch(_){}
}
function resetMemos(){
  persistMemoDraft();clearTimeout(memoTimer);memos=[];memoCurrent=null;memoDirty=false;memoLoaded=false;memoAssets.clear();renderMemoList();showMemo(null);
}
function renderMemoList(){
  const root=memoEl('memo-list');root.replaceChildren();const query=memoEl('memo-search').value.toLowerCase();
  const visible=memos.filter(m=>Boolean(m.deleted)===memoTrash&&`${m.title}\n${m.content}`.toLowerCase().includes(query));
  visible.forEach(m=>{
    const b=document.createElement('button');b.className='memo-item'+(memoCurrent?.id===m.id?' selected':'');
    const title=document.createElement('strong');title.textContent=m.title;b.append(title);
    const detail=document.createElement('small');detail.textContent=`v${m.version} · ${new Date(m.updated*1000).toLocaleString('zh-CN')}`;b.append(detail);
    b.addEventListener('click',async()=>{if(memoDirty&&!await saveMemo())return;showMemo(m);});root.append(b);
  });
  if(!visible.length){const p=document.createElement('p');p.className='muted';p.textContent=memoTrash?'回收站为空':'还没有备忘录';root.append(p);}
}
function showMemo(m){
  memoCurrent=m?{...m}:null;memoDirty=false;
  memoEl('memo-title').value=m?.title||'';memoEl('memo-content').value=m?.content||'';
  for(const id of ['memo-title','memo-content','memo-save','memo-delete'])memoEl(id).disabled=!m||Boolean(m.deleted);
  memoEl('memo-image').disabled=!m||Boolean(m.deleted);memoEl('memo-history').disabled=!m;
  memoEl('memo-restore').hidden=!m?.deleted;memoEl('memo-save-copy').hidden=true;
  memoStatus(m?`已同步 · v${m.version} · ${new Date(m.updated*1000).toLocaleString('zh-CN')}`:'选择或新建备忘录。');
  renderMemoList();renderMemoPreview();
}
async function loadMemos(force=false){
  if(!auth.user||!features.notes||memoLoading||memoBusy||memoDirty||memoLoaded&&!force)return;
  memoLoading=true;const generation=authGeneration;
  try{
    const data=await api('/api/memos');if(generation!==authGeneration)return;memos=data.memos;memoLoaded=true;
    let draft=null;try{draft=JSON.parse(localStorage.getItem(memoDraftKey())||'null');}catch(_){}
    if(draft&&!memoCurrent){
      const server=memos.find(m=>m.id===draft.id);showMemo(server||draft);
      memoEl('memo-title').value=draft.title;memoEl('memo-content').value=draft.content;memoDirty=true;
      if(server&&server.version!==draft.version){memoCurrent.version=draft.version;memoEl('memo-save-copy').hidden=false;memoStatus('已恢复本机未保存编辑，服务器已有新版本。可保存为副本。');}
      else{memoStatus('已恢复上次未保存编辑，请点击保存。');}
      renderMemoPreview();
    }else if(memoCurrent){const current=memos.find(m=>m.id===memoCurrent.id);if(current&&current.version!==memoCurrent.version)showMemo(current);else renderMemoList();}
    else{renderMemoList();if(force&&memos.length)showMemo(memos.find(m=>!m.deleted)||null);}
  }catch(e){if(generation===authGeneration)memoStatus(e.message);}finally{memoLoading=false;}
}
function editedMemo(){
  if(!memoCurrent)return;memoDirty=true;persistMemoDraft();clearTimeout(memoTimer);
  memoStatus('正在编辑，稍后自动保存…');memoTimer=setTimeout(saveMemo,900);renderMemoPreview();
}
async function saveMemo(){
  if(!memoCurrent||!memoDirty)return true;if(memoBusy)return false;
  clearTimeout(memoTimer);memoBusy=true;const generation=authGeneration;
  const current={...memoCurrent,title:memoEl('memo-title').value,content:memoEl('memo-content').value};
  try{
    const result=await api('/api/memos',{id:current.id,title:current.title,content:current.content,deleted:Boolean(current.deleted),base_version:current.version});
    if(generation!==authGeneration)return false;
    memoCurrent=result.memo;memos=[result.memo,...memos.filter(m=>m.id!==result.memo.id)];
    if(memoEl('memo-title').value===current.title&&memoEl('memo-content').value===current.content){memoDirty=false;try{localStorage.removeItem(memoDraftKey());}catch(_){}}
    else{persistMemoDraft();memoTimer=setTimeout(saveMemo,900);}
    memoStatus(`已同步 · v${result.memo.version} · ${new Date().toLocaleTimeString('zh-CN')}`);renderMemoList();return !memoDirty;
  }catch(e){
    if(generation===authGeneration){persistMemoDraft();memoStatus(e.status===409?'服务器已有更新；你的编辑仍保留，请保存冲突副本。':`${e.message} 编辑已暂存在本机，请稍后保存。`);memoEl('memo-save-copy').hidden=e.status!==409;}
    return false;
  }finally{memoBusy=false;}
}
async function memoAsset(id){
  if(!memoAssets.has(id)){const a=await api(`/api/memos/assets/${id}`);memoAssets.set(id,`data:${a.mime};base64,${a.data}`);}
  return memoAssets.get(id);
}
async function renderMemoPreview(){
  const root=memoEl('memo-preview');root.replaceChildren();const content=memoEl('memo-content').value;
  const regex=/!\[([^\]\n]*)\]\(omini-asset:\/\/([a-f0-9]{64})\)/g;let start=0,match;
  const text=value=>{const span=document.createElement('span');span.textContent=value;root.append(span);};
  while((match=regex.exec(content))){
    text(content.slice(start,match.index));start=regex.lastIndex;
    const img=document.createElement('img');img.alt=match[1]||'备忘录图片';root.append(img);
    const generation=authGeneration;
    memoAsset(match[2]).then(src=>{if(generation===authGeneration&&img.isConnected)img.src=src;}).catch(()=>{img.alt='图片暂未加载，请稍后重试';});
  }
  text(content.slice(start));
}
memoEl('memo-search').addEventListener('input',renderMemoList);
memoEl('memo-active').addEventListener('click',()=>{memoTrash=false;renderMemoList();});
memoEl('memo-trash').addEventListener('click',()=>{memoTrash=true;renderMemoList();});
memoEl('memo-title').addEventListener('input',editedMemo);memoEl('memo-content').addEventListener('input',editedMemo);
memoEl('memo-save').addEventListener('click',saveMemo);
memoEl('memo-new').addEventListener('click',async()=>{
  if(memoDirty&&!await saveMemo())return;
  try{const result=await api('/api/memos',{title:'未命名备忘录',content:'',base_version:0});memos.unshift(result.memo);memoTrash=false;showMemo(result.memo);memoEl('memo-title').focus();memoEl('memo-title').select();}catch(e){memoStatus(e.message);}
});
memoEl('memo-save-copy').addEventListener('click',async()=>{
  if(!memoCurrent)return;
  try{const result=await api('/api/memos',{title:(memoEl('memo-title').value+'（冲突副本）').slice(0,240),content:memoEl('memo-content').value,base_version:0});try{localStorage.removeItem(memoDraftKey());}catch(_){}memos.unshift(result.memo);showMemo(result.memo);memoStatus('冲突编辑已保存为独立备忘录，原记录继续保留。');}catch(e){memoStatus(e.message);}
});
async function changeMemoDeleted(deleted){
  if(!memoCurrent)return;if(memoDirty&&!await saveMemo())return;
  if(deleted&&!confirm('移到回收站？内容和全部历史版本仍可恢复。'))return;
  try{const result=await api('/api/memos',{...memoCurrent,deleted,base_version:memoCurrent.version});memos=memos.map(m=>m.id===result.memo.id?result.memo:m);showMemo(result.memo);}catch(e){memoStatus(e.message);}
}
memoEl('memo-delete').addEventListener('click',()=>changeMemoDeleted(true));memoEl('memo-restore').addEventListener('click',()=>changeMemoDeleted(false));
async function insertMemoImage(file){
  if(!memoCurrent||memoCurrent.deleted||!file)return;if(file.size>10*1024*1024){memoStatus('图片最大为 10 MB。');return;}
  const reader=new FileReader();reader.onload=async()=>{
    const memoId=memoCurrent?.id,generation=authGeneration;
    try{const asset=await api('/api/memos/assets',{name:file.name||'粘贴图片.png',data:String(reader.result).split(',')[1]});
      if(generation!==authGeneration||memoId!==memoCurrent?.id)return;
      const area=memoEl('memo-content');area.setRangeText(`\n![${asset.name.replace(/[\]\n]/g,'_')}](omini-asset://${asset.id})\n`,area.selectionStart,area.selectionEnd,'end');editedMemo();
    }catch(e){memoStatus(e.message);}
  };reader.readAsDataURL(file);
}
memoEl('memo-image').addEventListener('change',e=>{insertMemoImage(e.target.files[0]);e.target.value='';});
memoEl('memo-content').addEventListener('paste',e=>{const file=[...(e.clipboardData?.files||[])].find(f=>f.type.startsWith('image/'));if(file){e.preventDefault();insertMemoImage(file);}});
memoEl('memo-history').addEventListener('click',async()=>{
  if(!memoCurrent)return;if(memoDirty&&!await saveMemo())return;
  const id=memoCurrent.id;
  try{const result=await api(`/api/memos/${id}/history`);const root=memoEl('memo-versions');root.replaceChildren();
    const labels={create:'新建',edit:'编辑',delete:'删除',restore:'恢复',rollback:'回滚'};
    for(const v of result.versions){const box=document.createElement('article');box.className='memo-version';const heading=document.createElement('strong');heading.textContent=`v${v.version} · ${labels[v.action]||v.action} · ${v.device} · ${new Date(v.created*1000).toLocaleString('zh-CN')}`;box.append(heading);const pre=document.createElement('pre');pre.textContent=v.title+'\n'+v.content;box.append(pre);const button=document.createElement('button');button.className='button compact';button.textContent='回滚到此版本';button.addEventListener('click',async()=>{
      if(memoCurrent?.id!==id||!confirm('创建一个恢复此内容的新版本？当前版本仍保留。'))return;
      try{const restored=await api(`/api/memos/${id}/restore`,{version:v.version,base_version:memoCurrent.version});memos=memos.map(m=>m.id===id?restored.memo:m);showMemo(restored.memo);memoEl('memo-history-dialog').close();}catch(e){toast(e.message,true);}
    });box.append(button);root.append(box);}showDialog('memo-history-dialog');
  }catch(e){memoStatus(e.message);}
});
window.addEventListener('beforeunload',e=>{if(memoDirty){persistMemoDraft();e.preventDefault();e.returnValue='';}});
setInterval(()=>{if(!document.hidden&&location.hash==='#notes'&&auth.user)loadMemos(true);},15000);
