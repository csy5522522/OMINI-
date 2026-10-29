document.querySelector('#connect').addEventListener('click',async()=>{
  const status=document.querySelector('#status');
  try{
    const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
    if(!['https:','http:'].includes(new URL(tab.url).protocol))throw new Error('请先打开 Omini 工具箱页面。');
    await chrome.scripting.executeScript({target:{tabId:tab.id},files:['bridge.js']});
    const result=await chrome.scripting.executeScript({target:{tabId:tab.id},func:()=>{
      if(!document.querySelector('#online-form #school-cookie')||!document.querySelector('#import-dialog'))return false;
      const button=document.querySelector('#online-login');if(button){button.click();return true;}
      const date=new Date(),year=String(date.getMonth()>=7?date.getFullYear():date.getFullYear()-1);
      window.postMessage({type:'omini-school-connect',year,term:'3',requestId:String(Date.now())},location.origin);return true;
    }});
    if(!result[0]?.result)throw new Error('请在 Omini 学校课表页面连接。');
    status.textContent='已连接。完成学校登录后，课表将自动导入。';
  }catch(error){status.textContent=error.message;}
});
