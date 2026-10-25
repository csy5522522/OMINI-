'use strict';
let memos=[],memoCurrent=null,memoDirty=false,memoTimer,memoBusy=false,memoTrash=false,memoLoading=false,memoLoaded=false,memoComposing=false,memoOperation=0;
let memoImagePoint=null;
const memoAssets=new Map(),memoEl=id=>document.getElementById(id);
const memoEditor=OminiMemoEditor.create(memoEl('memo-content'),{
  loadAsset:memoAsset,onChange:editedMemo,onImage:insertMemoImage,
  onComposition(value){memoComposing=value;if(value)clearTimeout(memoTimer);else if(memoDirty)memoTimer=setTimeout(saveMemo,900);}
});
function memoStatus(text,state='saved'){
  memoEl('memo-status').textContent=text;memoEl('memo-status').title=text;
  document.querySelector('.memo-main').dataset.state=state;
}
function memoNotice(text='',conflict=false,retry=false){
  memoEl('memo-notice').hidden=!text;memoEl('memo-notice-text').textContent=text;
  memoEl('memo-save-copy').hidden=!conflict;memoEl('memo-retry').hidden=!retry;
}
function memoDraftKey(){return `omini-memo-draft:${auth.user?.id||'guest'}`;}
let memoDraftQueue=Promise.resolve(),memoDraftSaving=0;
function persistMemoDraft(value) {
  const key=memoDraftKey();
  const snapshot=arguments.length?value:(!memoCurrent||!memoDirty?null:{...memoCurrent,title:memoEl('memo-title').value,content:memoEditor.getContent()});
  if(!arguments.length&&!snapshot)return memoDraftQueue;
  memoDraftSaving++;
  memoDraftQueue=memoDraftQueue.catch(()=>{}).then(()=>cloudDraftStore.save(key,snapshot));
  memoDraftQueue.catch(()=>memoNotice('加密草稿保存失败，请保持页面打开并重试保存。',false,true)).finally(()=>memoDraftSaving--);
  return memoDraftQueue;
}
async function readMemoDraft() {
  await memoDraftQueue.catch(()=>{});
  const key=memoDraftKey();let legacy=null;
  try{legacy=JSON.parse(localStorage.getItem(key)||'null');}catch(_){}
  if(legacy){await cloudDraftStore.save(key,legacy);localStorage.removeItem(key);return legacy;}
  return cloudDraftStore.load(key);
}
window.addEventListener('beforeunload',event=>{if(memoDraftSaving){event.preventDefault();event.returnValue='';}});
function closeMemoMenu(){memoEl('memo-menu').hidden=true;memoEl('memo-more').setAttribute('aria-expanded','false');}
function resetMemos(){
  persistMemoDraft();clearTimeout(memoTimer);memoOperation++;memoBusy=false;memoLoading=false;
  memos=[];memoCurrent=null;memoDirty=false;memoLoaded=false;memoComposing=false;memoTrash=false;memoAssets.clear();
  memoEl('memo-search').value='';memoEl('memo-workbench').classList.remove('show-document');showMemo(null,false);
}
function memoSnippet(content){return content.replace(/!\[([^\]\n]*)\]\(omini-asset:\/\/[a-f0-9]{64}\)/g,' [图片] ').replace(/\s+/g,' ').trim();}
function memoDate(timestamp){
  const date=new Date(timestamp*1000),today=new Date();
  return date.toDateString()===today.toDateString()?`今天 ${date.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}`:date.toLocaleDateString('zh-CN',{month:'short',day:'numeric'});
}
function updateMemoRefresh(){memoEl('memo-refresh').disabled=memoLoading||memoBusy;}
function renderMemoAccess(){
  const signedIn=Boolean(auth.user);
  memoEl('memo-search').disabled=!signedIn;
  memoEl('memo-empty-title').textContent=signedIn?'让想法在这里停留':'登录后，找回你的想法';
  memoEl('memo-empty-description').textContent=signedIn?'当前账号暂无已同步的备忘录。请确认客户端已完成同步，且使用相同的服务地址与邮箱账号。':'可以先浏览工具箱。需要查看客户端已同步的备忘录时，再选择登录。';
  memoEl('memo-empty-new').textContent=signedIn?'新建备忘录':'邮箱登录 / 注册';
  memoEl('memo-new').title=memoEl('memo-new').ariaLabel=signedIn?'新建备忘录':'登录后新建备忘录';
  updateMemoRefresh();
  if(!memoCurrent)memoStatus(signedIn?'当前账号暂无已同步的备忘录':'登录后查看已同步的备忘录');
}
function renderMemoList(){
  const root=memoEl('memo-list'),scroll=root.scrollTop;root.replaceChildren();
  const query=memoEl('memo-search').value.toLowerCase().trim();
  memoEl('memo-count').textContent=memos.filter(m=>!m.deleted).length;
  memoEl('memo-trash-count').textContent=memos.filter(m=>m.deleted).length;
  for(const [id,selected] of [['memo-active',!memoTrash],['memo-trash',memoTrash]]){
    memoEl(id).classList.toggle('selected',selected);memoEl(id).setAttribute('aria-pressed',String(selected));
  }
  const visible=memos.filter(m=>Boolean(m.deleted)===memoTrash&&`${m.title}\n${m.content}`.toLowerCase().includes(query));
  if(!visible.length){
    const empty=document.createElement('p');empty.className='memo-list-empty';
    empty.textContent=!auth.user?'登录后显示你的备忘录':query?'没有匹配的备忘录':memoTrash?'回收站为空':'暂无已同步的备忘录';
    root.append(empty);
  }
  for(const memo of visible){
    const button=document.createElement('button');button.className='memo-item'+(memoCurrent?.id===memo.id?' selected':'');button.setAttribute('aria-current',String(memoCurrent?.id===memo.id));
    const title=document.createElement('strong');title.textContent=memo.title;
    const snippet=document.createElement('span');snippet.className='memo-snippet';snippet.textContent=memoSnippet(memo.content)||'空白备忘录';
    const date=document.createElement('small');date.textContent=memoDate(memo.updated);date.title=new Date(memo.updated*1000).toLocaleString('zh-CN');
    button.append(title,snippet,date);button.addEventListener('click',async()=>{
      if(memoDirty&&!await saveMemo())return;
      showMemo(memos.find(m=>m.id===memo.id)||memo);
    });root.append(button);
  }
  root.scrollTop=scroll;
}
function showMemo(memo,openDocument=true){
  clearTimeout(memoTimer);closeMemoMenu();memoCurrent=memo?{...memo}:null;memoDirty=false;
  memoEl('memo-title').value=memo?.title||'';memoEditor.loadContent(memo?.content||'');memoEditor.setReadOnly(!memo||Boolean(memo.deleted));
  for(const id of ['memo-title','memo-save','memo-delete','memo-image','memo-image-button'])memoEl(id).disabled=!memo||Boolean(memo.deleted);
  memoEl('memo-history').disabled=!memo;memoEl('memo-more').disabled=!memo;
  memoEl('memo-restore').hidden=!memo?.deleted;memoEl('memo-empty').hidden=Boolean(memo);
  document.querySelector('.memo-main').classList.toggle('is-blank',!memo);
  memoEl('memo-document-kind').textContent=memo?.deleted?'RECYCLE BIN':'PERSONAL NOTE';
  memoEl('memo-workbench').classList.toggle('show-document',Boolean(memo)&&openDocument);
  document.querySelector('.memo-document-scroll').scrollTop=0;
  memoStatus(memo?memo.deleted?'回收站 · 内容与历史均已保留':`已同步 · ${memoDate(memo.updated)}`:'选择或新建备忘录');
  memoNotice();renderMemoList();
  renderMemoAccess();
}
async function loadMemos(force=false){
  if(!auth.user||!features.notes||memoLoading||memoBusy||memoDirty||memoComposing||memoLoaded&&!force)return;
  memoLoading=true;const generation=authGeneration;
  renderMemoAccess();if(!memoLoaded)memoStatus('正在读取已同步的备忘录…','saving');
  try{
    const data=await api('/api/memos');if(generation!==authGeneration)return;
    // The user may start typing while the listing request is in flight.
    if(memoDirty||memoBusy||memoComposing)return;
    memos=data.memos;
    let draft=null;try{draft=await readMemoDraft();}catch(_){memoNotice('加密草稿读取失败，请检查浏览器存储。');}
    if(generation!==authGeneration||memoDirty||memoBusy||memoComposing)return;
    if(draft&&typeof draft.content==='string'&&typeof draft.title==='string'&&!memoCurrent){
      const server=memos.find(m=>m.id===draft.id);showMemo({...draft,deleted:false});memoDirty=true;
      if(server&&server.version!==draft.version){memoNotice('已恢复本机草稿，云端已有新版本。请保存冲突副本以保留两份内容。',true);memoStatus('存在版本冲突','error');}
      else{memoNotice('已恢复上次未保存的草稿，可继续编辑或点击重试保存。',false,true);memoStatus('本机草稿待保存','editing');}
    }else if(memoCurrent){
      const current=memos.find(m=>m.id===memoCurrent.id);
      if(current&&current.version!==memoCurrent.version){
        const scroll=document.querySelector('.memo-document-scroll').scrollTop,selection=memoEditor.view.state.selection;
        const wasOpen=memoEl('memo-workbench').classList.contains('show-document');showMemo(current,wasOpen);
        memoEditor.restoreSelection(selection.from,selection.to);document.querySelector('.memo-document-scroll').scrollTop=scroll;
      }else renderMemoList();
    }else{showMemo(memos.find(m=>Boolean(m.deleted)===memoTrash)||null,false);}
    memoLoaded=true;
  }catch(error){if(generation===authGeneration){memoStatus('同步暂未完成','error');memoNotice(error.message);}}
  finally{if(generation===authGeneration){memoLoading=false;updateMemoRefresh();}}
}
function editedMemo(){
  if(!memoCurrent||memoCurrent.deleted)return;
  memoDirty=true;persistMemoDraft();clearTimeout(memoTimer);memoStatus('正在编辑 · 自动保存','editing');
  if(!memoComposing&&!memoEditor.isComposing())memoTimer=setTimeout(saveMemo,900);
}
async function saveMemo(){
  if(memoComposing||memoEditor.isComposing()){clearTimeout(memoTimer);memoTimer=setTimeout(saveMemo,900);return false;}
  if(!memoCurrent||!memoDirty)return true;if(memoBusy)return false;
  clearTimeout(memoTimer);memoBusy=true;updateMemoRefresh();const generation=authGeneration,operation=++memoOperation;
  const current={...memoCurrent,title:memoEl('memo-title').value,content:memoEditor.getContent()};
  memoStatus('正在保存…','saving');
  try{
    const result=await api('/api/memos',{id:current.id,title:current.title,content:current.content,deleted:Boolean(current.deleted),base_version:current.version});
    if(generation!==authGeneration||memoCurrent?.id!==current.id)return false;
    memoCurrent=result.memo;memos=[result.memo,...memos.filter(m=>m.id!==result.memo.id)];
    if(memoEl('memo-title').value===current.title&&memoEditor.getContent()===current.content){
      memoDirty=false;persistMemoDraft(null);memoNotice();memoStatus(`已同步 · ${new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}`);
    }else{persistMemoDraft();memoTimer=setTimeout(saveMemo,900);memoStatus('正在编辑 · 自动保存','editing');}
    renderMemoList();return !memoDirty;
  }catch(error){
    if(generation===authGeneration){persistMemoDraft();memoStatus(error.status===409?'存在版本冲突':'草稿已保留在本机','error');memoNotice(error.status===409?'云端已有更新，你的编辑已保留。请保存冲突副本。':`${error.message} 编辑已暂存在本机，请稍后重试。`,error.status===409,error.status!==409);}
    return false;
  }finally{if(operation===memoOperation){memoBusy=false;updateMemoRefresh();}}
}
async function memoAsset(id){
  if(!memoAssets.has(id)){
    const generation=authGeneration;
    const promise=api(`/api/memos/assets/${id}`).then(asset=>{
      if(generation!==authGeneration)throw new Error('账号已切换');
      if(!['image/png','image/jpeg','image/gif','image/webp'].includes(asset.mime))throw new Error('图片格式有误');
      return `data:${asset.mime};base64,${asset.data}`;
    });memoAssets.set(id,promise);
    promise.catch(()=>{if(memoAssets.get(id)===promise)memoAssets.delete(id);});
  }
  return memoAssets.get(id);
}
async function switchMemoFolder(trash){
  if(memoDirty&&!await saveMemo())return;
  memoTrash=trash;showMemo(null,false);
}
async function newMemo(){
  if(!auth.user){showDialog('login-dialog');return;}
  if(memoDirty&&!await saveMemo())return;
  const generation=authGeneration;
  try{const result=await api('/api/memos',{title:'未命名备忘录',content:'',base_version:0});if(generation!==authGeneration)return;memos.unshift(result.memo);memoTrash=false;memoEl('memo-search').value='';showMemo(result.memo);memoEl('memo-title').focus();memoEl('memo-title').select();}
  catch(error){memoNotice(error.message);memoStatus('新建暂未完成','error');}
}
async function changeMemoDeleted(deleted){
  if(!memoCurrent||memoBusy)return;if(memoDirty&&!await saveMemo())return;
  if(deleted&&!confirm('移到回收站？内容和全部历史版本仍可恢复。'))return;
  const generation=authGeneration,id=memoCurrent.id;
  try{const result=await api('/api/memos',{...memoCurrent,deleted,base_version:memoCurrent.version});if(generation!==authGeneration)return;memos=memos.map(m=>m.id===result.memo.id?result.memo:m);memoTrash=deleted;showMemo(memoCurrent?.id===id?result.memo:memoCurrent);}
  catch(error){memoNotice(error.message);}
}
async function insertMemoImage(file,point){
  if(!memoCurrent||memoCurrent.deleted||!file)return;
  const bookmark=point||memoEditor.bookmark(),memoId=memoCurrent.id,generation=authGeneration;
  try{
    if(file.size>10*1024*1024)throw new Error('图片最大为 10 MB。');
    const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(new Error('图片读取失败，请重新选择。'));reader.readAsDataURL(file);});
    const asset=await api('/api/memos/assets',{name:file.name||'粘贴图片.png',data});
    if(generation!==authGeneration||memoId!==memoCurrent?.id)return;
    memoEditor.insertImage(asset,bookmark);
  }catch(error){if(generation===authGeneration&&memoId===memoCurrent?.id)memoNotice(error.message);}
  finally{memoEditor.releaseBookmark(bookmark);}
}
memoEl('memo-search').addEventListener('input',renderMemoList);
memoEl('memo-active').addEventListener('click',()=>switchMemoFolder(false));memoEl('memo-trash').addEventListener('click',()=>switchMemoFolder(true));
memoEl('memo-title').addEventListener('input',editedMemo);
memoEl('memo-title').addEventListener('compositionstart',()=>{memoComposing=true;clearTimeout(memoTimer);});
memoEl('memo-title').addEventListener('compositionend',()=>{memoComposing=false;if(memoDirty)memoTimer=setTimeout(saveMemo,900);});
for(const id of ['memo-save','memo-retry'])memoEl(id).addEventListener('click',()=>{closeMemoMenu();saveMemo();});
for(const id of ['memo-new','memo-empty-new'])memoEl(id).addEventListener('click',newMemo);
memoEl('memo-refresh').addEventListener('click',async()=>{
  if(!auth.user){showDialog('login-dialog');return;}
  if(memoDirty&&!await saveMemo())return;
  await loadMemos(true);
});
memoEl('memo-delete').addEventListener('click',()=>{closeMemoMenu();changeMemoDeleted(true);});memoEl('memo-restore').addEventListener('click',()=>changeMemoDeleted(false));
memoEl('memo-save-copy').addEventListener('click',async()=>{
  if(!memoCurrent||memoBusy)return;clearTimeout(memoTimer);const generation=authGeneration,id=memoCurrent.id;
  const originalTitle=memoEl('memo-title').value,title=(originalTitle+'（冲突副本）').slice(0,240),content=memoEditor.getContent();
  memoBusy=true;updateMemoRefresh();const operation=++memoOperation;
  try{
    const result=await api('/api/memos',{title,content,base_version:0});if(generation!==authGeneration)return;
    memos.unshift(result.memo);
    if(memoCurrent?.id===id&&memoEditor.getContent()===content&&memoEl('memo-title').value===originalTitle){persistMemoDraft(null);showMemo(result.memo);memoStatus('冲突副本已保存');}
    else{persistMemoDraft();memoNotice('已保存一份冲突副本。刚刚继续输入的编辑仍保留在当前草稿。',true);renderMemoList();}
  }catch(error){if(generation===authGeneration)memoNotice(error.message,true);}
  finally{if(operation===memoOperation){memoBusy=false;updateMemoRefresh();}}
});
memoEl('memo-image-button').addEventListener('click',()=>{if(memoImagePoint)memoEditor.releaseBookmark(memoImagePoint);memoImagePoint=memoEditor.bookmark();memoEl('memo-image').click();});
memoEl('memo-image').addEventListener('change',event=>{insertMemoImage(event.target.files[0],memoImagePoint);memoImagePoint=null;event.target.value='';});
memoEl('memo-image').addEventListener('cancel',()=>{if(memoImagePoint)memoEditor.releaseBookmark(memoImagePoint);memoImagePoint=null;});
memoEl('memo-more').addEventListener('click',()=>{const opening=memoEl('memo-menu').hidden;memoEl('memo-menu').hidden=!opening;memoEl('memo-more').setAttribute('aria-expanded',String(opening));if(opening)memoEl('memo-menu').querySelector('button:not(:disabled)')?.focus();});
document.addEventListener('click',event=>{if(!event.target.closest('.memo-more-wrap'))closeMemoMenu();});
memoEl('memo-menu').addEventListener('keydown',event=>{
  const items=[...memoEl('memo-menu').querySelectorAll('button:not(:disabled)')],index=items.indexOf(document.activeElement);
  if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?items.length-1:(index+(event.key==='ArrowDown'?1:-1)+items.length)%items.length;items[next]?.focus();}
});
document.addEventListener('keydown',event=>{
  if(event.key==='Escape'&&!memoEl('memo-menu').hidden){closeMemoMenu();memoEl('memo-more').focus();}
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='s'&&location.hash==='#notes'){event.preventDefault();saveMemo();}
});
memoEl('memo-back').addEventListener('click',()=>memoEl('memo-workbench').classList.remove('show-document'));
memoEl('memo-history').addEventListener('click',async()=>{
  closeMemoMenu();if(!memoCurrent)return;if(memoDirty&&!await saveMemo())return;
  const id=memoCurrent.id,generation=authGeneration;
  try{
    const result=await api(`/api/memos/${id}/history`);if(generation!==authGeneration||memoCurrent?.id!==id)return;
    const root=memoEl('memo-versions');root.replaceChildren();
    const labels={create:'新建',edit:'编辑',delete:'删除',restore:'恢复',rollback:'回滚',rename:'重命名',sync:'同步'};
    for(const version of result.versions){
      const box=document.createElement('article');box.className='memo-version';
      const heading=document.createElement('strong');heading.textContent=`v${version.version} · ${labels[version.action]||version.action} · ${version.device} · ${new Date(version.created*1000).toLocaleString('zh-CN')}`;
      const pre=document.createElement('pre');pre.textContent=version.title+'\n'+version.content;
      const button=document.createElement('button');button.className='button compact';button.textContent='回滚到此版本';
      button.addEventListener('click',async()=>{
        if(memoCurrent?.id!==id||!confirm('创建一个恢复此内容的新版本？当前版本仍保留。'))return;
        try{const restored=await api(`/api/memos/${id}/restore`,{version:version.version,base_version:memoCurrent.version});if(generation!==authGeneration)return;memos=memos.map(m=>m.id===id?restored.memo:m);memoTrash=Boolean(restored.memo.deleted);showMemo(restored.memo);memoEl('memo-history-dialog').close();}catch(error){toast(error.message,true);}
      });box.append(heading,pre,button);root.append(box);
    }showDialog('memo-history-dialog');
  }catch(error){if(generation===authGeneration)memoNotice(error.message);}
});
window.addEventListener('beforeunload',event=>{if(memoDirty){persistMemoDraft();event.preventDefault();event.returnValue='';}});
setInterval(()=>{if(!document.hidden&&location.hash==='#notes'&&auth.user)loadMemos(true);},15000);
showMemo(null,false);
