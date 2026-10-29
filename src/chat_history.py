"""Account-isolated encrypted text conversations and request idempotency records."""
from __future__ import annotations
import copy
import hashlib
import json
import re
import secrets
import threading
import time

MAX_CONVERSATIONS=100
MAX_BYTES=50*1024*1024


class ConversationConflict(ValueError):
    status_code = 409


class HistoryQuota(ValueError):
    pass


def request_fingerprint(model,message):
    files=[]
    for item in message.get('attachments',[]):
        data=item.get('data','')
        files.append({'name':item.get('name'), 'digest':hashlib.sha256(data.encode()).hexdigest()})
    raw=json.dumps({'model':model,'content':message.get('content'),'attachments':files},sort_keys=True,ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class ChatHistory:
    def __init__(self,store):
        self.store=store;self.database=store.database;self.lock=threading.RLock()
        with self.database.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS chat_conversations (
                    user_id VARCHAR(64) NOT NULL, id VARCHAR(64) NOT NULL,
                    version INTEGER NOT NULL, status VARCHAR(20) NOT NULL,
                    created DOUBLE NOT NULL, updated DOUBLE NOT NULL,
                    stored_size INTEGER NOT NULL, body LONGBLOB NOT NULL,
                    PRIMARY KEY(user_id,id)
                );
                CREATE TABLE IF NOT EXISTS chat_stream_requests (
                    user_id VARCHAR(64) NOT NULL, request_id VARCHAR(128) NOT NULL,
                    conversation_id VARCHAR(64) NOT NULL, run_id VARCHAR(64) NOT NULL,
                    fingerprint VARCHAR(64) NOT NULL, status VARCHAR(20) NOT NULL,
                    created DOUBLE NOT NULL, updated DOUBLE NOT NULL,
                    body LONGBLOB NOT NULL, PRIMARY KEY(user_id,request_id)
                );
            ''')
        self.recover()

    def recover(self):
        """An interrupted process must not leave a conversation permanently locked."""
        with self.database.connect() as db:
            rows = db.execute("SELECT user_id, request_id, body FROM chat_stream_requests WHERE status='streaming'").fetchall()
        for row in rows:
            saved = self.store.decode(row['body'])
            self.finish(row['user_id'], row['request_id'], saved.get('partial', ''), 'cancelled', '上次服务中断，已保留已接收的回复。')

    def checkpoint(self, owner, request_id, text):
        with self.lock, self.database.connect() as db:
            db.execute("UPDATE chat_stream_requests SET body=?,updated=? WHERE user_id=? AND request_id=? AND status='streaming'",
                       (self.store.encode({'partial': text}), time.time(), owner, request_id))

    def _decode(self,row):
        return self.store.decode(row['body']) if row else None

    def _row(self,db,owner,identity):
        return db.execute('SELECT * FROM chat_conversations WHERE user_id=? AND id=?',(owner,identity)).fetchone()

    def _write(self,db,owner,conversation,previous,reserve=0):
        encoded=self.store.encode(conversation)
        used=db.execute('SELECT COALESCE(SUM(stored_size),0) AS n FROM chat_conversations WHERE user_id=?',(owner,)).fetchone()['n']
        old_size=previous['stored_size'] if previous else 0
        if used-old_size+len(encoded)+reserve>MAX_BYTES:raise HistoryQuota('聊天历史达到 50 MiB 上限，请删除部分会话后继续。')
        if previous:
            cursor=db.execute('UPDATE chat_conversations SET version=?,status=?,updated=?,stored_size=?,body=? '
                              'WHERE user_id=? AND id=? AND version=?',
                              (conversation['version'],conversation['status'],conversation['updated_at'],len(encoded),encoded,
                               owner,conversation['id'],previous['version']))
            if cursor.rowcount!=1:raise ConversationConflict('会话已在其他页面更新，请刷新后继续。')
        else:
            count=db.execute('SELECT COUNT(*) AS n FROM chat_conversations WHERE user_id=?',(owner,)).fetchone()['n']
            if count>=MAX_CONVERSATIONS:raise HistoryQuota('最多保存 100 个会话，请删除部分历史后继续。')
            db.execute('INSERT INTO chat_conversations VALUES (?,?,?,?,?,?,?,?)',
                       (owner,conversation['id'],conversation['version'],conversation['status'],conversation['created_at'],
                        conversation['updated_at'],len(encoded),encoded))

    def listing(self,owner):
        with self.database.connect() as db:
            rows=db.execute('SELECT * FROM chat_conversations WHERE user_id=? ORDER BY updated DESC',(owner,)).fetchall()
        conversations=[]
        for row in rows:
            item=self._decode(row);item.pop('messages',None);conversations.append(item)
        return {'conversations':conversations,'bytes_used':sum(r['stored_size'] for r in rows),'limit_bytes':MAX_BYTES}

    def get(self,owner,identity):
        with self.database.connect() as db:row=self._row(db,owner,identity)
        if not row:raise ValueError('会话不存在或已删除。')
        return self._decode(row)

    def find_request(self,owner,request_id):
        with self.database.connect() as db:
            row=db.execute('SELECT * FROM chat_stream_requests WHERE user_id=? AND request_id=?',(owner,request_id)).fetchone()
        return dict(row) if row else None

    def begin(self,owner,value,user_message,fingerprint,run_id):
        request_id=value['request_id'];identity=value.get('conversation_id');version=value.get('version',0)
        with self.lock,self.database.connect() as db:
            existing=db.execute('SELECT * FROM chat_stream_requests WHERE user_id=? AND request_id=?',(owner,request_id)).fetchone()
            if existing:raise ConversationConflict('此请求已提交，请读取原请求结果。')
            row=self._row(db,owner,identity) if identity else None
            if identity and not row:raise ValueError('会话不存在或已删除。')
            if row:
                if row['version']!=version:raise ConversationConflict('会话已在其他页面更新，请刷新后继续。')
                if row['status']=='streaming':raise ConversationConflict('此会话正在生成回复。')
                conversation=self._decode(row)
            else:
                if version not in (0,None):raise ConversationConflict('新会话版本应为 0。')
                now=time.time();conversation={'id':secrets.token_hex(16),'title':user_message['content'].strip()[:40] or '附件对话',
                    'model':value['model'],'version':0,'status':'idle','messages':[],'created_at':now,'updated_at':now}
            if len(conversation['messages'])+2>80:raise ValueError('当前会话已达 80 条消息，请新建对话。')
            previous=copy.deepcopy(conversation)
            conversation['messages'].append(dict(user_message,id=secrets.token_hex(16),status='complete'))
            conversation.update(model=value['model'],version=conversation['version']+1,status='streaming',updated_at=time.time())
            self._write(db,owner,conversation,row,reserve=256*1024)
            db.execute('INSERT INTO chat_stream_requests VALUES (?,?,?,?,?,?,?,?,?)',
                       (owner,request_id,conversation['id'],run_id,fingerprint,'streaming',time.time(),time.time(),self.store.encode({})))
            return conversation,previous

    def finish(self,owner,request_id,text,status,error=''):
        with self.lock,self.database.connect() as db:
            request=db.execute('SELECT * FROM chat_stream_requests WHERE user_id=? AND request_id=?',(owner,request_id)).fetchone()
            if not request:raise ValueError('聊天请求不存在。')
            row=self._row(db,owner,request['conversation_id'])
            if not row:return None
            conversation=self._decode(row)
            if request['status']!='streaming':return conversation
            if text:
                assistant_id=secrets.token_hex(16)
                conversation['messages'].append({'id':assistant_id,'role':'assistant','content':text,'status':status})
            else:assistant_id=None
            conversation.update(version=conversation['version']+1,status=status,updated_at=time.time())
            self._write(db,owner,conversation,row)
            body=self.store.encode({'error':error,'status':status,'assistant_id':assistant_id})
            db.execute('UPDATE chat_stream_requests SET status=?,updated=?,body=? WHERE user_id=? AND request_id=?',
                       (status,time.time(),body,owner,request_id))
            return conversation

    def rename(self,owner,identity,value):
        title=value.get('title');version=value.get('version')
        if not isinstance(title,str) or not title.strip() or len(title)>120:raise ValueError('会话标题应为 1 至 120 个字符。')
        with self.lock,self.database.connect() as db:
            row=self._row(db,owner,identity)
            if not row:raise ValueError('会话不存在或已删除。')
            if row['version']!=version:raise ConversationConflict('会话已更新，请刷新后重试。')
            if row['status']=='streaming':raise ConversationConflict('请在回复结束后修改标题。')
            conversation=self._decode(row);conversation.update(title=title.strip(),version=version+1,updated_at=time.time())
            self._write(db,owner,conversation,row);return conversation

    def delete(self,owner,identity,version=None):
        with self.lock,self.database.connect() as db:
            row=self._row(db,owner,identity)
            if not row:raise ValueError('会话不存在或已删除。')
            if version is not None and row['version']!=version:raise ConversationConflict('会话已更新，请刷新后重试。')
            if row['status']=='streaming':raise ConversationConflict('请先停止回复，再删除会话。')
            db.execute('DELETE FROM chat_stream_requests WHERE user_id=? AND conversation_id=?',(owner,identity))
            db.execute('DELETE FROM chat_conversations WHERE user_id=? AND id=?',(owner,identity))
