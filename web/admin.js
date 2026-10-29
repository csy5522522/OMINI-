'use strict';
const el=id=>document.getElementById(id);let adminUser;
if(!window.OminiAppearance){try{document.documentElement.dataset.theme=localStorage.getItem('omini-theme')||'dark';}catch(_){document.documentElement.dataset.theme='dark';}}
async function request(path,body){const options={credentials:'same-origin',headers:{'X-Omini-Client':'web'}};if(body!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}const response=await fetch(path,options);const data=await response.json();if(!response.ok)throw new Error(data.error||'请求失败');return data;}
async function refresh(){
  try{const session=await request('/api/auth/session');adminUser=session.user;if(adminUser?.role!=='admin')throw new Error('请使用管理员账号登录。');
    const data=await request('/api/admin/users');await loadAdminAnnouncements();await loadConsole();el('admin-login').hidden=true;el('admin-panel').hidden=false;el('admin-status').textContent=`${data.users.length} 个账号 · ${data.version}`;
    el('admin-users').replaceChildren();for(const user of data.users){const tr=document.createElement('tr');for(const text of [user.email,new Date(user.created_at*1000).toLocaleString('zh-CN'),`${user.role==='admin'?'管理员':'用户'} / ${user.disabled?'已停用':'正常'}`]){const td=document.createElement('td');td.textContent=text;tr.append(td);}const td=document.createElement('td');
      if(user.role!=='admin'){const edit=document.createElement('button');edit.className='button compact';edit.textContent='编辑邮箱';edit.disabled=user.email.endsWith('@hualixy.school.omini.invalid');edit.onclick=()=>editUser(user);td.append(edit);const remove=document.createElement('button');remove.className='button compact danger';remove.textContent='删除';remove.onclick=()=>deleteUser(user);td.append(remove);}
      const reset=document.createElement('button');reset.className='button compact';reset.textContent='重置密码';reset.onclick=()=>resetPassword(user.id);td.append(reset);
      if(user.role!=='admin'){const toggle=document.createElement('button');toggle.className='button compact';toggle.textContent=user.disabled?'启用':'停用';toggle.onclick=async()=>{if(!confirm(`${toggle.textContent}账号 ${user.email}？现有会话会退出。`))return;try{await request('/api/admin/user',{id:user.id,disabled:!user.disabled});await refresh();}catch(e){el('admin-status').textContent=e.message;}};td.append(toggle);}tr.append(td);el('admin-users').append(tr);}
    filterUsers();
  }catch(e){el('admin-status').textContent=e.message;el('admin-login').hidden=false;el('admin-panel').hidden=true;}
}
async function resetPassword(id){const password=prompt('输入新密码（8 至 128 个字符）；保存后此账号所有设备需重新登录。');if(password===null)return;try{await request('/api/admin/user',{id,password});el('admin-status').textContent='密码已重置，原有会话已退出。';if(id===adminUser?.id)await refresh();}catch(e){el('admin-status').textContent=e.message;}}
el('admin-login').onsubmit=async e=>{e.preventDefault();try{await request('/api/auth/login',{email:el('admin-name').value,password:el('admin-password').value});el('admin-password').value='';await refresh();}catch(error){el('admin-status').textContent=error.message;}};
el('admin-reload').onclick=refresh;el('admin-change').onclick=()=>resetPassword(adminUser.id);el('admin-logout').onclick=async()=>{await request('/api/auth/logout',{});await refresh();};

