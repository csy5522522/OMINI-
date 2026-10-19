"""Backend integration checks use a temporary store and a local mock Ollama."""
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
import base64
import io
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xuanwu_ai import app


class MockOllama(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        blob = json.dumps({"models": [{"name": "fixture:1b"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == '/api/show':
            capabilities = ['completion'] if payload['model']=='no-tools' else ['completion','tools','vision']
            blob = json.dumps({'capabilities':capabilities}).encode()
            self.send_response(200)
            self.send_header('Content-Length',str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            return
        self.server.received = payload
        if payload["model"] == "missing":
            blob = b'{"error":"model missing"}'
            self.send_response(404)
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            return
        self.send_response(200)
        self.end_headers()
        if payload['model']=='agent' and payload['messages'][-1]['role']!='tool':
            calls=[{'function':{'name':'create_directory','arguments':{'path':'created'}}},
                   {'function':{'name':'write_file','arguments':{'path':'created/hello.txt','content':'玄武工具测试'}}}]
            self.wfile.write((json.dumps({'message':{'tool_calls':calls},'done':True})+'\n').encode())
            self.wfile.flush()
            return
        try:
            self.wfile.write((json.dumps({"message": {"thinking": "思考"}}, ensure_ascii=False)+"\n").encode())
            self.wfile.flush()
            if payload["model"] == "slow":
                self.server.release.wait(5)
            for event in [
                {"message": {"content": "你好"}},
                {"message": {"content": "，玄武"}},
                {"done": True, "eval_count": 4, "eval_duration": 2_000_000_000},
            ]:
                self.wfile.write((json.dumps(event, ensure_ascii=False)+"\n").encode())
                self.wfile.flush()
        except (OSError, ValueError):
            pass


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.mock = ThreadingHTTPServer(("127.0.0.1", 0), MockOllama)
        self.mock.daemon_threads = True
        self.mock.release = threading.Event()
        threading.Thread(target=self.mock.serve_forever, daemon=True).start()
        self.server = app.create_server(Path(self.temp.name))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.client = requests.Session()
        self.client.trust_env = False
        token = self.client.get(self.url + "/api/bootstrap", timeout=5).json()["token"]
        self.client.headers["X-Xuanwu-Token"] = token
        settings = dict(app.DEFAULTS, endpoint=f"http://127.0.0.1:{self.mock.server_address[1]}", model="fixture:1b")
        self.client.post(self.url + "/api/settings", json=settings, timeout=5).raise_for_status()

    def tearDown(self):
        self.mock.release.set()
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.mock.shutdown()
        self.mock.server_close()
        self.temp.cleanup()

    def test_models_and_unicode_persistence(self):
        self.assertEqual(self.client.get(self.url+"/api/models").json()["models"][0]["name"], "fixture:1b")
        session = {"id":"test", "title":"中文对话", "messages":[{"role":"user","content":"你好"}]}
        self.client.post(self.url+"/api/session", json=session).raise_for_status()
        reloaded = app.Store(Path(self.temp.name)).snapshot()
        self.assertEqual(reloaded["sessions"][0]["messages"][0]["content"], "你好")
        self.client.post(self.url+"/api/delete",json={"id":"test"}).raise_for_status()
        self.assertEqual(app.Store(Path(self.temp.name)).snapshot()["sessions"], [])

    def test_bundled_ui_and_native_folder_picker_route(self):
        page=self.client.get(self.url+"/",timeout=5)
        self.assertEqual(page.status_code,200)
        self.assertIn("text/html",page.headers["Content-Type"])
        self.assertIn("玄武".encode("utf-8"),page.content)
        css=self.client.get(self.url+"/assets/index-D3EjhvSI.css",timeout=5)
        self.assertEqual(css.status_code,200)
        self.assertIn("text/css",css.headers["Content-Type"])
        self.server.runtime.folder_picker=lambda: "C:\\fixture\\workspace"
        response=self.client.post(self.url+"/api/pick-folder",json={},timeout=5)
        self.assertEqual(response.json(),{"path":"C:\\fixture\\workspace"})

    def test_stream_and_system_context(self):
        response = self.client.post(self.url+"/api/chat",json={"id":"stream","model":"fixture:1b","messages":[{"role":"user","content":"测试"}]})
        events = [json.loads(line) for line in response.content.splitlines()]
        self.assertEqual(events[0]["type"], "thinking")
        self.assertEqual("".join(e["text"] for e in events if e["type"]=="content"), "你好，玄武")
        self.assertEqual(events[-1]["speed"], 2.0)
        self.assertEqual(self.mock.received["messages"][0]["role"], "system")
        self.assertEqual(self.mock.received["messages"][1]["content"], "测试")

    def test_cancel_interrupts_blocked_upstream(self):
        response = self.client.post(self.url+"/api/chat",json={"id":"cancel","model":"slow","messages":[{"role":"user","content":"测试"}]},stream=True,timeout=5)
        iterator = response.iter_lines(chunk_size=1)
        self.assertEqual(json.loads(next(iterator))["type"], "thinking")
        self.client.post(self.url+"/api/stop",json={"id":"cancel"},timeout=2).raise_for_status()
        deadline = time.monotonic()+1.5
        while self.server.runtime.jobs and time.monotonic()<deadline:
            time.sleep(.02)
        self.assertFalse(self.server.runtime.jobs, "cancel must unblock upstream read")
        response.close()

    def test_auth_origin_and_local_endpoint_validation(self):
        self.assertEqual(requests.get(self.url+"/api/models",timeout=5).status_code,403)
        self.assertEqual(self.client.post(self.url+"/api/stop",json={"id":"x"},headers={"Origin":"https://example.com"}).status_code,403)
        self.assertEqual(self.client.get(self.url+"/api/bootstrap",headers={"Host":"example.com"}).status_code,403)
        invalid = dict(app.DEFAULTS, endpoint="https://example.com")
        self.assertEqual(self.client.post(self.url+"/api/settings",json=invalid).status_code,400)

    def test_error_and_invalid_message(self):
        response = self.client.post(self.url+"/api/chat",json={"model":"missing","messages":[{"role":"user","content":"测试"}]})
        self.assertIn("model missing",response.json()["text"])
        response = self.client.post(self.url+"/api/chat",json={"model":"fixture:1b","messages":[{"role":"system","content":"测试"}]})
        self.assertEqual(response.status_code,400)

    def test_agent_executes_real_files_and_returns_tool_results(self):
        current = self.server.runtime.store.snapshot()["settings"]
        self.client.post(self.url+"/api/settings",json={**current,"permission_mode":"full"}).raise_for_status()
        response=self.client.post(self.url+'/api/chat',json={'model':'agent','messages':[{'role':'user','content':'创建文件'}]},timeout=10)
        events=[json.loads(line) for line in response.content.splitlines()]
        tools=[e for e in events if e['type']=='tool' and e['status']=='done']
        self.assertEqual(len(tools),2)
        self.assertEqual((Path(self.temp.name)/'workspace/created/hello.txt').read_text(encoding='utf-8'),'玄武工具测试')
        self.assertEqual(events[-1]['type'],'done')
        self.assertEqual(self.mock.received['messages'][-1]['role'],'tool')
        self.assertTrue(json.loads(self.mock.received['messages'][-1]['content'])['ok'])

    def test_approve_mode_waits_for_each_tool_decision(self):
        response=self.client.post(self.url+'/api/chat',json={'id':'approval','model':'agent','messages':[{'role':'user','content':'创建文件'}]},stream=True,timeout=10)
        events=[]
        for raw in response.iter_lines(chunk_size=1):
            if not raw:
                continue
            event=json.loads(raw)
            events.append(event)
            if event['type']=='approval_required':
                result=self.client.post(self.url+'/api/approval',json={
                    'id':event['request_id'],'approval_id':event['id'],'approved':True
                },timeout=5)
                result.raise_for_status()
            if event['type']=='done':
                break
        response.close()
        approvals=[event for event in events if event['type']=='approval_required']
        tools=[event for event in events if event['type']=='tool' and event['status']=='done']
        self.assertEqual(len(approvals),2)
        self.assertEqual(len(tools),2)

    def test_tools_path_boundaries_backup_move_and_trash(self):
        from xuanwu_ai.local_tools import FileTools
        root=Path(self.temp.name)/'workspace'
        tools=FileTools(root,Path(self.temp.name))
        self.assertFalse(tools.execute('write_file',{'path':'../outside.txt','content':'x'})['ok'])
        self.assertFalse(tools.execute('write_file',{'path':'noteC:stream','content':'x'})['ok'])
        self.assertFalse(tools.execute('move_path',{'source':'.','destination':'moved'})['ok'])
        self.assertTrue(tools.execute('write_file',{'path':'中文.txt','content':'旧版本'})['ok'])
        result=tools.execute('write_file',{'path':'中文.txt','content':'新版本'})
        self.assertEqual(Path(result['backup']).read_text(encoding='utf-8'),'旧版本')
        self.assertTrue(tools.execute('move_path',{'source':'中文.txt','destination':'归档/中文.txt'})['ok'])
        self.assertTrue(tools.execute('search_files',{'query':'新版本'})['matches'])
        result=tools.execute('trash_file',{'path':'归档/中文.txt'})
        self.assertTrue(result['ok'])
        self.assertFalse((root/'归档/中文.txt').exists())
        self.assertEqual(Path(result['backup']).read_text(encoding='utf-8'),'新版本')

    def test_attachments_text_docx_image_and_invalid_id(self):
        def upload(name,raw):
            result=self.client.post(self.url+'/api/attachment',json={'name':name,'data':base64.b64encode(raw).decode()},timeout=5)
            result.raise_for_status()
            return result.json()
        text=upload('资料.txt','这份附件的标记是玄武。'.encode())
        document=io.BytesIO()
        with zipfile.ZipFile(document,'w') as z:
            z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Word测试</w:t></w:r></w:p></w:body></w:document>')
        word=upload('文件.docx',document.getvalue())
        from pypdf import PdfWriter
        from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject
        writer=PdfWriter()
        page=writer.add_blank_page(300,300)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
        stream=DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 20 200 Td (PDF-MARKER-17) Tj ET')
        page[NameObject('/Contents')]=writer._add_object(stream)
        pdf_buffer=io.BytesIO()
        writer.write(pdf_buffer)
        pdf=upload('资料.pdf',pdf_buffer.getvalue())
        picture=upload('像素.png',base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg=='))
        response=self.client.post(self.url+'/api/chat',json={'model':'fixture:1b','messages':[{'role':'user','content':'总结','attachments':[text['id'],word['id'],pdf['id'],picture['id']]}]},timeout=5)
        self.assertEqual(json.loads(response.content.splitlines()[-1])['type'],'done')
        content=self.mock.received['messages'][-1]['content']
        self.assertIn('标记是玄武',content)
        self.assertIn('Word测试',content)
        self.assertIn('PDF-MARKER-17',content)
        self.assertEqual(len(self.mock.received['messages'][-1]['images']),1)
        invalid=self.client.post(self.url+'/api/chat',json={'model':'fixture:1b','messages':[{'role':'user','content':'x','attachments':['../../data']}]})
        self.assertEqual(invalid.status_code,400)

    def test_theme_collapse_persistence_and_unsupported_tools(self):
        settings=self.server.runtime.store.snapshot()['settings']
        response=self.client.post(self.url+'/api/settings',json={**settings,'theme':'light','sidebar_collapsed':True})
        self.assertEqual(response.status_code,200)
        saved=app.Store(Path(self.temp.name)).snapshot()['settings']
        self.assertEqual(saved['theme'],'light')
        self.assertTrue(saved['sidebar_collapsed'])
        response=self.client.post(self.url+'/api/chat',json={'model':'no-tools','messages':[{'role':'user','content':'test'}]})
        self.assertIn('tools',response.json()['text'])
        self.assertFalse(self.client.get(self.url+'/api/speech/status').json()['active'])


if __name__ == "__main__":
    unittest.main()
