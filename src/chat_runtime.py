"""Async model adapters and the cancellable bridge used by the threaded HTTP server.

Only semantic text/usage events are exposed. Tools have a registry boundary but
are deliberately not advertised to or executed by models in this release.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError as FutureCancelled
import json
import queue
import threading
from typing import AsyncIterator

import httpx

TOTAL_TIMEOUT = 300.0
IDLE_TIMEOUT = 60.0


class ChatBusy(ValueError):
    status_code = 429


class ModelStreamError(ValueError):
    pass


class ToolRegistry:
    """Future extension seam; no actual tools are enabled in this release."""
    def definitions(self):
        return []


def request_parts(config, messages, *, model=None, system='', json_schema=None, streaming=False):
    protocol = config['protocol']
    model = model or (config.get('models') or [{}])[0].get('name')
    if not model:
        raise ValueError('请配置模型 ID。')
    if protocol == 'openai':
        path = '/chat/completions'
        payload = {'model': model, 'messages': ([{'role':'system','content':system}] if system else []) + messages,
                   'stream': streaming}
        if json_schema:
            payload['response_format'] = {'type':'json_schema','json_schema':{'name':'result','schema':json_schema,'strict':True}}
    elif protocol == 'responses':
        path = '/responses'
        payload = {'model': model, 'input': messages, 'stream': streaming, 'store': False}
        if system: payload['instructions'] = system
        if json_schema:
            payload['text'] = {'format':{'type':'json_schema','name':'result','schema':json_schema,'strict':True}}
    elif protocol == 'anthropic':
        path = '/v1/messages'
        payload = {'model': model, 'messages': messages, 'max_tokens': 8192, 'stream': streaming}
        if system: payload['system'] = system
        if json_schema:
            payload['output_config'] = {'format':{'type':'json_schema','schema':json_schema}}
    else:
        raise ValueError('请选择受支持的聊天协议。')
    headers = {'Accept': 'text/event-stream' if streaming else 'application/json'}
    if protocol == 'anthropic':
        headers.update({'x-api-key':config['api_key'],'anthropic-version':'2023-06-01'})
    else:
        headers['Authorization'] = 'Bearer ' + config['api_key']
    headers.update(config.get('headers') or {})
    return config['base_url'].rstrip('/') + path, headers, payload


def client_options(config, idle_timeout):
    options = {'timeout':httpx.Timeout(idle_timeout, connect=10, write=30, pool=10),
               'trust_env':False, 'follow_redirects':False}
    proxy = config.get('proxy')
    if proxy and proxy != 'direct': options['proxy'] = proxy
    return options


def extract_text(protocol, data):
    if protocol == 'openai':
        content = data['choices'][0]['message'].get('content')
    elif protocol == 'responses':
        content = ''.join(p.get('text','') for item in data.get('output',[]) if item.get('type') == 'message'
                          for p in item.get('content',[]) if p.get('type') == 'output_text')
    else:
        content = ''.join(p.get('text','') for p in data.get('content',[]) if p.get('type') == 'text')
    if not isinstance(content,str) or not content.strip():
        raise ModelStreamError('模型未返回文本，请检查模型和协议。')
    return content


async def complete(config, messages, *, model=None, system='', json_schema=None,
                   timeout=TOTAL_TIMEOUT, idle_timeout=IDLE_TIMEOUT) -> str:
    """Non-streamed model call for structured tasks such as timetable recognition."""
    url, headers, payload = request_parts(config,messages,model=model,system=system,json_schema=json_schema)
    try:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(**client_options(config,idle_timeout)) as client:
                response = await client.post(url,headers=headers,json=payload)
                if not 200 <= response.status_code < 300:
                    raise ModelStreamError(f'模型服务返回 HTTP {response.status_code}，请检查配置或稍后重试。')
                return extract_text(config['protocol'],response.json())
    except (TimeoutError,httpx.TimeoutException):
        raise ModelStreamError('模型请求超时，请稍后重试。') from None
    except httpx.HTTPError:
        raise ModelStreamError('连接模型服务失败，请稍后重试。') from None
    except (KeyError,IndexError,TypeError,json.JSONDecodeError):
        raise ModelStreamError('模型响应格式有误，请检查协议。') from None


async def sse_events(response) -> AsyncIterator[tuple[str,str]]:
    """Read SSE frames, including multiline data, comments and UTF-8 fragmentation."""
    event = ''; data = []; size = 0
    async for line in response.aiter_lines():
        size += len(line)
        if size > 1024*1024: raise ModelStreamError('模型流式事件过大。')
        if not line:
            if data: yield event, '\n'.join(data)
            event = ''; data = []; size = 0
            continue
        if line.startswith(':'): continue
        field, _, value = line.partition(':')
        if value.startswith(' '): value=value[1:]
        if field == 'event': event=value
        elif field == 'data': data.append(value)
    if data: yield event,'\n'.join(data)


async def stream_model(config, model, messages, *, system='', timeout=TOTAL_TIMEOUT,
                       idle_timeout=IDLE_TIMEOUT) -> AsyncIterator[dict]:
    url, headers, payload = request_parts(config,messages,model=model,system=system,streaming=True)
    protocol=config['protocol']; finished=False; received=False
    try:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(**client_options(config,idle_timeout)) as client:
                async with client.stream('POST',url,headers=headers,json=payload) as response:
                    if not 200 <= response.status_code < 300:
                        raise ModelStreamError(f'模型服务返回 HTTP {response.status_code}，请检查配置或稍后重试。')
                    if 'text/event-stream' not in response.headers.get('content-type','').lower():
                        raise ModelStreamError('此模型线路未返回流式响应，请检查协议和上游配置。')
                    async for event, raw in sse_events(response):
                        if raw.strip() == '[DONE]': finished=True; break
                        try: data=json.loads(raw)
                        except (ValueError,TypeError): raise ModelStreamError('模型流式数据格式有误。') from None
                        if not isinstance(data,dict): raise ModelStreamError('模型流式数据格式有误。')
                        kind=data.get('type',event); text=''
                        if data.get('error') or kind in ('error','response.failed','response.incomplete'):
                            raise ModelStreamError('模型服务中断了回复，请重试或更换模型。')
                        if protocol == 'openai':
                            choices=data.get('choices') or []
                            if choices:
                                delta=choices[0].get('delta') or {}
                                if delta.get('tool_calls'): raise ModelStreamError('当前会话尚未启用工具执行，请使用普通对话模型。')
                                text=delta.get('content') or ''
                                reason=choices[0].get('finish_reason')
                                if reason is not None:
                                    if reason not in ('stop',):
                                        raise ModelStreamError('模型回复未完整结束，请继续提问或调整模型配置。')
                                    finished=True
                            if data.get('usage'): yield {'type':'usage','usage':data['usage']}
                        elif protocol == 'responses':
                            if kind == 'response.output_text.delta': text=data.get('delta') or ''
                            elif kind == 'response.completed':
                                finished=True
                                if (data.get('response') or {}).get('usage'):
                                    yield {'type':'usage','usage':data['response']['usage']}
                            elif kind == 'response.output_item.added' and (data.get('item') or {}).get('type') in ('function_call','tool_call'):
                                raise ModelStreamError('当前会话尚未启用工具执行。')
                        else:
                            if kind == 'content_block_delta':
                                delta=data.get('delta') or {}
                                if delta.get('type') == 'text_delta':text=delta.get('text') or ''
                            elif kind == 'content_block_start' and (data.get('content_block') or {}).get('type') == 'tool_use':
                                raise ModelStreamError('当前会话尚未启用工具执行。')
                            elif kind == 'message_delta':
                                reason=(data.get('delta') or {}).get('stop_reason')
                                if reason and reason not in ('end_turn','stop_sequence'):
                                    raise ModelStreamError('模型回复未完整结束，请继续提问或调整模型配置。')
                                if data.get('usage'):yield {'type':'usage','usage':data['usage']}
                            elif kind == 'message_stop':finished=True
                        if text:
                            if not isinstance(text,str):raise ModelStreamError('模型文本片段格式有误。')
                            received=True;yield {'type':'text_delta','text':text}
                    if not finished:raise ModelStreamError('模型连接提前中断，已保留收到的回复。')
                    if not received:raise ModelStreamError('模型未返回文本，请检查模型和协议。')
    except (TimeoutError,httpx.TimeoutException):
        raise ModelStreamError('模型请求超时，已保留收到的回复。') from None
    except httpx.HTTPError:
        raise ModelStreamError('连接模型服务失败，已保留收到的回复。') from None


class AsyncRuntime:
    """One lazy daemon loop per ChatService; each run is a cancellable task."""
    def __init__(self):
        self._loop=None;self._thread=None;self._lock=threading.Lock()

    def submit(self,coro):
        with self._lock:
            if self._loop is None:
                ready=threading.Event()
                def worker():
                    loop=asyncio.new_event_loop();asyncio.set_event_loop(loop);self._loop=loop;ready.set();loop.run_forever()
                self._thread=threading.Thread(target=worker,name='omini-chat-async',daemon=True);self._thread.start();ready.wait()
        return asyncio.run_coroutine_threadsafe(coro,self._loop)

    def close(self):
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = None
            self._thread = None
        if loop:
            loop.call_soon_threadsafe(loop.stop)
            if thread: thread.join(timeout=5)
            if not loop.is_running(): loop.close()


class StreamRun:
    def __init__(self,run_id,owner,request_id):
        self.id=run_id;self.owner=owner;self.request_id=request_id
        self.queue=queue.Queue(maxsize=256);self.cancelled=threading.Event();self.finished=threading.Event()
        self.started=threading.Event()
        self.future=None

    async def emit(self,event,data):
        while not self.cancelled.is_set() or event in ('done','error'):
            try:self.queue.put_nowait({'event':event,'data':data});return
            except queue.Full:
                if self.cancelled.is_set():return
                await asyncio.sleep(.02)

    def events(self):
        try:
            while True:
                try:item=self.queue.get(timeout=10)
                except queue.Empty:
                    if self.finished.is_set():break
                    yield {'event':'heartbeat','data':{'run_id':self.id}};continue
                yield item
                if item['event'] in ('done','error'):break
        finally:
            if not self.finished.is_set():self.cancel()

    def cancel(self):
        if self.finished.is_set():return
        self.cancelled.set()
        if self.future is not None and self.started.is_set():self.future.cancel()

    def wait(self,timeout=5):
        return self.finished.wait(timeout)