function clearAnnouncementForm(){
  for(const id of ['announcement-id','announcement-version','announcement-title','announcement-content'])el(id).value='';
  el('announcement-save').textContent='发布公告';el('announcement-cancel').hidden=true;
}
async function loadAdminAnnouncements(){
  const data=await request('/api/admin/announcements');
  const list=el('admin-announcements-list');list.replaceChildren();
  for(const item of data.announcements){
    const article=document.createElement('article');article.className='announcement';
    const title=document.createElement('h3');title.textContent=item.title;
    const time=document.createElement('time');time.textContent=new Date(item.updated_at*1000).toLocaleString('zh-CN');
    const content=document.createElement('p');content.className='announcement-content';content.textContent=item.content;
    const actions=document.createElement('div');actions.className='announcement-actions';
    const edit=document.createElement('button');edit.className='button compact';edit.textContent='编辑';edit.onclick=()=>{
      el('announcement-id').value=item.id;el('announcement-version').value=item.version;el('announcement-title').value=item.title;el('announcement-content').value=item.content;
      el('announcement-save').textContent='保存修改';el('announcement-cancel').hidden=false;el('announcement-status').textContent='正在编辑公告';
      el('announcement-title').focus();el('announcement-form').scrollIntoView({block:'center',behavior:'smooth'});
    };
    const remove=document.createElement('button');remove.className='button compact';remove.textContent='删除';remove.onclick=async()=>{
      if(!confirm(`删除公告“${item.title}”？`))return;
      remove.disabled=true;
      try{await request('/api/admin/announcements',{action:'delete',id:item.id,version:item.version});
        if(el('announcement-id').value===item.id)clearAnnouncementForm();
        await loadAdminAnnouncements();el('announcement-status').textContent='公告已删除。';
      }catch(error){el('announcement-status').textContent=error.message;}
      finally{remove.disabled=false;}
    };
    actions.append(edit,remove);article.append(title,time,content,actions);list.append(article);
  }
  if(!data.announcements.length){const empty=document.createElement('p');empty.className='muted';empty.textContent='暂无公告，填写上方表单即可发布。';list.append(empty);}
}
el('announcement-form').onsubmit=async event=>{
  event.preventDefault();const button=el('announcement-save');button.disabled=true;
  const id=el('announcement-id').value;
  try{await request('/api/admin/announcements',{...(id?{id,version:Number(el('announcement-version').value)}:{}),title:el('announcement-title').value,content:el('announcement-content').value});
    clearAnnouncementForm();await loadAdminAnnouncements();el('announcement-status').textContent=id?'公告已更新。':'公告已发布。';
  }catch(error){el('announcement-status').textContent=error.message;}
  finally{button.disabled=false;}
};
el('announcement-cancel').onclick=()=>{clearAnnouncementForm();el('announcement-status').textContent='';};
el('announcement-reload').onclick=async()=>{try{await loadAdminAnnouncements();el('announcement-status').textContent='公告列表已刷新。';}catch(error){el('announcement-status').textContent=error.message;}};
// Management forms are installed before the initial session refresh.
el('admin-panel').insertAdjacentHTML('afterbegin', `
  <nav class="admin-nav" aria-label="后台导航"><a class="button" href="#admin-overview">总览表盘</a><a class="button" href="#admin-chat">模型配置</a><a class="button" href="#announcement-management-title">公告管理</a><a class="button" href="#user-tools">用户管理</a></nav>
  <section id="admin-overview"><h2>总览表盘</h2><p class="muted">当前服务的真实数据 · 点击刷新账号同步更新</p><div id="overview-cards" class="overview-grid"></div></section>
  <section id="admin-chat" class="console-section"><div class="model-section-heading"><div><span class="eyebrow">MODEL LIBRARY</span><h2>模型配置</h2><p class="muted">每个模型，独立配置。集中管理你的 AI 能力。</p></div><button class="button primary" id="model-add" type="button">＋ 添加模型</button></div>
  <div id="provider-list" class="provider-list model-grid"></div>
  <dialog id="model-dialog" class="dialog model-dialog"><div class="dialog-heading"><h2 id="model-dialog-title">添加模型</h2><button id="model-close" type="button" class="button compact">关闭</button></div><p class="muted">API Key 加密保存，编辑时留空沿用原 Key。</p><form id="provider-form" class="console-form"><input id="provider-id" type="hidden">
  <div class="console-fields"><label>模型名称<input id="provider-label" maxlength="120" required placeholder="例如：日常助理"></label>
  <label>API 协议<select id="provider-protocol"><option value="openai">OpenAI Chat Completions / 兼容接口</option><option value="responses">OpenAI Responses</option><option value="anthropic">Anthropic Messages</option></select></label>
  <label>API 基础地址<input id="provider-base" type="url" required placeholder="https://api.openai.com/v1"></label>
  <label>API Key<input id="provider-key" type="password" autocomplete="new-password" placeholder="填写密钥；编辑时留空保留"></label></div>
  <label for="provider-models">模型 ID</label><input id="provider-models" maxlength="256" required placeholder="获取模型 ID 或手动填写"><label for="provider-alias">显示 ID（可选）</label><input id="provider-alias" maxlength="256" placeholder="留空使用上游模型 ID">
  <label class="console-checkbox"><input id="provider-disabled" type="checkbox">停用此模型</label>
  <div class="announcement-actions"><button class="button" id="provider-discover" type="button">获取模型 ID</button><button class="button primary" type="submit">保存模型</button><button class="button" id="provider-clear" type="button">取消</button></div>
  <p id="provider-status" role="status"></p><div id="provider-discovered" class="model-picks"></div></form></dialog><p id="model-library-status" role="status"></p></section>`);
