'use strict';
(() => {
  const doc=document;
  const byId=id=>doc.getElementById(id);
  if(!byId('page-trends')){
    const section=doc.createElement('section');section.id='page-trends';section.className='page';section.hidden=true;
    doc.querySelector('main')?.append(section);
  }
  if(!doc.querySelector('[data-page="trends"]')){
    const link=doc.createElement('a');link.href='#trends';link.className='nav-item';link.dataset.page='trends';
    link.innerHTML='<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M13 3s2 5-2 8c-2-3-4-2-4-2s-6 8 2 12c8 3 12-5 9-10 0 4-3 4-3 4s2-7-2-12Z"/></svg><span class="nav-text">热搜大全</span>';
    byId('sidebar-menu')?.append(link);
  }
  const page=byId('page-trends');if(!page)return;
  page.innerHTML='<div class="page-heading trends-heading"><div><span class="eyebrow">WHAT IS HAPPENING</span><h1>热搜大全<span class="heading-dot">.</span></h1><p>四个平台，一起看看正在发生的事。</p></div><button id="trends-refresh" class="button" type="button">刷新热搜</button></div><div class="trends-page-status"><span id="trends-status" role="status">榜单每 5 分钟自动更新</span><span>公开榜单 · 来自各平台</span></div><div id="trends-grid" class="trends-grid"></div>';
  const definitions=[['douyin','抖音热搜','D'],['baidu','百度热搜','百'],['weibo','微博热搜','微'],['bilibili','哔哩哔哩热搜','B']];
  const sourceCards=new Map();
  for(const [id,title,monogram]of definitions){
    const card=doc.createElement('section');card.className='card trends-card';card.dataset.source=id;
    const header=doc.createElement('header'),mark=doc.createElement('span'),name=doc.createElement('h2'),meta=doc.createElement('p');
    mark.className='trends-source-icon';mark.textContent=monogram;name.textContent=title;meta.className='trends-source-status';meta.textContent='等待读取';header.append(mark,name);
    const list=doc.createElement('ol');list.className='trends-list';const empty=doc.createElement('p');empty.className='trends-empty';empty.textContent='正在读取榜单…';
    const footer=doc.createElement('footer'),link=doc.createElement('a');link.textContent='前往平台 ↗';link.target='_blank';link.rel='noopener noreferrer';footer.append(meta,link);
    card.append(header,list,empty,footer);byId('trends-grid').append(card);sourceCards.set(id,{card,list,empty,meta,link});
  }
  let timer=null,busy=false,controller=null,epoch=0;
  const visible=()=>location.hash==='#trends'&&!doc.hidden;
  function stop(){clearTimeout(timer);timer=null;if(controller){controller.abort();controller=null;}busy=false;epoch++;byId('trends-refresh').disabled=false;}
  function schedule(seconds){clearTimeout(timer);if(visible())timer=setTimeout(()=>load(),seconds*1000);}
  function heatLabel(value){if(typeof value!=='number'||!Number.isFinite(value))return '';return value>=10000?(value/10000).toFixed(value>=1000000?0:1)+'万':value.toLocaleString('zh-CN');}
  function render(data){
    for(const source of data.sources||[]){
      const ui=sourceCards.get(source.id);if(!ui)continue;ui.card.dataset.state=source.status;ui.list.replaceChildren();
      for(const item of(source.items||[]).slice(0,10)){
        const row=doc.createElement('li'),rank=doc.createElement('span'),link=doc.createElement('a'),heat=doc.createElement('span');rank.className='trends-rank';rank.textContent=item.rank;
        link.textContent=item.title;link.href=item.url;link.target='_blank';link.rel='noopener noreferrer';heat.className='trends-heat';heat.textContent=heatLabel(item.heat);
        if(item.tag){const tag=doc.createElement('small');tag.className='trends-tag';tag.textContent=item.tag;link.append(tag);}row.append(rank,link,heat);ui.list.append(row);
      }
      ui.empty.hidden=Boolean(source.items?.length);ui.empty.textContent=source.status==='loading'?'正在更新榜单…':'此平台暂时没有可用榜单，请稍后重试。';
      ui.link.href=source.url;
      const stamp=source.updated_at?new Date(source.updated_at*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}):'';
      ui.meta.textContent=source.status==='stale'?`上次数据 ${stamp} · 更新暂未成功`:source.status==='error'?'更新失败':source.refreshing?'正在更新…':stamp?`${stamp} 更新`:'正在读取…';
    }
  }
  async function load(force=false){
    if(!visible()||busy)return;busy=true;const current=epoch;controller=new AbortController();byId('trends-refresh').disabled=true;
    if(force)byId('trends-status').textContent='正在同步各平台榜单…';
    try{
      const response=await fetch('/api/trends'+(force?'?refresh=1':''),{credentials:'same-origin',signal:controller.signal});
      const data=await response.json();if(!response.ok)throw new Error(data.error||'榜单读取失败，请重试。');
      if(current!==epoch)return;render(data);
      const stale=(data.sources||[]).filter(s=>s.status==='stale'||s.status==='error').length;
      byId('trends-status').textContent=stale?`${stale} 个平台暂未更新，其余榜单正常显示`:'榜单每 5 分钟自动更新 · 当前页面自动同步';
      schedule(Math.max(2,Math.min(60,Number(data.poll_after)||60)));
    }catch(error){if(current===epoch&&error.name!=='AbortError'){byId('trends-status').textContent=error.message;schedule(60);}}
    finally{if(current===epoch){busy=false;controller=null;byId('trends-refresh').disabled=false;}}
  }
  function routeChanged(){if(visible()){if(!busy)load();}else stop();}
  byId('trends-refresh').onclick=()=>load(true);
  doc.addEventListener('visibilitychange',routeChanged);window.addEventListener('omini-route-change',routeChanged);window.addEventListener('hashchange',routeChanged);
  if(typeof route==='function')route();else routeChanged();
})();
