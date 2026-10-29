import tempfile
from pathlib import Path
import unittest

import requests

from school_session_bridge import SchoolSessionBridge


class SessionBridgeTests(unittest.TestCase):
    def test_loopback_handoff_requires_secret_path_host_and_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'connector.zip'; file.write_bytes(b'fixture')
            bridge = SchoolSessionBridge(file)
            client = requests.Session(); client.trust_env = False
            try:
                page = client.get(bridge.url, timeout=3)
                self.assertEqual(page.status_code, 200)
                self.assertIn('no-store', page.headers['Cache-Control'])
                self.assertEqual(client.get(bridge.origin + '/wrong/', timeout=3).status_code, 404)
                payload = {'cookie':'JSESSIONID=FIXTURE'}
                target = bridge.url + 'session'
                self.assertEqual(client.post(target,json=payload,timeout=3).status_code,403)
                self.assertEqual(client.post(target,json=payload,headers={'Origin':'https://other.example'},timeout=3).status_code,403)
                self.assertEqual(bridge.cookie, '')
                self.assertEqual(client.post(target,json=payload,headers={'Origin':bridge.origin},timeout=3).status_code,200)
                self.assertEqual(bridge.cookie,'JSESSIONID=FIXTURE')
                self.assertEqual(client.post(target,json={'cookie':'a=b\r\nInjected: true'},headers={'Origin':bridge.origin},timeout=3).status_code,400)
                self.assertEqual(bridge.cookie,'JSESSIONID=FIXTURE')
            finally:
                bridge.close(); client.close()
            self.assertEqual(bridge.cookie,'')
