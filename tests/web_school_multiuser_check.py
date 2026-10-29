"""Two students scan independently and retain separate account timetables."""
from pathlib import Path
import tempfile
import threading

from playwright.sync_api import sync_playwright, expect

import school_timetable as school
from school_login import SchoolLoginService
from tests.school_login_fixture import FakeSchoolSession, Response
from web_server import WebState, make_server


def main():
    names=iter(['同学 A 的课程','同学 B 的课程'])
    class StudentSession(FakeSchoolSession):
        def __init__(self):
            super().__init__();self.name=next(names)
        def request(self,method,url,**kwargs):
            if school.COURSE_PATH in url:
                assert self.school_ready
                return Response({'kbList':[{'kcmc':self.name,'xqj':'1','jcs':'1-2','zcd':'1-16周'}]})
            return super().request(method,url,**kwargs)

    with tempfile.TemporaryDirectory(prefix='omini-school-students-') as directory:
        state=WebState(Path(directory));state.school_login=SchoolLoginService(StudentSession)
        server=make_server(state,'127.0.0.1',0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True)
                contexts=[];pages=[];users=[]
                for email in ['student-a@example.test','student-b@example.test']:
                    context=browser.new_context();contexts.append(context)
                    response=context.request.post(url+'/api/auth/register',data={'email':email,'password':'FixturePass-123'},headers={'X-Omini-Client':'web'})
                    assert response.ok;users.append(response.json()['user'])
                    page=context.new_page();pages.append(page);page.goto(url)
                    page.locator('#import-open').click();page.locator('[data-import-mode=school]').click();page.locator('#school-login').click()
                    expect(page.locator('#school-qr')).to_be_visible()
                for page,name in zip(pages,['同学 A 的课程','同学 B 的课程']):
                    expect(page.locator('#schedule-grid')).to_contain_text(name,timeout=15000)
                    expect(page.locator('#online-dialog')).not_to_be_visible()
                for context,name in zip(contexts,['同学 A 的课程','同学 B 的课程']):
                    courses=context.request.get(url+'/api/timetable').json()['schedule']['courses']
                    assert [course['name'] for course in courses]==[name]
                key_b=next(key for key,flow in state.school_login.flows.items() if flow.owner[0]==users[1]['id'])
                denied=contexts[0].request.post(url+'/api/timetable/school/poll',data={'id':key_b},headers={'X-Omini-Client':'web'})
                assert denied.status==400
                contexts[0].request.post(url+'/api/auth/logout',data={},headers={'X-Omini-Client':'web'})
                assert contexts[1].request.get(url+'/api/timetable/school/session').json()['connected']
                pages[1].reload();expect(pages[1].locator('#schedule-grid')).to_contain_text('同学 B 的课程')
                browser.close()
            print('PASS two students: independent QR sessions, automatic import into their own accounts, blocked cross-account polling and logout isolation')
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)


if __name__=='__main__':main()
