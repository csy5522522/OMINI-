'use strict';
(() => {
  const byId=id=>document.getElementById(id);
  byId('settings-dialog').append(byId('admin-settings-entry').content.cloneNode(true));
  byId('settings-admin').hidden=auth.user?.role!=='admin';
  let messages=[], attachments=[], owner=null, generation=0, busy=false, loading=false, reading=false;
  let conversationId=null, version=0, runId=null, controller=null, stopping=false,cancelRequest=Promise.resolve();
  const attachmentMemory=new Map();
  const drawer=document.createElement('dialog');drawer.id='chat-history-dialog';drawer.className='dialog chat-history-dialog';
  drawer.innerHTML='<div class="dialog-heading"><h2>历史对话</h2><button type="button" class="button compact" id="chat-history-close">关闭</button></div><p id="chat-history-status" role="status"></p><div id="chat-history-list"></div>';
  document.body.append(drawer);
  const historyButton=document.createElement('button');historyButton.id='chat-history';historyButton.type='button';historyButton.className='button';historyButton.textContent='历史对话';
  byId('chat-new').before(historyButton);
  byId('chat-history-close').onclick=()=>drawer.close();
  const icons={spark:'m12 3 2.8 6.2L21 12l-6.2 2.8L12 21l-2.8-6.2L3 12l6.2-2.8L12 3Z',file:'M14 2H6v20h12V6l-4-4Zm0 0v5h5M8 12h8m-8 4h6',image:'M3 3h18v18H3V3Zm0 14 6-6 4 4 3-3 5 5M8 7h.01',copy:'M9 7h11v14H9V7ZM5 17H3V3h11v2',check:'m5 12 4 4L19 6'};
  function icon(name){const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',icons[name]||icons.file);svg.append(path);return svg;}
  function status(text='',error=false){const node=byId('chat-status');node.textContent=error?text:'';node.hidden=!error;}
  function remember(){if(conversationId)attachmentMemory.set(conversationId,messages.map(m=>m.attachments||[]));}
  function acceptConversation(value){
    const conversation=value?.conversation||value;if(!conversation?.id)return;
    remember();const cached=attachmentMemory.get(conversation.id)||[];
    conversationId=conversation.id;version=conversation.version;
    messages=conversation.messages.map((message,index)=>({...message,attachments:(message.attachments||[]).map(file=>cached[index]?.find(old=>old.data&&old.name===file.name&&old.size===file.size)||{...file,expired:true})}));
    const heading=document.querySelector('.chat-page-header h1');if(heading)heading.textContent=conversation.title||'ChatGPT 对话';remember();
  }
  async function refreshConversation(id,epoch){const data=await api('/api/chat/conversations/'+encodeURIComponent(id));if(epoch===generation&&conversationId===id)acceptConversation(data);}
  async function history(){
    if(!auth.user){status('请先登录账号后查看历史对话。',true);return;}
    drawer.showModal();byId('chat-history-status').textContent='正在读取…';const epoch=generation;
    try{
      const data=await api('/api/chat/conversations');if(epoch!==generation)return;
      byId('chat-history-list').replaceChildren();byId('chat-history-status').textContent=data.conversations.length?'':'还没有保存的对话。';
      for(const item of data.conversations){
        const row=document.createElement('div');row.className='chat-history-row';const open=document.createElement('button');open.type='button';open.className='chat-history-open';const title=document.createElement('strong');title.textContent=item.title;const date=document.createElement('small');date.textContent=new Date(item.updated_at*1000).toLocaleString('zh-CN');open.append(title,date);
        open.onclick=async()=>{try{const detail=await api('/api/chat/conversations/'+encodeURIComponent(item.id));if(epoch!==generation)return;generation++;attachments=[];byId('chat-input').value='';acceptConversation(detail);render();renderAttachments();controls();drawer.close();}catch(error){byId('chat-history-status').textContent=error.message;}};
        const rename=document.createElement('button');rename.type='button';rename.className='button compact';rename.textContent='重命名';rename.onclick=async()=>{const name=prompt('对话名称',item.title);if(name===null)return;try{await api('/api/chat/conversations/'+item.id,{action:'rename',title:name,version:item.version});if(item.id===conversationId)await refreshConversation(item.id,generation);drawer.close();await history();}catch(error){byId('chat-history-status').textContent=error.message;}};
        const remove=document.createElement('button');remove.type='button';remove.className='button compact danger';remove.textContent='删除';remove.onclick=async()=>{if(!confirm('删除这段对话？'))return;try{await api('/api/chat/conversations/'+item.id,{action:'delete',version:item.version});attachmentMemory.delete(item.id);if(item.id===conversationId)newConversation();drawer.close();await history();}catch(error){byId('chat-history-status').textContent=error.message;}};
        row.append(open,rename,remove);byId('chat-history-list').append(row);
      }
    }catch(error){if(epoch===generation)byId('chat-history-status').textContent=error.message;}
  }
  function controls(){
    const locked=busy||reading;
    byId('chat-send').disabled=busy?stopping:locked||loading||!auth.user||!byId('chat-model').value||(!byId('chat-input').value.trim()&&!attachments.length);
    byId('chat-send').classList.toggle('is-stop',busy);byId('chat-send').title=busy?'停止生成':'发送消息';byId('chat-send').setAttribute('aria-label',busy?'停止生成':'发送消息');
    historyButton.disabled=locked;
    byId('chat-model').disabled=locked||loading;byId('chat-new').disabled=locked;byId('chat-input').disabled=busy;
    byId('chat-attach').disabled=locked;byId('chat-refresh').disabled=locked||loading;
    byId('chat-messages').setAttribute('aria-busy',String(busy));byId('chat-form').classList.toggle('is-busy',busy);
  }
  function fileChip(file,removable=false){
    const chip=document.createElement('div');chip.className='chat-file-chip';
    if(file.preview){const img=document.createElement('img');img.src=file.preview;img.alt=file.name;chip.append(img);}else chip.append(icon('file'));
    const details=document.createElement('span'),name=document.createElement('strong'),size=document.createElement('small');name.textContent=file.name;size.textContent=file.expired?'需重新上传':file.size>=1048576?(file.size/1048576).toFixed(1)+' MB':Math.max(1,Math.round(file.size/1024))+' KB';details.append(name,size);chip.append(details);
    if(file.expired){const upload=document.createElement('button');upload.type='button';upload.className='text-button';upload.textContent='重新上传';upload.onclick=()=>byId('chat-files').click();chip.append(upload);}
    if(removable){const remove=document.createElement('button');remove.type='button';remove.className='chat-file-remove';remove.textContent='×';remove.setAttribute('aria-label','移除 '+file.name);remove.disabled=busy;remove.onclick=()=>{attachments=attachments.filter(a=>a!==file);renderAttachments();controls();};chip.append(remove);}return chip;
  }
  function renderAttachments(){byId('chat-attachments').replaceChildren(...attachments.map(f=>fileChip(f,true)));}
  function resizeInput(){const input=byId('chat-input');input.style.height='auto';input.style.height=Math.min(input.scrollHeight,180)+'px';}
  function render(){
    const root=byId('chat-messages');root.replaceChildren();root.classList.toggle('has-messages',!!messages.length);
    if(!messages.length){
      const welcome=document.createElement('div');welcome.className='chat-welcome';const mark=document.createElement('div');mark.className='chat-hero-mark';mark.append(icon('spark'));
      const eyebrow=document.createElement('span');eyebrow.className='chat-hero-eyebrow';eyebrow.textContent='A SPACE FOR YOUR NEXT IDEA';
      const h=document.createElement('h2');h.textContent='有什么想法，一起聊聊。';const p=document.createElement('p');p.textContent='从一个问题开始，或把文档与图片交给我。';const prompts=document.createElement('div');prompts.className='chat-prompts';
      for(const [glyph,title,description,prompt] of [['file','读懂一份文档','提炼重点，理清思路','请帮我总结附件中的主要内容，并提炼关键结论。'],['image','看看这张图片','发现细节，获得灵感','请分析这张图片，描述关键细节。'],['spark','打磨一个想法','一起构思，一起完善','我有一个想法，想和你一起梳理。']]){
        const button=document.createElement('button');button.type='button';button.className='chat-prompt';const titleNode=document.createElement('strong');titleNode.textContent=title;const detail=document.createElement('span');detail.textContent=description;button.append(icon(glyph),titleNode,detail);button.onclick=()=>{byId('chat-input').value=prompt;resizeInput();controls();byId('chat-input').focus();if(glyph==='file'||glyph==='image')byId('chat-files').click();};prompts.append(button);
      }welcome.append(mark,eyebrow,h,p,prompts);root.append(welcome);
    }
    for(const message of messages){
      const article=document.createElement('article');article.className='chat-message '+message.role;const content=document.createElement('div');content.className='chat-message-body';content.textContent=message.content;article.append(content);
      if(message.attachments?.length){const files=document.createElement('div');files.className='chat-message-files';files.append(...message.attachments.map(f=>fileChip(f)));article.append(files);}
      if(message.role==='assistant'){const copy=document.createElement('button');copy.type='button';copy.className='chat-copy';copy.setAttribute('aria-label','复制回复');copy.title='复制回复';copy.append(icon('copy'));copy.onclick=async()=>{try{await navigator.clipboard.writeText(message.content);copy.replaceChildren(icon('check'));status('回复已复制');}catch(_){status('复制失败，请选中文字后复制。');}};article.append(copy);}root.append(article);
    }
    if(busy){const pending=document.createElement('div');pending.className='chat-thinking';pending.textContent='正在思考';for(let i=0;i<3;i++)pending.append(document.createElement('i'));root.append(pending);}root.scrollTop=root.scrollHeight;
  }
  function emptyModel(text){const option=document.createElement('option');option.value='';option.textContent=text;byId('chat-model').replaceChildren(option);}
  async function models(){
    const epoch=generation;loading=true;controls();
    try{
      if(!auth.user){emptyModel('登录后选择模型');status();return;}
      const data=await api('/api/chat/models');if(epoch!==generation)return;const previous=byId('chat-model').value;byId('chat-model').replaceChildren();
      for(const model of data.models){const option=document.createElement('option');option.value=model.id;option.textContent=model.label;byId('chat-model').append(option);}
      if(data.models.some(m=>m.id===previous))byId('chat-model').value=previous;if(!data.models.length)emptyModel('暂无模型');status(data.models.length?'':'暂无可用模型，请管理员在后台添加模型',!data.models.length);
    }catch(error){if(epoch===generation)status(error.message,true);}finally{if(epoch===generation){loading=false;controls();}}
  }
  function authChanged(){const next=auth.user?.id||null;if(owner!==next){stop(true);owner=next;generation++;messages=[];attachments=[];attachmentMemory.clear();conversationId=null;version=0;busy=false;reading=false;loading=false;controller=null;runId=null;stopping=false;byId('chat-input').value='';if(drawer.open)drawer.close();render();renderAttachments();}if(!busy)models();}
  async function addFiles(files){
    if(busy||reading)return;const epoch=generation;reading=true;controls();
    try{
      const selected=Array.from(files);if(attachments.length+selected.length>6)throw new Error('每条消息最多添加 6 个附件。');
      const total=[...messages.flatMap(m=>m.attachments||[]).filter(f=>f.data),...attachments].reduce((sum,f)=>sum+f.size,0)+selected.reduce((sum,f)=>sum+f.size,0);if(total>12*1024*1024)throw new Error('当前对话的附件合计最多 12 MB，请新建对话继续。');
      const added=[];
      for(const file of selected){
        if(!/\.(png|jpe?g|webp|pdf|txt|md|csv|json|log|docx|py|js|ts|html|css)$/i.test(file.name))throw new Error('支持图片、PDF、文本、代码和 DOCX 文件。');if(!file.size||file.size>5*1024*1024)throw new Error('每个附件应为 1 字节至 5 MB。');
        const url=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error('文件读取失败，请重新选择。'));reader.readAsDataURL(file);});added.push({name:file.name,data:url.slice(url.indexOf(',')+1),size:file.size,preview:/\.(png|jpe?g|webp)$/i.test(file.name)?url:null});
      }if(epoch!==generation)return;attachments.push(...added);renderAttachments();status();
    }catch(error){if(epoch===generation)status(error.message,true);}finally{if(epoch===generation){reading=false;controls();byId('chat-files').value='';}}
  }
  async function readEvents(response,callback){
    if(!response.body)throw new Error('浏览器未返回流式响应。');
    const reader=response.body.getReader(),decoder=new TextDecoder();let pending='',event='message',data=[];
    function line(value){if(value===''){if(data.length){callback(event,JSON.parse(data.join('\n')));}event='message';data=[];}else if(value.startsWith('event:'))event=value.slice(6).trim();else if(value.startsWith('data:'))data.push(value.slice(5).replace(/^ /,''));}
    try{while(true){const chunk=await reader.read();pending+=decoder.decode(chunk.value||new Uint8Array(),{stream:!chunk.done});let index;while((index=pending.indexOf('\n'))>=0){line(pending.slice(0,index).replace(/\r$/,''));pending=pending.slice(index+1);}if(chunk.done){if(pending)line(pending.replace(/\r$/,''));line('');break;}}}finally{reader.releaseLock();}
  }
  async function stop(leaving=false){
    if(!busy&&!controller)return;stopping=true;controls();const id=runId,transport=controller;
    cancelRequest=id?fetch('/api/chat/runs/'+encodeURIComponent(id)+'/cancel',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Omini-Client':'web'},body:'{}',keepalive:leaving}).catch(()=>{}):Promise.resolve();
    transport?.abort();await cancelRequest;
  }
  byId('chat-form').onsubmit=async event=>{
    event.preventDefault();if(busy){await stop();return;}
    const text=byId('chat-input').value.trim();if((!text&&!attachments.length)||reading||loading||!auth.user||!byId('chat-model').value)return;
    const epoch=generation,before=messages.slice(),draftFiles=[...attachments],prompt=[...messages,{role:'user',content:text,...(attachments.length?{attachments:draftFiles}:{})}];
    const serialised=prompt.map(m=>({role:m.role,content:m.content,...(m.attachments?.some(f=>f.data)?{attachments:m.attachments.filter(f=>f.data).map(({name,data})=>({name,data}))}:{})}));
    const assistant={role:'assistant',content:''};messages=[...prompt,assistant];busy=true;stopping=false;runId=null;controller=new AbortController();render();renderAttachments();controls();status();
    const bodyNode=byId('chat-messages').querySelector('.chat-message.assistant:last-of-type .chat-message-body');let accepted=false,terminal=false;
    try{
      const response=await fetch('/api/chat/stream',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Omini-Client':'web'},signal:controller.signal,body:JSON.stringify({model:byId('chat-model').value,messages:serialised,conversation_id:conversationId,version,request_id:crypto.randomUUID()})});
      if(!response.ok){let message='请求失败，请重试。';try{message=(await response.json()).error||message;}catch(_){}if(response.status===401)requireLogin();throw new Error(message);}
      await readEvents(response,(type,data)=>{
        if(epoch!==generation)return;
        if(type==='start'){
          accepted=true;conversationId=data.conversation_id;version=data.version;runId=data.run_id;remember();byId('chat-input').value='';attachments=[];resizeInput();renderAttachments();
          if(stopping)stop();
        }else if(type==='delta'){
          const root=byId('chat-messages'),follow=root.scrollHeight-root.scrollTop-root.clientHeight<90;
          assistant.content+=data.text||'';if(bodyNode)bodyNode.textContent=assistant.content;root.querySelector('.chat-thinking')?.remove();if(follow)root.scrollTop=root.scrollHeight;
        }else if(type==='done'||type==='error'){
          terminal=true;if(data.conversation)acceptConversation(data.conversation);else if(data.version)version=data.version;
          if(type==='error')status(data.message||data.error||'生成中断，已保留现有内容。',true);
        }
      });
      if(!terminal&&!stopping)throw new Error('连接已中断，已保留生成的内容。');
    }catch(error){
      if(epoch===generation){
        if(!accepted){messages=before;attachments=draftFiles;}
        if(error.name!=='AbortError')status(error.message+(!accepted?' 输入和附件已保留。':''),true);
      }
    }finally{
      if(epoch===generation){
        const id=conversationId,wasStopped=stopping;await cancelRequest;if(epoch!==generation)return;busy=false;runId=null;controller=null;stopping=false;remember();
        if(accepted&&id&&(!terminal||wasStopped)){try{await refreshConversation(id,epoch);}catch(_){} }
        render();renderAttachments();controls();if(activePage==='chat')byId('chat-input').focus();
      }
    }
  };
  byId('chat-input').oninput=()=>{resizeInput();controls();};byId('chat-input').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();if(!byId('chat-send').disabled)byId('chat-form').requestSubmit();}};
  function newConversation(){remember();generation++;conversationId=null;version=0;messages=[];attachments=[];byId('chat-input').value='';const heading=document.querySelector('.chat-page-header h1');if(heading)heading.textContent='ChatGPT 对话';resizeInput();render();renderAttachments();controls();status();}
  byId('chat-new').onclick=newConversation;historyButton.onclick=history;
  byId('chat-attach').onclick=()=>byId('chat-files').click();byId('chat-files').onchange=event=>addFiles(event.target.files);byId('chat-input').onpaste=event=>{const files=Array.from(event.clipboardData?.files||[]);if(files.length){event.preventDefault();addFiles(files);}};
  const drop=byId('chat-dropzone');let depth=0;
  drop.ondragenter=event=>{if(Array.from(event.dataTransfer.types).includes('Files')){event.preventDefault();depth++;drop.classList.add('dragging');}};drop.ondragover=event=>{event.preventDefault();event.dataTransfer.dropEffect='copy';};drop.ondragleave=event=>{event.preventDefault();if(--depth<=0){depth=0;drop.classList.remove('dragging');}};drop.ondrop=event=>{event.preventDefault();depth=0;drop.classList.remove('dragging');addFiles(event.dataTransfer.files);};
  window.addEventListener('omini-route-change',()=>{if(activePage!=='chat')stop(true);});
  window.addEventListener('pagehide',()=>stop(true));
  byId('chat-refresh').onclick=models;window.addEventListener('omini-auth-change',authChanged);authChanged();render();controls();
})();
