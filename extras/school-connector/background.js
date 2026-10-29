const schoolHost='jw.hualixy.edu.cn';
const loginUrl='https://sso.hualixy.edu.cn/heer_cas/#/login?service=https%3A%2F%2Fjw.hualixy.edu.cn%2Fsso%2Fddlogin';
function allowedPage(url){
  const value=new URL(url);
  return ['https://omini.xin','https://my.omini.xin'].includes(value.origin)||value.protocol==='http:'&&value.hostname==='127.0.0.1';
}
async function send(connection,message){
  const tab=await chrome.tabs.get(connection.tabId);
  if(new URL(tab.url).origin!==connection.origin)return false;
  await chrome.tabs.sendMessage(connection.tabId,{...message,requestId:connection.requestId});return true;
}
async function readSchoolSchedule(year,term){
  async function post(path,values){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
    try{
      const response=await fetch(path+'?gnmkdm=N2151',{method:'POST',credentials:'include',signal:controller.signal,referrer:location.origin+'/jwglxt/kbcx/xskbcx_cxXskbcxIndex.html?gnmkdm=N2151&layout=default',headers:{'X-Requested-With':'XMLHttpRequest','Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'},body:new URLSearchParams(values).toString()});
      if(!response.ok)throw new Error('教务接口暂未就绪');
      const text=await response.text();if(text.length>4*1024*1024)throw new Error('课表响应过大');return JSON.parse(text);
    }finally{clearTimeout(timer);}
  }
  try{
    const data=await post('/jwglxt/kbcx/xskbcx_cxXsgrkb.html',{xnm:year,xqm:term,kzlx:'ck',xsdm:'',kclbdm:'',kclxdm:''});
    if(!Array.isArray(data.kbList))return null;
    let periods=[];
    try{const value=await post('/jwglxt/kbcx/xskbcx_cxRjc.html',{xnm:year,xqm:term,xqh_id:String(data.kbList[0]?.xqh_id||'1')});if(Array.isArray(value))periods=value;}catch(_){}
    return {payload:{kbList:data.kbList},periods,year,term};
  }catch(_){return null;}
}
let reading=false;
async function refresh(){
  if(reading)return;reading=true;
  try{
    const {connection}=await chrome.storage.session.get('connection');if(!connection)return;
    if(!await send(connection,{type:'school-status',status:connection.delivered?'connected':'waiting'}))return;
    if(!connection.delivered&&Date.now()-connection.startedAt>10*60*1000){await send(connection,{type:'school-status',status:'expired'});return;}
    let importData;
    if(!connection.delivered&&connection.loginTabId){
      const tab=await chrome.tabs.get(connection.loginTabId);
      if(!tab.url||new URL(tab.url).hostname!==schoolHost||tab.status!=='complete')return;
      const result=await chrome.scripting.executeScript({target:{tabId:connection.loginTabId},func:readSchoolSchedule,args:[connection.year,connection.term]});
      importData=result[0]?.result;if(!importData)return;
    }else if(!connection.delivered)return;
    const cookies=await chrome.cookies.getAll({url:'https://jw.hualixy.edu.cn/jwglxt/kbcx/xskbcx_cxXsgrkb.html'});
    const cookie=cookies.map(item=>`${item.name}=${item.value}`).join('; ');
    const latest=(await chrome.storage.session.get('connection')).connection;
    if(latest?.requestId!==connection.requestId)return;
    if(await send(connection,{type:'school-session',cookie,importData,automatic:!connection.delivered})){
      const current=(await chrome.storage.session.get('connection')).connection;
      if(current?.requestId===connection.requestId)await chrome.storage.session.set({connection:{...current,delivered:true}});
    }
  }catch(_){/* The waiting page retries transient navigation failures. */}
  finally{reading=false;}
}
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  if(!['connect','refresh','disconnect'].includes(message.type))return;
  (async()=>{
    if(!sender.tab||!allowedPage(sender.url))throw new Error('请在 Omini 课表页面连接。');
    const {connection}=await chrome.storage.session.get('connection');
    if(message.type==='connect'){
      if(!/^\d{4}$/.test(String(message.year))||!['3','12','16'].includes(String(message.term))||typeof message.requestId!=='string'||message.requestId.length>100)throw new Error('请先选择学年和学期。');
      if(connection?.requestId===message.requestId){await refresh();return;}
      const next={tabId:sender.tab.id,origin:new URL(sender.url).origin,requestId:message.requestId,year:String(message.year),term:String(message.term),startedAt:Date.now(),delivered:false};
      const tab=await chrome.tabs.create({url:loginUrl});next.loginTabId=tab.id;
      await chrome.storage.session.set({connection:next});await refresh();return;
    }
    if(!connection||sender.tab.id!==connection.tabId||new URL(sender.url).origin!==connection.origin)return;
    if(message.type==='disconnect'){await chrome.storage.session.remove('connection');return;}
    await refresh();
  })().then(()=>reply({ok:true}),error=>reply({ok:false,error:error.message}));return true;
});
chrome.cookies.onChanged.addListener(change=>{
  const domain=change.cookie.domain.replace(/^\./,'');if(domain===schoolHost||schoolHost.endsWith('.'+domain))refresh();
});
chrome.tabs.onUpdated.addListener((_tabId,change)=>{if(change.status==='complete')refresh();});
