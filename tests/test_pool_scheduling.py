"""Scheduling maps, member priorities and real pinned gateway updates."""
import copy
import unittest
from unittest.mock import patch

import ai_assistants as core
import tests.test_ai_assistants as gateway


class SchedulingTests(unittest.TestCase):
    def test_balanced_disables_affinity_and_member_validation(self):
        pool = core.default_settings()['pool']; pool['strategy']='balanced'
        self.assertEqual(core.routing_config(pool),{'strategy':'round-robin','session-affinity':False})
        pool['strategy']='weighted-round-robin'
        self.assertEqual(core.routing_config(pool)['strategy'],'weighted-round-robin')
        for weight in (0,-1,True,1.5,1000001):
            with self.assertRaises(ValueError): core.member_rule({'member_rules':{'a':{'weight':weight}}},{'name':'a'})

    def test_remembered_priority_survives_engine_neutralization(self):
        pool = core.default_settings()['pool']
        pool = core.remember_member_rules(pool,[{'name':'a','priority':30,'weight':2}])
        self.assertEqual(core.member_rule(pool,{'name':'a','priority':0})['priority'],30)

    def test_partial_field_failure_rolls_back_and_leaves_prior_routing(self):
        pool = core.default_settings()['pool']; pool['strategy']='balanced'
        rows = [{'name':'a','priority':10,'weight':1},{'name':'b','priority':20,'weight':1}]
        previous = {'strategy':'fill-first','session-affinity':True}
        client = core.PoolClient(pool); calls=[]
        def request(method,path,**kwargs):
            calls.append((method,path,kwargs.get('json')))
            if method=='GET': return previous
            if path=='/credentials/fields' and kwargs['json']=={'name':'b','priority':0,'weight':1}:
                raise ValueError('fixture write failure')
            return {'status':'ok'}
        with patch.object(client,'request',side_effect=request):
            with self.assertRaisesRegex(ValueError,'fixture'): client.apply_scheduling(pool,rows)
        self.assertIn(('PATCH','/credentials/fields',{'name':'a','priority':10,'weight':1}),calls)
        self.assertIn(('PUT','/config/routing',previous),calls)


class LiveSchedulingTests(gateway.LiveGatewayTests):
    def test_native_response_distribution_for_balanced_weighted_and_priority(self):
        from collections import Counter
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import json
        import threading
        import requests
        seen=[]
        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                seen.append(self.headers.get('Authorization'))
                body=json.dumps({'id':'chatcmpl-test','object':'chat.completion','created':1700000000,
                    'model':'fixture-model','choices':[{'index':0,'message':{'role':'assistant','content':'fixture reply'},'finish_reason':'stop'}],
                    'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}).encode()
                self.send_response(200); self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self,*_): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        provider={'name':'local-fixture','base-url':f'http://127.0.0.1:{server.server_port}/v1',
                  'keys':[{'api-key':'member-A','weight':1},{'api-key':'member-B','weight':3}],
                  'models':[{'name':'fixture-model','alias':'fixture-model'}]}
        self.config['api-keys']={'openai-compatibility':[provider]}
        try:
            self.start()
            with requests.Session() as session:
                session.trust_env=False
                def send(count):
                    seen.clear()
                    for _ in range(count):
                        response=session.post(self.client.base+'/v1/responses',headers={
                            'Authorization':'Bearer '+self.pool['api_key'],'X-Session-ID':'same-conversation'},
                            json={'model':'fixture-model','input':[{'role':'user','content':'test'}]},timeout=10)
                        self.assertEqual(response.status_code,200,response.text)
                    return Counter(seen)
                pool=copy.deepcopy(self.pool); pool['strategy']='balanced'
                self.client.apply_scheduling(pool)
                counts=send(8); self.assertEqual(sorted(counts.values()),[4,4])
                pool['strategy']='weighted-round-robin'; pool['session_affinity']=False
                self.client.apply_scheduling(pool)
                counts=send(8); self.assertEqual(counts['Bearer member-A'],2); self.assertEqual(counts['Bearer member-B'],6)
                first=copy.deepcopy(provider); first.update(name='local-first',keys=[{'api-key':'member-A'}])
                preferred=copy.deepcopy(provider); preferred.update(name='local-preferred',keys=[{'api-key':'member-B'}],priority=100)
                self.client.request('PUT','/config/api-keys/openai-compatibility',json=[first,preferred])
                pool['strategy']='fill-first'; self.client.apply_scheduling(pool)
                counts=send(3); self.assertEqual(counts,{'Bearer member-B':3})
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_priority_weight_and_balanced_live_updates_preserve_credentials(self):
        self.start()
        for index in range(2):
            source = self.root/f'member-{index}.json'
            core.atomic_json(source,{'type':'codex','email':f'member-{index}@example.com',
                'account_id':f'account-{index}','access_token':f'fixture-access-{index}',
                'refresh_token':f'fixture-refresh-{index}','expired':'2099-01-01T00:00:00Z','last_refresh':'2099-01-01T00:00:00Z'})
            self.client.import_account(source)
        rows=self.client.accounts()
        pool=core.remember_member_rules(self.pool,rows)
        preferred=rows[1]['name']
        pool['member_rules'][preferred]={'priority':100,'weight':3}
        pool['strategy']='fill-first'
        applied=self.client.apply_scheduling(pool)
        self.assertEqual(next(row for row in applied if row['name']==preferred)['priority'],100)
        self.assertEqual(self.client.request('GET','/config/routing')['strategy'],'fill-first')
        pool['strategy']='balanced'
        applied=self.client.apply_scheduling(pool)
        self.assertTrue(all(row.get('priority',0)==0 for row in applied))
        routing=self.client.request('GET','/config/routing')
        self.assertEqual(routing['strategy'],'round-robin'); self.assertFalse(routing['session-affinity'])
        self.assertEqual(core.member_rule(pool,{'name':preferred})['priority'],100)
        pool['strategy']='weighted-round-robin'
        applied=self.client.apply_scheduling(pool)
        self.assertEqual(self.client.request('GET','/config/routing')['strategy'],'weighted-round-robin')
        self.assertEqual(next(row for row in applied if row['name']==preferred)['weight'],3)
        import json
        for row in applied:
            raw=json.loads((self.root/'auths'/row['name']).read_text(encoding='utf-8'))
            self.assertTrue(raw['access_token'].startswith('fixture-access-'))
