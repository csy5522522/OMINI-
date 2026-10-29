'use strict';
(() => {
  const root=document.documentElement;
  const validScales=[100,110,125];
  const read=(key,fallback)=>{try{return localStorage.getItem(key)||fallback;}catch(_){return fallback;}};
  const save=(key,value)=>{try{localStorage.setItem(key,String(value));}catch(_){}};
  const mobile=()=>window.innerWidth<=720;
  const scaleKey=()=>mobile()?'omini-scale-mobile':'omini-scale-desktop';
  let theme=read('omini-theme','dark');if(!['light','dark'].includes(theme))theme='dark';
  let scale=100;
  const event=(name,detail)=>window.dispatchEvent(new window.CustomEvent(name,{detail}));
  function updateLabels(){
    document.querySelectorAll('[data-appearance-theme]').forEach(select=>{select.value=theme;});
    document.querySelectorAll('[data-appearance-scale]').forEach(select=>{select.value=String(scale);});
    const label=document.getElementById('theme-label');if(label)label.textContent=theme==='dark'?'切换浅色模式':'切换深色模式';
    const button=document.getElementById('admin-theme-toggle');if(button)button.textContent=theme==='dark'?'浅色模式':'深色模式';
  }
  function setTheme(value,{persist=true}={}){
    if(!['light','dark'].includes(value))return;
    theme=value;root.dataset.theme=theme;if(persist)save('omini-theme',theme);updateLabels();event('omini-appearance-change',{theme,scale});
  }
  function applyScale(){
    const saved=Number(read(scaleKey(),mobile()?'100':'125'));scale=validScales.includes(saved)?saved:(mobile()?100:125);
    root.style.setProperty('--app-scale',String(scale/100));root.style.setProperty('--app-scale-percent',scale+'%');
    root.style.setProperty('--app-viewport-height',(window.innerHeight/(scale/100))+'px');
    root.style.setProperty('--app-viewport-width',(window.innerWidth/(scale/100))+'px');
    root.dataset.scale=String(scale);root.dataset.layout=mobile()?'mobile':'desktop';
    // CSS zoom participates in layout and keeps text crisp; do not use transform.
    document.documentElement.style.zoom=String(scale/100);updateLabels();event('omini-appearance-change',{theme,scale});
  }
  function setScale(value){const number=Number(value);if(!validScales.includes(number))return;save(scaleKey(),number);applyScale();}
  function mountSettings(){
    const settings=document.getElementById('settings-dialog');
    if(settings&&!document.getElementById('appearance-settings')){
      const section=document.createElement('section');section.id='appearance-settings';section.className='appearance-settings';
      section.innerHTML='<h3>显示与主题</h3><div class="appearance-controls"><label>颜色主题<select data-appearance-theme aria-label="全局颜色主题"><option value="light">浅色模式</option><option value="dark">深色模式</option></select></label><label>页面缩放<select data-appearance-scale aria-label="页面缩放"><option value="100">100%</option><option value="110">110%</option><option value="125">125%</option></select></label></div>';
      settings.insertBefore(section,document.getElementById('settings-form'));
    }
    const admin=document.querySelector('.admin-shell');
    if(admin&&!document.getElementById('admin-appearance')){
      const section=document.createElement('div');section.id='admin-appearance';section.className='admin-appearance';
      section.innerHTML='<button class="button compact" id="admin-theme-toggle" type="button"></button><label>缩放 <select data-appearance-scale aria-label="后台页面缩放"><option value="100">100%</option><option value="110">110%</option><option value="125">125%</option></select></label>';
      const heading=admin.querySelector('h1');heading?.insertAdjacentElement('afterend',section);
      document.getElementById('admin-theme-toggle').onclick=async()=>{
        setTheme(theme==='dark'?'light':'dark');
        if(!document.getElementById('admin-panel')?.hidden){
          try{
            const response=await fetch('/api/settings',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Omini-Client':'web'},body:JSON.stringify({features:{},theme:theme==='dark'?'深色':'浅色'})});
            if(!response.ok)throw new Error('主题已在本机切换，账号同步稍后重试。');
          }catch(error){const status=document.getElementById('admin-status');if(status)status.textContent=error.message;}
        }
      };
    }
    document.querySelectorAll('[data-appearance-theme]').forEach(select=>select.onchange=()=>{
      if(typeof window.setTheme==='function')window.setTheme(select.value,true);else setTheme(select.value);
    });
    document.querySelectorAll('[data-appearance-scale]').forEach(select=>select.onchange=()=>setScale(select.value));
    updateLabels();
  }
  window.OminiAppearance={getTheme:()=>theme,setTheme,getScale:()=>scale,setScale};
  setTheme(theme,{persist:false});applyScale();
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mountSettings,{once:true});else mountSettings();
  window.addEventListener('resize',applyScale);
  window.addEventListener('storage',e=>{if(e.key==='omini-theme')setTheme(read('omini-theme','dark'),{persist:false});if(e.key===scaleKey())applyScale();});
})();
