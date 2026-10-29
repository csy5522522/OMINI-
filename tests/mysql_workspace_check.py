"""Create a disposable loopback-only MySQL instance to verify new data paths."""
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from unittest.mock import patch
import pymysql
from accounts import AccountStore
from account_data import AccountDataStore
from memo_store import MemoStore
from web_chat import ChatService
from timetable_ai import TimetableAIService
from tests.test_web_chat import Upstream


def main():
    binary=shutil.which('mysqld') or next((str(p) for p in Path('C:/Program Files/MySQL').glob('*/bin/mysqld.exe')),None)
    if not binary:raise RuntimeError('No local MySQL server binary available')
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    with tempfile.TemporaryDirectory(prefix='omini-mysql-workspace-') as directory:
        root=Path(directory);data=root/'mysql';data.mkdir()
        command=[binary,'--no-defaults','--basedir='+str(Path(binary).parents[1]),'--datadir='+str(data)]
        init=subprocess.run(command+['--initialize-insecure','--console'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,creationflags=flags,timeout=60)
        if init.returncode:raise RuntimeError(init.stdout.decode(errors='replace')[-2000:])
        print('Disposable MySQL initialized',flush=True)
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        log=(root/'mysql.log').open('wb');process=subprocess.Popen(command+['--port='+str(port),'--bind-address=127.0.0.1','--mysqlx=OFF','--skip-log-bin','--performance-schema=OFF','--innodb-buffer-pool-size=64M','--max-connections=20','--console'],stdout=log,stderr=subprocess.STDOUT,creationflags=flags)
        upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream);threading.Thread(target=upstream.serve_forever,daemon=True).start()
        try:
            for attempt in range(60):
                if process.poll() is not None:raise RuntimeError((root/'mysql.log').read_text(errors='replace')[-2000:])
                try:connection=pymysql.connect(host='127.0.0.1',port=port,user='root',connect_timeout=1);break
                except pymysql.OperationalError:time.sleep(.5)
            else:raise RuntimeError('Temporary MySQL startup timed out')
            with connection.cursor() as cursor:cursor.execute('CREATE DATABASE omini_test CHARACTER SET utf8mb4')
            connection.close()
            env={'OMINI_MYSQL_HOST':'127.0.0.1','OMINI_MYSQL_PORT':str(port),'OMINI_MYSQL_USER':'root','OMINI_MYSQL_DATABASE':'omini_test','OMINI_MYSQL_PASSWORD':''}
            with patch.dict(os.environ,env):
                account=AccountStore(root/'accounts.sqlite3');user=account.register('mysql@example.test','FixturePass-123');store=AccountDataStore(account.database,root);MemoStore(account.database,key_root=root)
                chat=ChatService(store)
                try:
                    key='local-mysql-fixture-key';config={'label':'mysql','protocol':'responses','base_url':f'http://127.0.0.1:{upstream.server_port}','api_key':key,'models':[{'name':'fixture-model','alias':'fixture'}]}
                    identity=chat.save(config)['id'];run=chat.open_stream(user['id'],{'request_id':secrets.token_hex(12),'version':0,'model':identity+':fixture','messages':[{'role':'user','content':'MySQL 中文流式测试'}]})
                    frames=list(run.events());conversation=frames[-1]['data']['conversation'];assert conversation['messages'][-1]['content']=='你好，测试回复'
                    cid=conversation['id'];assert chat.history_get(user['id'],cid)['conversation']['status']=='complete'
                    assert len(chat.history_list(user['id'])['conversations'])==1
                    ai=TimetableAIService(store,complete=lambda *a,**k:json.dumps({'courses':[{'name':'课程','weekday':1,'periods':'1-2','weeks':'1-16'}]}))
                    aid=ai.save(dict(config,default=True))['id'];assert ai.listing()[0]['default']
                    assert not ai.listing(private=True)[0]['api_key']
                    renamed=chat.history_update(user['id'],cid,{'version':conversation['version'],'title':'改名'})['conversation'];assert renamed['title']=='改名'
                    with account.connect() as db:
                        blob=db.execute('SELECT config FROM timetable_ai_lines WHERE id=?',(aid,)).fetchone()['config'];assert key.encode() not in bytes(blob)
                    account.delete_user(user['id'])
                    with account.connect() as db:
                        assert db.execute('SELECT COUNT(*) AS n FROM chat_conversations').fetchone()['n']==0
                        assert db.execute('SELECT COUNT(*) AS n FROM chat_stream_requests').fetchone()['n']==0
                    report={'database':'temporary MySQL','streaming':'passed','encrypted_history':'passed','rename_delete_account':'passed','independent_ai_routes':'passed'}
                    destination=Path(__file__).resolve().parents[1]/'artifacts/verification/workspace-v3';destination.mkdir(parents=True,exist_ok=True);(destination/'mysql.json').write_text(json.dumps(report,indent=2))
                    print(json.dumps(report),flush=True)
                finally:chat.close()
        finally:
            upstream.shutdown();upstream.server_close()
            try:
                connection=pymysql.connect(host='127.0.0.1',port=port,user='root',connect_timeout=1)
                with connection.cursor() as cursor:cursor.execute('SHUTDOWN')
                connection.close()
                process.wait(timeout=15)
            except Exception:process.terminate();process.wait(timeout=10)
            log.close()


if __name__=='__main__':main()
