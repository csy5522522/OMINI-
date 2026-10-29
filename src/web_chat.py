"""Server-managed chat routes. Credentials never appear in public model lists."""
import json
import asyncio
import base64
import copy
import hashlib
import re
import secrets
import threading
import time

import requests
from api_members import validate_api_member, probe_api_member
from chat_attachments import prepare_messages, attachment
from chat_history import ChatHistory, ConversationConflict, HistoryQuota, request_fingerprint
from chat_runtime import AsyncRuntime, StreamRun, ToolRegistry, ChatBusy, ModelStreamError, stream_model, TOTAL_TIMEOUT, IDLE_TIMEOUT


class ChatService:
    def __init__(self, account_data):
        self.store = account_data
        self.database = account_data.database
        self.lock = threading.RLock()
        self.active = set()
        self.runs = {}
        self.runtime = AsyncRuntime()
        self.tools = ToolRegistry()
        with self.database.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS chat_providers (
                    id VARCHAR(64) PRIMARY KEY, config LONGBLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_requests (
                    id VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64) NOT NULL,
                    provider_id VARCHAR(64) NOT NULL, created DOUBLE NOT NULL,
                    success INTEGER NOT NULL
                );
            """)
        self.history = ChatHistory(account_data)

    def resolve_model(self, selection):
        if not isinstance(selection,str) or ':' not in selection:raise ValueError('请选择模型。')
        identity,alias=selection.split(':',1)
        config=self.get(identity)
        model=next((m['name'] for m in config['models'] if m['alias']==alias),None)
        if config['disabled'] or not model:raise ValueError('模型已停用或移除，请重新选择。')
        return identity,config,model

    @staticmethod
    def _new_message(value):
        messages=value.get('messages')
        if not isinstance(messages,list) or not messages or not isinstance(messages[-1],dict):raise ValueError('请发送用户消息。')
        message=messages[-1]
        if message.get('role')!='user':raise ValueError('请发送用户消息。')
        raw={'role':'user','content':message.get('content','')}
        if message.get('attachments'):
            raw['attachments']=[{'name':f.get('name'),'data':f.get('data')} if isinstance(f,dict) else f for f in message['attachments']]
        prepare_messages([raw],'openai')
        metadata=copy.deepcopy(raw)
        if raw.get('attachments'):
            metadata['attachments']=[]
            for file in raw['attachments']:
                validated=attachment(file)
                mime=validated['mime'] or ('application/vnd.openxmlformats-officedocument.wordprocessingml.document' if file['name'].lower().endswith('.docx') else 'text/plain')
                metadata['attachments'].append({'name':file['name'],'mime':mime,'size':validated['size'],
                    'sha256':hashlib.sha256(base64.b64decode(file['data'])).hexdigest()})
        return raw,metadata

    @staticmethod
    def _context(previous,incoming,new_message,protocol):
        """Canonical history wins; old attachment data is accepted only by hash."""
        context=[];old=previous['messages'];prefix=incoming[:-1]
        aligned=len(prefix)==len(old)
        for index,message in enumerate(old):
            current={'role':message['role'],'content':message['content']};files=[];missing=[]
            supplied=(prefix[index].get('attachments',[]) if aligned and isinstance(prefix[index],dict) else [])
            for meta in message.get('attachments',[]):
                match=None
                for candidate in supplied:
                    if not isinstance(candidate,dict) or candidate.get('name')!=meta['name'] or not isinstance(candidate.get('data'),str):continue
                    try:digest=hashlib.sha256(base64.b64decode(candidate['data'],validate=True)).hexdigest()
                    except ValueError:continue
                    if digest==meta['sha256']:match={'name':meta['name'],'data':candidate['data']};break
                if match:files.append(match)
                else:missing.append(meta['name'])
            if files:current['attachments']=files
            if missing:current['content']+='\n[历史附件 '+ '、'.join(missing)+' 未随本轮提供，需要重新上传原文件以读取其内容。]'
            context.append(current)
        context.append(new_message)
        return prepare_messages(context,protocol)

    def open_stream(self,user_id,value):
        if not isinstance(value,dict):raise ValueError('聊天请求格式有误。')
        request_id=value.get('request_id')
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',request_id):raise ValueError('请求标识格式有误。')
        if type(value.get('version',0)) is not int or value.get('version',0)<0:raise ValueError('会话版本格式有误。')
        if value.get('conversation_id') is not None and (not isinstance(value['conversation_id'],str) or not re.fullmatch('[a-f0-9]{32}',value['conversation_id'])):
            raise ValueError('会话标识格式有误。')
        raw,metadata=self._new_message(value)
        fingerprint=request_fingerprint(value.get('model'),raw)
        with self.lock:
            existing=self.history.find_request(user_id,request_id)
            if existing:
                if existing['fingerprint']!=fingerprint or (value.get('conversation_id') and value['conversation_id']!=existing['conversation_id']):
                    raise ConversationConflict('此请求标识已用于其他消息，请使用新的请求标识。')
                if existing['status']=='streaming':
                    if existing['run_id'] in self.runs:raise ChatBusy('此请求正在生成，请等待或停止原请求。')
                    self.history.finish(user_id,request_id,'','cancelled','上次运行已中断，请发送新的消息继续。')
                    existing=self.history.find_request(user_id,request_id)
                conversation=self.history.get(user_id,existing['conversation_id']);stored=self.store.decode(existing['body'])
                run=StreamRun(existing['run_id'],user_id,request_id)
                base={'run_id':run.id,'request_id':request_id,'conversation_id':conversation['id'],'version':conversation['version'],'replayed':True}
                run.queue.put({'event':'start','data':dict(base,conversation=conversation)})
                assistant=next((m for m in conversation['messages'] if m['id']==stored.get('assistant_id')),None)
                if assistant:run.queue.put({'event':'delta','data':{'text':assistant['content']}})
                event='done' if existing['status']=='complete' else 'error'
                run.queue.put({'event':event,'data':dict(base,conversation=conversation,status=existing['status'],error=stored.get('error',''),content=assistant['content'] if assistant else '')})
                run.finished.set();return run
            if user_id in self.active:raise ChatBusy('上一条消息正在生成，请稍后。')
            if len(self.active)>=2:raise ChatBusy('当前生成任务较多，请稍后重试。')
            identity,config,model=self.resolve_model(value.get('model'))
            # Validate canonical context before committing the user turn.
            previous=self.history.get(user_id,value['conversation_id']) if value.get('conversation_id') else {'messages':[]}
            prepared=self._context(previous,value['messages'],raw,config['protocol'])
            run=StreamRun(secrets.token_hex(16),user_id,request_id)
            conversation,_=self.history.begin(user_id,value,metadata,fingerprint,run.id)
            self.active.add(user_id);self.runs[run.id]=run
            run.queue.put({'event':'start','data':{'run_id':run.id,'request_id':request_id,'conversation_id':conversation['id'],
                'version':conversation['version'],'conversation':conversation}})
            run.future=self.runtime.submit(self._generate(run,identity,config,model,prepared))
            return run

    async def _generate(self,run,identity,config,model,messages):
        text='';status='error';error='';usage={};conversation=None;checkpoint=0
        run.started.set()
        try:
            if run.cancelled.is_set():raise asyncio.CancelledError()
            async for event in stream_model(config,model,messages,timeout=TOTAL_TIMEOUT,idle_timeout=IDLE_TIMEOUT):
                if run.cancelled.is_set():raise asyncio.CancelledError()
                if event['type']=='text_delta':
                    chunk=event['text']
                    remaining=32000-len(text)
                    if remaining<=0:raise ModelStreamError('回复达到长度上限，已保留生成内容。')
                    text+=chunk[:remaining]
                    if time.monotonic()-checkpoint >= 1:
                        self.history.checkpoint(run.owner,run.request_id,text)
                        checkpoint=time.monotonic()
                    await run.emit('delta',{'text':chunk[:remaining]})
                    if len(chunk)>remaining:raise ModelStreamError('回复达到长度上限，已保留生成内容。')
                elif event['type']=='usage':usage.update(event['usage'])
            status='complete'
        except asyncio.CancelledError:
            status='cancelled';error='已停止生成，已保留收到的回复。'
        except Exception as failure:
            error=str(failure) if isinstance(failure,ValueError) else '模型请求失败，已保留收到的回复。'
        finally:
            try:
                conversation=self.history.finish(run.owner,run.request_id,text,status,error)
                with self.database.connect() as db:
                    db.execute('INSERT INTO chat_requests SELECT ?, ?, ?, ?, ? FROM users WHERE id=?',
                        (run.id,run.owner,identity,time.time(),int(status=='complete'),run.owner))
            except Exception:
                status='error';error='保存聊天历史失败，请刷新后重试。'
            finally:
                with self.lock:self.active.discard(run.owner);self.runs.pop(run.id,None)
            data={'run_id':run.id,'request_id':run.request_id,'conversation_id':conversation['id'] if conversation else None,
                  'version':conversation['version'] if conversation else None,'status':status,'content':text,'conversation':conversation,'usage':usage}
            if error:data['error']=error
            await run.emit('done' if status=='complete' else 'error',data)
            run.finished.set()

    def stream(self,user_id,value):
        run=self.open_stream(user_id,value)
        try:yield from run.events()
        finally:run.cancel()

    def cancel(self,user_id,run_id):
        with self.lock:
            run=self.runs.get(run_id)
            if not run:
                with self.database.connect() as db:
                    row=db.execute('SELECT status FROM chat_stream_requests WHERE user_id=? AND run_id=?',(user_id,run_id)).fetchone()
                if row:return {'ok':True,'status':row['status']}
                raise ValueError('运行不存在或已结束。')
            if run.owner!=user_id:raise ValueError('运行不存在或已结束。')
            run.cancel()
        run.wait(5)
        return {'ok':True,'status':'cancelled' if run.finished.is_set() else 'cancelling'}

    def history_list(self,user_id):return self.history.listing(user_id)
    def close(self):
        with self.lock: runs = list(self.runs.values())
        for run in runs: run.cancel()
        for run in runs: run.wait(5)
        self.runtime.close()
    def history_get(self,user_id,identity):return {'conversation':self.history.get(user_id,identity)}
    def history_update(self,user_id,identity,value):return {'conversation':self.history.rename(user_id,identity,value)}
    def history_delete(self,user_id,identity,version=None):
        self.history.delete(user_id,identity,version);return {'ok':True}

    def listing(self, private=False):
        with self.database.connect() as db:
            rows = db.execute('SELECT id, config FROM chat_providers ORDER BY id').fetchall()
        result = []
        for row in rows:
            config = self.store.decode(row['config'])
            if private:
                result.append(dict(config, id=row['id'], api_key='', has_key=bool(config['api_key'])))
            elif not config['disabled']:
                result.extend({'id': row['id'] + ':' + m['alias'], 'label': config['label'] + ' / ' + m['alias']}
                              for m in config['models'])
        return result

    def config(self, value, require_models=True):
        value = dict(value)
        identity = value.pop('id', '')
        value.pop('has_key', None)
        if identity and not value.get('api_key'):
            value['api_key'] = self.get(identity)['api_key']
        config = validate_api_member(value, require_models=require_models)
        if config['protocol'] not in ('openai', 'responses', 'anthropic'):
            raise ValueError('请选择 Chat Completions、Responses 或 Anthropic Messages 协议。')
        return identity, config

    def get(self, identity):
        with self.database.connect() as db:
            row = db.execute('SELECT config FROM chat_providers WHERE id=?', (identity,)).fetchone()
        if not row:
            raise ValueError('模型线路不存在，请刷新列表。')
        return self.store.decode(row['config'])

    def save(self, value):
        with self.lock:
            value = dict(value)
            selected = value.pop('model_alias', None)
            if value.get('action') == 'delete_model':
                config = self.get(value.get('id'))
                remaining = [m for m in config['models'] if m['alias'] != selected]
                if len(remaining) == len(config['models']): raise ValueError('模型不存在，请刷新。')
                with self.database.connect() as db:
                    if remaining:
                        db.execute('UPDATE chat_providers SET config=? WHERE id=?',
                                   (self.store.encode(dict(config, models=remaining)), value['id']))
                    else: db.execute('DELETE FROM chat_providers WHERE id=?', (value['id'],))
                return {'ok': True}
            if value.get('action') == 'delete':
                with self.database.connect() as db:
                    db.execute('DELETE FROM chat_providers WHERE id=?', (value.get('id'),))
                return {'ok': True}
            identity, config = self.config(value)
            previous = None
            if identity:
                old = self.get(identity)
                if selected is not None:
                    if len(config['models']) != 1: raise ValueError('每张卡片配置一个模型。')
                    remaining = [m for m in old['models'] if m['alias'] != selected]
                    if len(remaining) == len(old['models']): raise ValueError('模型不存在，请刷新。')
                    if remaining:
                        previous = (self.store.encode(dict(old, models=remaining)), identity)
                        identity = ''
            identity = identity or secrets.token_hex(16)
            with self.database.connect() as db:
                if previous: db.execute('UPDATE chat_providers SET config=? WHERE id=?', previous)
                db.execute('DELETE FROM chat_providers WHERE id=?', (identity,))
                db.execute('INSERT INTO chat_providers VALUES (?, ?)', (identity, self.store.encode(config)))
            return {'id': identity}

    def discover(self, value):
        _, config = self.config({k:v for k,v in value.items() if k != 'model_alias'}, require_models=False)
        return probe_api_member(config)

    def send(self, user_id, value):
        selection = value.get('model', '')
        if not isinstance(selection, str) or ':' not in selection:
            raise ValueError('请选择模型。')
        identity, alias = selection.split(':', 1)
        config = self.get(identity)
        model = next((m['name'] for m in config['models'] if m['alias'] == alias), None)
        if config['disabled'] or not model:
            raise ValueError('模型已停用或移除，请重新选择。')
        messages = prepare_messages(value.get('messages'), config['protocol'])
        with self.lock:
            if user_id in self.active:
                raise ValueError('上一条消息正在生成，请稍后。')
            if len(self.active) >= 2: raise ChatBusy('当前生成任务较多，请稍后重试。')
            if len(self.active)>=2:raise ChatBusy('当前生成任务较多，请稍后重试。')
            self.active.add(user_id)
        ok = False
        try:
            headers = {'Authorization': 'Bearer ' + config['api_key']}
            if config['protocol'] == 'openai':
                path, payload = '/chat/completions', {'model': model, 'messages': messages, 'stream': False}
            elif config['protocol'] == 'responses':
                path, payload = '/responses', {'model': model, 'input': messages, 'stream': False, 'store': False}
            else:
                path, payload = '/v1/messages', {'model': model, 'messages': messages, 'max_tokens': 4096}
                headers = {'x-api-key': config['api_key'], 'anthropic-version': '2023-06-01'}
            with requests.Session() as session:
                session.trust_env = False
                with session.post(config['base_url'] + path, json=payload, headers=headers,
                                  timeout=(10, 120), allow_redirects=False) as response:
                    if not 200 <= response.status_code < 300:
                        raise ValueError(f'模型服务返回 HTTP {response.status_code}，请检查配置或稍后重试。')
                    data = response.json()
            if config['protocol'] == 'openai':
                content = data['choices'][0]['message']['content']
            elif config['protocol'] == 'responses':
                content = ''.join(part.get('text', '') for item in data.get('output', [])
                                  if item.get('type') == 'message' for part in item.get('content', [])
                                  if part.get('type') == 'output_text')
            else:
                content = ''.join(p.get('text', '') for p in data.get('content', []) if p.get('type') == 'text')
            if not isinstance(content, str) or not content.strip():
                raise ValueError('模型未返回文本，请检查模型是否支持文本对话。')
            ok = True
            return {'content': content}
        except requests.RequestException:
            raise ValueError('连接模型服务失败或超时，请稍后重试。') from None
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            raise ValueError('模型响应格式有误，请检查所选协议。') from None
        finally:
            with self.lock:
                self.active.discard(user_id)
            with self.database.connect() as db:
                db.execute('INSERT INTO chat_requests SELECT ?, ?, ?, ?, ? FROM users WHERE id=?',
                           (secrets.token_hex(16), user_id, identity, time.time(), int(ok), user_id))

    def overview(self):
        now = time.time()
        with self.database.connect() as db:
            def count(sql, args=()):
                return db.execute(sql, args).fetchone()['n']
            return {
                'users': count('SELECT COUNT(*) AS n FROM users'),
                'disabled': count('SELECT COUNT(*) AS n FROM account_profiles WHERE disabled=1'),
                'new_users': count('SELECT COUNT(*) AS n FROM users WHERE created_at>?', (now - 7*86400,)),
                'active_users': count('SELECT COUNT(DISTINCT user_id) AS n FROM sessions WHERE expires_at>?', (now,)),
                'memos': count('SELECT COUNT(*) AS n FROM memos WHERE deleted=0'),
                'requests': count('SELECT COUNT(*) AS n FROM chat_requests'),
                'today_requests': count('SELECT COUNT(*) AS n FROM chat_requests WHERE created>?', ((now + 8*3600)//86400*86400 - 8*3600,)),
                'successes': count('SELECT COUNT(*) AS n FROM chat_requests WHERE success=1'),
                'models': len(self.listing()),
            }
