if(!globalThis.ominiSchoolBridge){
  globalThis.ominiSchoolBridge=true;
  const post=value=>window.postMessage(value,location.origin);
  const ready=()=>post({type:'omini-school-ready',version:chrome.runtime.getManifest().version});
  chrome.runtime.onMessage.addListener((message,_sender,reply)=>{
    if(message.type==='school-session')post({...message,type:'omini-school-session'});
    if(message.type==='school-status')post({...message,type:'omini-school-status'});
    reply({ok:true});
  });
  window.addEventListener('message',event=>{
    if(event.source!==window||event.origin!==location.origin)return;
    const message=event.data;
    if(message?.type==='omini-school-hello'){ready();return;}
    const type={'omini-school-connect':'connect','omini-school-request':'refresh','omini-school-disconnect':'disconnect'}[message?.type];
    if(!type)return;
    chrome.runtime.sendMessage({...message,type}).then(result=>{
      if(result?.ok===false)post({type:'omini-school-status',status:'error',error:result.error,requestId:message.requestId});
    }).catch(()=>post({type:'omini-school-status',status:'error',error:'教务扩展连接已失效，请刷新页面后重新连接。',requestId:message.requestId}));
  });
  ready();
}
