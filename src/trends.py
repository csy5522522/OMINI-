"""Public trending adapters with shared bounded latest-value caching.

These are public website endpoints rather than guaranteed developer APIs. Each
adapter is independent, so a changed upstream never hides the other platforms.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import quote

import requests

TTL = 300
MIN_REFRESH = 60
MAX_STALE = 24 * 3600
MAX_RESPONSE = 2 * 1024 * 1024
SOURCES = {
    'douyin': ('抖音热搜', 'https://www.douyin.com/hot'),
    'baidu': ('百度热搜', 'https://top.baidu.com/board?tab=realtime'),
    'weibo': ('微博热搜', 'https://s.weibo.com/top/summary/'),
    'bilibili': ('哔哩哔哩热搜', 'https://search.bilibili.com/'),
}
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'


def _get(url, referer, *, text=False):
    with requests.Session() as session:
        session.trust_env = False
        with session.get(url, headers={'User-Agent': UA, 'Referer': referer,
                                      'Accept': 'application/json,text/html;q=0.9'},
                         timeout=(3, 6), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ValueError(f'HTTP {response.status_code}')
            chunks = []; size = 0
            for chunk in response.iter_content(16384):
                size += len(chunk)
                if size > MAX_RESPONSE: raise ValueError('响应数据过大')
                chunks.append(chunk)
            raw = b''.join(chunks).decode('utf-8-sig')
            return raw if text else json.loads(raw)


def _heat(value):
    if isinstance(value, (int, float)) and value >= 0: return int(value)
    if isinstance(value, str) and re.fullmatch(r'\d+', value): return int(value)
    return None


def _items(source, rows):
    if not isinstance(rows, list): raise ValueError('榜单结构已变化')
    result = []; seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get('is_ad') or row.get('is_commercial'): continue
        title = row.get('word') or row.get('keyword') or row.get('show_name') or row.get('note')
        if not isinstance(title, str) or not title.strip(): continue
        title = title.strip()[:240]
        if title.casefold() in seen: continue
        seen.add(title.casefold())
        if source == 'douyin':
            url = 'https://www.douyin.com/search/' + quote(title, safe='')
            heat = _heat(row.get('hot_value')); tag = ''
        elif source == 'baidu':
            url = 'https://www.baidu.com/s?wd=' + quote(title, safe='')
            heat = _heat(row.get('hotScore')); tag = {1:'新', 3:'热'}.get(row.get('hotTag'), '')
        elif source == 'weibo':
            url = 'https://s.weibo.com/weibo?q=' + quote(title, safe='')
            heat = _heat(row.get('num', row.get('raw_hot'))); tag = str(row.get('label_name') or '')[:10]
        else:
            url = 'https://search.bilibili.com/all?keyword=' + quote(title, safe='')
            heat = _heat(row.get('heat_score')); tag = ''
        result.append({'rank':len(result)+1, 'title':title, 'url':url, 'heat':heat, 'tag':tag})
        if len(result) == 50: break
    if not result: raise ValueError('源站暂未返回榜单')
    return result


def _baidu_rows(data):
    cards = data.get('data', data).get('cards', [])
    content = cards[0].get('content', []) if cards else []
    if content and isinstance(content[0], dict) and isinstance(content[0].get('content'), list):
        content = content[0]['content']
    return content


def fetch_source(source):
    """Try only fixed public source endpoints; no login or arbitrary URL fetches."""
    if source not in SOURCES: raise ValueError('热搜来源有误')
    if source == 'douyin':
        attempts = [lambda host=host: _get('https://'+host+'/aweme/v1/hot/search/list/?aid=1128&version_code=880', 'https://www.douyin.com/')
                    for host in ('aweme.snssdk.com','aweme-lq.snssdk.com')]
    elif source == 'baidu':
        attempts = [lambda: _get('https://top.baidu.com/api/board?platform=pc&tab=realtime','https://top.baidu.com/'),
                    lambda: _get('https://top.baidu.com/board?tab=realtime','https://top.baidu.com/',text=True)]
    elif source == 'weibo':
        attempts = [lambda: _get('https://weibo.com/ajax/side/hotSearch','https://weibo.com/')]
    else:
        attempts = [lambda: _get('https://s.search.bilibili.com/main/hotword?limit=50','https://www.bilibili.com/'),
                    lambda: _get('https://api.bilibili.com/x/web-interface/search/square?limit=50','https://www.bilibili.com/')]
    error = '源站暂不可用'
    for attempt in attempts:
        try:
            data = attempt()
            if isinstance(data, str):
                match = re.search(r'<!--s-data:(.*?)-->', data, re.S)
                if not match: raise ValueError('榜单结构已变化')
                data = json.loads(match.group(1))
            if not isinstance(data, dict): raise ValueError('榜单结构已变化')
            if source == 'douyin':
                if data.get('status_code',0) != 0: raise ValueError('源站暂不可用')
                rows = data.get('data',{}).get('word_list',[])
            elif source == 'baidu': rows = _baidu_rows(data)
            elif source == 'weibo': rows = data.get('data',{}).get('realtime',[])
            else:
                if data.get('code',0) != 0: raise ValueError('源站暂不可用')
                rows = data.get('list') or data.get('data',{}).get('trending',{}).get('list',[])
            return _items(source, rows)
        except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
            error = str(exc) if isinstance(exc, ValueError) and str(exc).startswith('HTTP ') else '源站响应异常，请稍后重试'
    raise ValueError(error)


class TrendStore:
    """At most two requests run concurrently, with one shared refresh per source."""
    def __init__(self, data_dir, *, fetcher=fetch_source, clock=time.time, executor=None):
        path = Path(data_dir)
        self.path = path if path.suffix == '.json' else path / 'trends-latest.json'
        self.fetcher, self.clock = fetcher, clock
        self.lock = threading.RLock()
        self.cache = {}; self.attempts = {}; self.errors = {}; self.inflight = set(); self.closed = False
        self.executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix='omini-trends')
        self._load()

    def _load(self):
        try:
            if self.path.stat().st_size > 256 * 1024: return
            document = json.loads(self.path.read_text(encoding='utf-8'))
            for source, saved in document.get('sources',{}).items():
                if source not in SOURCES or not isinstance(saved,dict): continue
                updated = saved.get('updated_at'); rows = saved.get('items')
                if not isinstance(updated,(int,float)) or not isinstance(rows,list): continue
                if not 0 <= self.clock()-updated < MAX_STALE: continue
                valid = []
                for item in rows[:50]:
                    if (isinstance(item,dict) and isinstance(item.get('title'),str) and
                            isinstance(item.get('url'),str) and item['url'].startswith(('https://www.douyin.com/search/','https://www.baidu.com/s?','https://s.weibo.com/weibo?','https://search.bilibili.com/all?'))):
                        valid.append({'rank':len(valid)+1,'title':item['title'][:240],'url':item['url'][:2048],
                                      'heat':_heat(item.get('heat')),'tag':str(item.get('tag')or'')[:10]})
                if valid: self.cache[source] = {'updated_at':updated,'items':valid}
        except (OSError, ValueError, TypeError, AttributeError): pass

    def _persist(self):
        # Called under the lock. Only one bounded latest snapshot is retained.
        try:
            raw = json.dumps({'version':1,'sources':self.cache},ensure_ascii=False).encode('utf-8')
            if len(raw)>256*1024: return
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            with temporary.open('wb') as file: file.write(raw)
            os.replace(temporary,self.path)
        except OSError: pass  # A cache persistence issue must not take the page down.

    def _refresh(self, source):
        try:
            items = self.fetcher(source)
            if not isinstance(items,list) or not items: raise ValueError('源站暂未返回榜单')
            with self.lock:
                self.cache[source] = {'updated_at':self.clock(),'items':items[:50]}
                self.errors.pop(source,None); self._persist()
        except Exception as error:
            with self.lock:
                self.errors[source] = str(error) if isinstance(error,ValueError) and str(error).startswith('HTTP ') else '源站暂不可用，请稍后重试'
        finally:
            with self.lock: self.inflight.discard(source)

    def snapshot(self, *, refresh=False):
        """Return immediately. Cold/stale sources refresh in background."""
        with self.lock:
            now = self.clock(); sources = []
            for source,(title,url) in SOURCES.items():
                cached = self.cache.get(source)
                age = now-cached['updated_at'] if cached else MAX_STALE
                due = refresh or not cached or age>=TTL or age<0
                if (not self.closed and due and source not in self.inflight and
                        now-self.attempts.get(source,-MAX_STALE)>=MIN_REFRESH):
                    self.inflight.add(source); self.attempts[source]=now
                    self.executor.submit(self._refresh,source)
                has_data = cached is not None and 0<=age<MAX_STALE
                state = ('fresh' if has_data and age<TTL and source not in self.errors else
                         'stale' if has_data else 'loading' if source in self.inflight else 'error')
                item={'id':source,'title':title,'url':url,'status':state,
                      'updated_at':cached['updated_at'] if has_data else None,
                      'refreshing':source in self.inflight,'items':copy.deepcopy(cached['items'][:10]) if has_data else []}
                if source in self.errors: item['error']=self.errors[source]
                sources.append(item)
            return {'generated_at':now,'poll_after':2 if any(s['status']=='loading' for s in sources) else 60,
                    'refresh_interval':TTL,'sources':sources}

    def close(self):
        with self.lock: self.closed = True; self.inflight.clear()
        self.executor.shutdown(wait=False,cancel_futures=True)


class TrendsService(TrendStore):
    def get(self, *, force=False):
        return self.snapshot(refresh=force)