el('admin-users').closest('.admin-table').insertAdjacentHTML('beforebegin', `
  <div id="user-tools" class="user-tools"><label for="user-search">查找用户</label><input id="user-search" type="search" placeholder="邮箱 / 角色 / 状态"><button id="user-create" class="button primary" type="button">新增用户</button></div>
  <dialog id="user-dialog" class="dialog narrow"><form id="user-form"><div class="dialog-heading"><h2>新增用户</h2><button class="button compact" type="button" id="user-cancel">关闭</button></div><label for="user-email">邮箱</label><input id="user-email" type="email" required maxlength="254"><label for="user-password">初始密码</label><input id="user-password" type="password" minlength="8" maxlength="128" autocomplete="new-password" required><p id="user-status" role="status"></p><button class="button primary" type="submit">创建用户</button></form></dialog>`);
let providers=[],editingAlias=null;
el('admin-panel').prepend(el('admin-panel').querySelector('.memo-toolbar'));
async function loadConsole(){
  const [overview,data]=await Promise.all([request('/api/admin/overview'),request('/api/admin/chat/providers')]);
  el('overview-cards').replaceChildren();
  const stats=[['账号总数',overview.users],['已登录账号',overview.active_users],['近 7 天注册',overview.new_users],['停用账号',overview.disabled],['有效备忘录',overview.memos],['可选聊天模型',overview.models],['今日对话请求',overview.today_requests],['总请求 / 成功',`${overview.requests} / ${overview.successes}`]];
  for(const [label,value] of stats){const card=document.createElement('div');card.className='card overview-card';const name=document.createElement('span');name.textContent=label;const number=document.createElement('strong');number.textContent=value;card.append(name,number);el('overview-cards').append(card);}
  providers=data.providers;el('provider-list').replaceChildren();
  for(const provider of providers)for(const model of provider.models){
    const card=document.createElement('article');card.className='model-card'+(provider.disabled?' disabled':'');
    const head=document.createElement('div');head.className='model-card-head';const monogram=document.createElement('span');monogram.className='model-monogram';monogram.textContent=provider.protocol==='anthropic'?'A':'O';const badge=document.createElement('span');badge.className='model-state';badge.textContent=provider.disabled?'已停用':'已启用';head.append(monogram,badge);
    const name=document.createElement('h3');name.textContent=provider.models.length>1?model.alias:provider.label;
    const id=document.createElement('code');id.className='model-id';id.textContent=model.name;
    const meta=document.createElement('div');meta.className='model-meta';for(const text of [({openai:'Chat Completions',responses:'Responses',anthropic:'Anthropic'})[provider.protocol],new URL(provider.base_url).host,provider.has_key?'密钥已配置':'待配置密钥']){const span=document.createElement('span');span.textContent=text;meta.append(span);}
    const actions=document.createElement('div');actions.className='model-card-actions';
    const edit=document.createElement('button');edit.className='button compact';edit.textContent='编辑模型';edit.onclick=()=>fillProvider(provider,model);
    const toggle=document.createElement('button');toggle.className='button compact';toggle.textContent=provider.disabled?'启用':'停用';toggle.onclick=async()=>{toggle.disabled=true;try{await request('/api/admin/chat/providers',{...provider,model_alias:model.alias,models:[model],disabled:!provider.disabled});await loadConsole();}catch(error){el('model-library-status').textContent=error.message;}finally{toggle.disabled=false;}};
    const remove=document.createElement('button');remove.className='button compact danger';remove.textContent='删除';remove.onclick=async()=>{if(!confirm(`删除模型“${model.alias}”？`))return;try{await request('/api/admin/chat/providers',{action:'delete_model',id:provider.id,model_alias:model.alias});await loadConsole();}catch(error){el('model-library-status').textContent=error.message;}};
    actions.append(edit,toggle,remove);card.append(head,name,id,meta,actions);el('provider-list').append(card);
  }
  if(!providers.length){const empty=document.createElement('div');empty.className='models-empty';const h=document.createElement('h3');h.textContent='连接你的第一个模型';const p=document.createElement('p');p.textContent='添加 API 地址与密钥，让对话拥有更多可能。';empty.append(h,p);el('provider-list').append(empty);}
  filterUsers();
}
function providerValue(){const original=providers.find(p=>p.id===el('provider-id').value);return {id:el('provider-id').value,label:el('provider-label').value,protocol:el('provider-protocol').value,base_url:el('provider-base').value,api_key:el('provider-key').value,disabled:el('provider-disabled').checked,proxy:original?.proxy||'',headers:original?.headers||{},models:el('provider-models').value.trim()?[{name:el('provider-models').value.trim(),alias:el('provider-alias').value.trim()||el('provider-models').value.trim()}]:[],...(editingAlias!==null?{model_alias:editingAlias}:{})};}
function resetProvider(){editingAlias=null;el('provider-form').reset();el('provider-id').value='';el('provider-key').placeholder='填写密钥';el('provider-status').textContent='';el('provider-discovered').replaceChildren();}
function clearProvider(){resetProvider();el('model-dialog').close();}
function fillProvider(p,m){resetProvider();editingAlias=m.alias;el('model-dialog-title').textContent='编辑模型';for(const [key,value] of Object.entries({id:p.id,label:p.models.length>1?m.alias:p.label,protocol:p.protocol,base:p.base_url,models:m.name,alias:m.alias===m.name?'':m.alias}))el('provider-'+key).value=value;el('provider-disabled').checked=p.disabled;el('provider-key').placeholder='已保存 Key，留空保留';el('model-dialog').showModal();el('provider-label').focus();}
el('model-add').onclick=()=>{resetProvider();el('model-dialog-title').textContent='添加模型';el('model-dialog').showModal();};el('model-close').onclick=clearProvider;
el('provider-clear').onclick=clearProvider;
el('provider-form').onsubmit=async event=>{event.preventDefault();const buttons=el('provider-form').querySelectorAll('button');buttons.forEach(b=>b.disabled=true);try{await request('/api/admin/chat/providers',providerValue());clearProvider();await loadConsole();el('model-library-status').textContent='模型已保存，前端刷新模型后可选。';}catch(error){el('provider-status').textContent=error.message;}finally{buttons.forEach(b=>b.disabled=false);}};
el('provider-discover').onclick=async()=>{const button=el('provider-discover');button.disabled=true;el('provider-status').textContent='正在获取模型 ID…';el('provider-discovered').replaceChildren();try{const result=await request('/api/admin/chat/models',providerValue());el('provider-status').textContent=result.message;if(result.ok){for(const id of result.models){const pick=document.createElement('button');pick.type='button';pick.className='button compact';pick.textContent=id;pick.onclick=()=>{el('provider-models').value=id;el('provider-alias').value='';};el('provider-discovered').append(pick);}}}catch(error){el('provider-status').textContent=error.message;}finally{button.disabled=false;}};
function filterUsers(){const text=el('user-search').value.trim().toLowerCase();for(const row of el('admin-users').children)row.hidden=!row.textContent.toLowerCase().includes(text);}
el('user-search').oninput=filterUsers;
el('user-create').onclick=()=>{el('user-form').reset();el('user-status').textContent='';el('user-dialog').showModal();};
el('user-cancel').onclick=()=>el('user-dialog').close();
el('user-form').onsubmit=async event=>{event.preventDefault();const button=el('user-form').querySelector('[type=submit]');button.disabled=true;try{await request('/api/admin/users',{email:el('user-email').value,password:el('user-password').value});el('user-password').value='';el('user-dialog').close();await refresh();}catch(error){el('user-status').textContent=error.message;}finally{button.disabled=false;}};
async function editUser(user){const email=prompt('修改邮箱，保存后该账号需重新登录。',user.email);if(email===null||email===user.email)return;try{await request('/api/admin/user',{id:user.id,email});await refresh();}catch(error){el('admin-status').textContent=error.message;}}
async function deleteUser(user){if(!confirm(`永久删除 ${user.email} 及其备忘录、同步数据和会话？此操作不可撤销。`))return;try{await request('/api/admin/user',{id:user.id,action:'delete'});await refresh();}catch(error){el('admin-status').textContent=error.message;}}
refresh();
