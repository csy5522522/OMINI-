'use strict';
const el=id=>document.getElementById(id);let adminUser;
async function request(path,body){const options={credentials:'same-origin',headers:{'X-Omini-Client':'web'}};if(body!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}const response=await fetch(path,options);const data=await response.json();if(!response.ok)throw new Error(data.error||'请求失败');return data;}
async function refresh(){
  try{const session=await request('/api/auth/session');adminUser=session.user;if(adminUser?.role!=='admin')throw new Error('请使用管理员账号登录。');
    const data=await request('/api/admin/users');el('admin-login').hidden=true;el('admin-panel').hidden=false;el('admin-status').textContent=`${data.users.length} 个账号 · ${data.version}`;
    el('admin-users').replaceChildren();for(const user of data.users){const tr=document.createElement('tr');for(const text of [user.email,new Date(user.created_at*1000).toLocaleString('zh-CN'),`${user.role==='admin'?'管理员':'用户'} / ${user.disabled?'已停用':'正常'}`]){const td=document.createElement('td');td.textContent=text;tr.append(td);}const td=document.createElement('td');
      const reset=document.createElement('button');reset.className='button compact';reset.textContent='重置密码';reset.onclick=()=>resetPassword(user.id);td.append(reset);
      if(user.role!=='admin'){const toggle=document.createElement('button');toggle.className='button compact';toggle.textContent=user.disabled?'启用':'停用';toggle.onclick=async()=>{if(!confirm(`${toggle.textContent}账号 ${user.email}？现有会话会退出。`))return;try{await request('/api/admin/user',{id:user.id,disabled:!user.disabled});await refresh();}catch(e){el('admin-status').textContent=e.message;}};td.append(toggle);}tr.append(td);el('admin-users').append(tr);}
  }catch(e){el('admin-status').textContent=e.message;el('admin-login').hidden=false;el('admin-panel').hidden=true;}
}
async function resetPassword(id){const password=prompt('输入新密码（8 至 128 个字符）；保存后此账号所有设备需重新登录。');if(password===null)return;try{await request('/api/admin/user',{id,password});el('admin-status').textContent='密码已重置，原有会话已退出。';if(id===adminUser?.id)await refresh();}catch(e){el('admin-status').textContent=e.message;}}
el('admin-login').onsubmit=async e=>{e.preventDefault();try{await request('/api/auth/login',{email:el('admin-name').value,password:el('admin-password').value});el('admin-password').value='';await refresh();}catch(error){el('admin-status').textContent=error.message;}};
el('admin-reload').onclick=refresh;el('admin-change').onclick=()=>resetPassword(adminUser.id);el('admin-logout').onclick=async()=>{await request('/api/auth/logout',{});await refresh();};refresh();
