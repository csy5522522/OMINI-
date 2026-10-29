from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest

from school_login import SchoolLoginService, LoginFlow, checked_url, school_redirect
from tests.school_login_fixture import FakeSchoolSession
from tests.school_login_fixture import Response
import school_timetable as school


class DirectSchoolLoginTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.service = SchoolLoginService(FakeSchoolSession, lambda:self.now)
        self.owner = ('user-a','browser-session-a')

    def start(self):
        return self.service.start(self.owner, '2026', '3', 7)

    def save(self, schedule, revision):
        self.assertEqual(revision, 7)
        return {'schedule':schedule,'revision':8}

    def test_real_sequence_confirmation_ticket_redirect_and_direct_scrape(self):
        start = self.start(); key = start['id']
        self.assertTrue(start['qr_image'].startswith('data:image/png;base64,'))
        self.assertEqual(self.service.poll(key,self.owner,self.save)['status'],'waiting')
        self.assertEqual(self.service.poll(key,self.owner,self.save)['status'],'scanned')
        result = self.service.poll(key,self.owner,self.save)
        self.assertEqual(result['result']['schedule']['courses'][0]['name'],'直接抓取课表')
        self.assertNotIn('PRIVATE_',json.dumps(result))
        self.assertTrue(all(call[1].startswith('https://') for call in self.service.flows[key].client.calls))
        self.assertTrue(self.service.connected(self.owner))
        self.assertEqual(result,self.service.poll(key,self.owner,lambda *_:self.fail('Duplicate write')))
        refreshed = self.service.refresh(self.owner,'2026','12')
        self.assertEqual(refreshed['term'],'12')
        self.service.cancel(key,self.owner,keep_complete=True)
        self.assertTrue(self.service.connected(self.owner))
        self.service.clear_owner(self.owner)
        self.assertFalse(self.service.connected(self.owner))

    def test_owner_and_browser_session_boundaries_and_expiry(self):
        key = self.start()['id']
        for owner in [('user-b','browser-session-a'),('user-a','browser-session-b')]:
            with self.assertRaises(ValueError):self.service.poll(key,owner,self.save)
            self.service.cancel(key,owner)
            self.assertIn(key,self.service.flows)
        self.now = 301
        with self.assertRaises(ValueError):self.service.poll(key,self.owner,self.save)

    def test_cancel_during_course_read_prevents_write(self):
        key = self.start()['id']; flow = self.service.flows[key];flow.client.codes=[405]
        entered, release = threading.Event(),threading.Event()
        flow.client.block_course=lambda:(entered.set(),release.wait(5))
        with ThreadPoolExecutor(1) as pool:
            result=pool.submit(self.service.poll,key,self.owner,lambda *_:self.fail('Canceled login wrote a timetable'))
            self.assertTrue(entered.wait(5));self.service.cancel(key,self.owner);release.set()
            with self.assertRaises(ValueError):result.result()

    def test_redirect_scope_and_replacement(self):
        for value in ['http://sso.hualixy.edu.cn/a','https://evil.example/a','https://sso.hualixy.edu.cn:444/a','https://user@sso.hualixy.edu.cn/a']:
            with self.assertRaises(ValueError):checked_url(value,{'sso.hualixy.edu.cn'})
        old=self.start()['id'];new=self.start()['id']
        self.assertNotIn(old,self.service.flows);self.assertIn(new,self.service.flows)

    def test_reverse_proxy_redirects_upgrade_only_the_known_school_hosts(self):
        self.assertEqual(school_redirect('http://sso.hualixy.edu.cn/heer_cas/#/login'), 'https://sso.hualixy.edu.cn/heer_cas/#/login')
        self.assertEqual(school_redirect('http://jw.hualixy.edu.cn:80/jwglxt/ticketlogin?ticket=FIXTURE'), 'https://jw.hualixy.edu.cn/jwglxt/ticketlogin?ticket=FIXTURE')
        for url in ['http://evil.example/a', 'http://jw.hualixy.edu.cn:8080/a', 'http://user@jw.hualixy.edu.cn/a']:
            with self.assertRaises(ValueError):school_redirect(url)

    def test_two_students_can_confirm_concurrently_without_sharing_courses(self):
        class StudentSession(FakeSchoolSession):
            def __init__(self, name):
                super().__init__();self.name=name;self.codes=[405]

            def request(self, method, url, **kwargs):
                if school.COURSE_PATH in url:
                    assert self.school_ready
                    return Response({'kbList':[{'kcmc':self.name,'xqj':'1','jcs':'1-2','zcd':'1-16周'}]})
                return super().request(method,url,**kwargs)

        names=iter(['同学 A 的课表','同学 B 的课表'])
        service=SchoolLoginService(lambda:StudentSession(next(names)))
        a,b=('student-a','browser-a'),('student-b','browser-b')
        ka=service.start(a,'2026','3',0)['id'];kb=service.start(b,'2026','3',0)['id']
        self.assertIsNot(service.flows[ka].client,service.flows[kb].client)
        with ThreadPoolExecutor(2) as pool:
            fa=pool.submit(service.poll,ka,a,lambda value,_:{'schedule':value})
            fb=pool.submit(service.poll,kb,b,lambda value,_:{'schedule':value})
            self.assertEqual(fa.result()['result']['schedule']['courses'][0]['name'],'同学 A 的课表')
            self.assertEqual(fb.result()['result']['schedule']['courses'][0]['name'],'同学 B 的课表')
        with self.assertRaises(ValueError):service.poll(kb,a,lambda *_:self.fail('Cross-account write'))
        service.clear_owner(a)
        self.assertTrue(service.connected(b))
        self.assertEqual(service.refresh(b,'2026','3')['courses'][0]['name'],'同学 B 的课表')

    def test_completed_students_do_not_use_the_pending_scan_quota(self):
        for index in range(64):
            self.service.flows[str(index)]=LoginFlow((str(index),'device'),FakeSchoolSession(),'2026','3',0,self.now,status='complete')
        self.assertEqual(self.start()['status'],'waiting')
