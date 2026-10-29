"""Ephemeral loopback handoff from the opt-in browser extension to native Qt."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import threading

from school_timetable import normalize_cookie


class SchoolSessionBridge:
    def __init__(self, connector_path):
        self.cookie = ''
        self.token = secrets.token_urlsafe(32)
        self.connector_path = Path(connector_path)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, code, body, kind='text/html; charset=utf-8'):
                self.send_response(code)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)

            def valid(self):
                return self.headers.get('Host') == owner.origin.removeprefix('http://') and self.path.startswith('/' + owner.token + '/')

            def do_GET(self):
                if not self.valid(): return self.reply(404, b'Not found')
                if self.path == '/' + owner.token + '/connector.zip':
                    return self.reply(200, owner.connector_path.read_bytes(), 'application/zip')
                if self.path != '/' + owner.token + '/': return self.reply(404, b'Not found')
                html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Omini 桌面教务连接</title>
<body style="font:16px/1.8 sans-serif;max-width:650px;margin:60px auto;padding:24px"><h1>连接桌面学校课表</h1><p>首次使用：<a href="connector.zip" download>下载教务连接扩展</a>并解压，在 Chrome / Edge 扩展管理中开启开发者模式，加载解压目录。</p><p>点击浏览器工具栏中的「Omini 教务连接」扩展图标，再点击「连接当前工具箱并登录」。完成学校登录后，返回桌面工具箱点击「读取课表」。</p><p id="status" role="status">等待浏览器连接…</p><form id="online-form"><input id="school-cookie" type="hidden"></form><div id="import-dialog" hidden></div>
<script>window.addEventListener('message',async event=>{if(event.source!==window||event.origin!==location.origin||event.data?.type!=='omini-school-session'||typeof event.data.cookie!=='string'||!event.data.cookie)return;try{const r=await fetch('session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cookie:event.data.cookie})});if(r.ok)document.querySelector('#status').textContent='已连接，Cookie 已自动填入桌面工具箱。';}catch(_){document.querySelector('#status').textContent='桌面连接已关闭，请返回工具箱重新打开登录。';}});window.postMessage({type:'omini-school-request'},location.origin);</script></body></html>'''
                self.reply(200, html.encode())

            def do_POST(self):
                if not self.valid() or self.path != '/' + owner.token + '/session' or self.headers.get('Origin') != owner.origin:
                    return self.reply(403, b'Forbidden')
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 20000: raise ValueError()
                    value = json.loads(self.rfile.read(length))
                    owner.cookie = normalize_cookie(value['cookie'])
                except (ValueError, KeyError, TypeError):
                    return self.reply(400, b'Invalid session')
                self.reply(200, b'{"ok":true}', 'application/json')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.url = self.origin + '/' + self.token + '/'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.cookie = ''
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
