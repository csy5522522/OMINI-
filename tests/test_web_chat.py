"""Real HTTP coverage of chat protocols, credentials, permissions and user lifecycle."""
import json
import base64
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
import requests
from account_client import AccountClient
import tests.test_memos as fixtures


class Upstream(BaseHTTPRequestHandler):
    calls = []
    def log_message(self, *args): pass
    def reply(self, data):
        raw = json.dumps(data).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self): self.reply({'data': [{'id': 'fixture-model'}, {'id': 'second-model'}]})
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.calls.append((self.path, data, dict(self.headers)))
        if '课表识别器' in json.dumps(data,ensure_ascii=False):
            content=json.dumps({'courses':[{'name':'高等数学','weekday':1,'periods':'1-2','weeks':'','teacher':'张老师','location':'教学楼101'}],'warnings':['原图未标注周次']},ensure_ascii=False)
            if self.path.endswith('/chat/completions'):self.reply({'choices':[{'message':{'content':content}}]})
            elif self.path.endswith('/responses'):self.reply({'output':[{'type':'message','content':[{'type':'output_text','text':content}]}]})
            else:self.reply({'content':[{'type':'text','text':content}]})
            return
        if data.get('stream'):
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
            messages=data.get('messages',data.get('input',[]));slow=any('SLOW' in str(m) for m in messages[-1:])
            try:
                for part in (['你好，','测试回复'] if not slow else ['部分回复','稍后继续']):
                    if self.path.endswith('/chat/completions'): event={'choices':[{'delta':{'content':part},'finish_reason':None}]}
                    elif self.path.endswith('/responses'): event={'type':'response.output_text.delta','delta':part}
                    else:event={'type':'content_block_delta','delta':{'type':'text_delta','text':part}}
                    self.wfile.write(('data: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode()); self.wfile.flush();time.sleep(2 if slow else .12)
                if self.path.endswith('/chat/completions'):event={'choices':[{'delta':{},'finish_reason':'stop'}]}
                elif self.path.endswith('/responses'):event={'type':'response.completed'}
                else:event={'type':'message_stop'}
                self.wfile.write(('data: '+json.dumps(event)+'\n\n').encode());self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
            return
        if self.path.endswith('/chat/completions'):
            self.reply({'choices': [{'message': {'content': '你好，测试回复'}}]})
        elif self.path.endswith('/responses'):
            self.reply({'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '你好，测试回复'}]}]})
        else: self.reply({'content': [{'type': 'text', 'text': '你好，测试回复'}]})


class WebChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.MemoHTTPTests.setUpClass.__func__(cls)
        cls.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        cls.worker = threading.Thread(target=cls.upstream.serve_forever, daemon=True); cls.worker.start()
        cls.base = f'http://127.0.0.1:{cls.upstream.server_port}'
    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown(); cls.upstream.server_close(); cls.worker.join()
        fixtures.MemoHTTPTests.tearDownClass.__func__(cls)

    def test_protocols_configuration_permissions_and_cleanup(self):
        admin = AccountClient(self.url); admin_user=admin.request('login', {'email':'admin', 'password':'admin123'})['user']
        created = admin.api('/api/admin/users', {'email':'chat@example.test','password':'FixturePass-123'})['user']
        member = AccountClient(self.url); member.request('login', {'email':created['email'],'password':'FixturePass-123'})
        headers = {'X-Omini-Client': 'web'}
        with requests.Session() as session:
            session.trust_env = False
            self.assertEqual(session.get(self.url+'/api/chat/models').status_code, 401)
            session.cookies.set('omini_session', member.token)
            for endpoint in ('/api/admin/overview', '/api/admin/chat/providers', '/api/admin/users'):
                self.assertEqual(session.get(self.url+endpoint).status_code, 403)
                self.assertEqual(session.post(self.url+endpoint,json={},headers=headers).status_code,403)
        for protocol in ('openai','responses','anthropic'):
            config = {'label':protocol,'protocol':protocol,'base_url':self.base,'api_key':'fixture-secret-key',
                      'models':[{'name':'fixture-model','alias':'friendly'}]}
            discovery = admin.api('/api/admin/chat/models',config)
            self.assertEqual(discovery['models'],['fixture-model','second-model'])
            identity = admin.api('/api/admin/chat/providers',config)['id']
            private = next(p for p in admin.api('/api/admin/chat/providers')['providers'] if p['id']==identity)
            self.assertEqual(private['api_key'],''); self.assertTrue(private['has_key'])
            public = member.api('/api/chat/models')
            self.assertNotIn('fixture-secret-key', json.dumps(public)); self.assertNotIn('base_url',json.dumps(public))
            selection=identity+':friendly'
            messages=[{'role':'user','content':'你好'},{'role':'assistant','content':'你好'},{'role':'user','content':'继续'}]
            self.assertEqual(member.api('/api/chat/send',{'model':selection,'messages':messages})['content'],'你好，测试回复')
            self.assertEqual(Upstream.calls[-1][1]['model'],'fixture-model')
            self.assertEqual(Upstream.calls[-1][1]['input' if protocol=='responses' else 'messages'],messages)
            with_files=[{'role':'user','content':'分析文档','attachments':[{'name':'notes.txt','data':base64.b64encode('附件里的内容'.encode()).decode()}]}]
            self.assertEqual(member.api('/api/chat/send',{'model':selection,'messages':with_files})['content'],'你好，测试回复')
            forwarded=Upstream.calls[-1][1]['input' if protocol=='responses' else 'messages']
            self.assertIn('附件里的内容',forwarded[0]['content'])
            admin.api('/api/admin/chat/providers',{**private,'disabled':True})
            self.assertNotIn(selection,[m['id'] for m in member.api('/api/chat/models')['models']])
            with self.assertRaises(ValueError): member.api('/api/chat/send',{'model':selection,'messages':messages})
            admin.api('/api/admin/chat/providers',{'id':identity,'action':'delete'})
        self.assertNotIn(b'fixture-secret-key',(self.root/'server'/'accounts.sqlite3').read_bytes())
        overview=admin.api('/api/admin/overview'); self.assertEqual(overview['requests'],6);self.assertEqual(overview['successes'],6)
        config={'label':'legacy','protocol':'openai','base_url':self.base,'api_key':'fixture-secret-key',
                'models':[{'name':'one','alias':'one'},{'name':'two','alias':'two'}]}
        legacy=admin.api('/api/admin/chat/providers',config)['id']
        updated=admin.api('/api/admin/chat/providers',{**config,'id':legacy,'api_key':'','model_alias':'one','models':[{'name':'one','alias':'one'}],'disabled':True})['id']
        available=member.api('/api/chat/models')['models']
        self.assertIn(legacy+':two',[m['id'] for m in available]);self.assertNotIn(updated+':one',[m['id'] for m in available])
        admin.api('/api/admin/chat/providers',{'action':'delete_model','id':updated,'model_alias':'one'})
        self.assertIn(legacy+':two',[m['id'] for m in member.api('/api/chat/models')['models']])
        admin.api('/api/admin/chat/providers',{'action':'delete_model','id':legacy,'model_alias':'two'})
        member.api('/api/sync/schedule',{'revision':0,'data':{}})
        member.api('/api/memos',{'title':'private note','content':'body'})
        admin.api('/api/admin/user',{'id':created['id'],'email':'updated@example.test'})
        with self.assertRaises(ValueError): member.api('/api/chat/models')
        member.request('login',{'email':'updated@example.test','password':'FixturePass-123'})
        with self.assertRaises(ValueError): admin.api('/api/admin/user',{'id':admin_user['id'],'action':'delete'})
        admin.api('/api/admin/user',{'id':created['id'],'action':'delete'})
        self.assertNotIn(created['id'],[u['id'] for u in admin.api('/api/admin/users')['users']])
        self.assertEqual(admin.api('/api/admin/overview')['memos'],0)
        with self.assertRaises(ValueError): member.api('/api/chat/models')


if __name__ == '__main__': unittest.main()
