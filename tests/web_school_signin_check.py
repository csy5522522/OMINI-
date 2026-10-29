"""Guest students sign into Omini with school QR, with no email registration."""
from pathlib import Path
import tempfile
import threading

from playwright.sync_api import sync_playwright, expect

import school_timetable as school
from school_login import SchoolLoginService
from tests.school_login_fixture import FakeSchoolSession, Response
from web_server import WebState,make_server


def main():
    identities=iter(['STUDENT_A','STUDENT_B','STUDENT_A','STUDENT_A'])
    class StudentSession(FakeSchoolSession):
        def __init__(self):
            super().__init__();self.username=next(identities)
        def request(self,method,url,**kwargs):
            if school.COURSE_PATH in url:
                assert self.school_ready
                return Response({'kbList':[{'kcmc':'课程 '+self.username[-1],'xqj':'1','jcs':'1-2','zcd':'1-16周'}]})
            return super().request(method,url,**kwargs)
    out=Path(__file__).resolve().parents[1]/'artifacts/verification/school-signin';out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='omini-school-signin-') as directory:
        state=WebState(Path(directory));state.school_login=SchoolLoginService(StudentSession)
        server=make_server(state,'127.0.0.1',0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True);contexts=[];pages=[];errors=[]
                for index in range(2):
                    context=browser.new_context();contexts.append(context);page=context.new_page();pages.append(page)
                    page.on('pageerror',lambda error:errors.append(str(error)));page.goto(url)
                    assert context.request.get(url+'/api/timetable').status==401
                    page.locator('#import-open').click();page.locator('[data-import-mode=school]').click();page.locator('#school-login').click()
                    expect(page.locator('#school-qr')).to_be_visible()
                    expect(page.locator('#online-submit')).to_be_disabled()
                users=[]
                for index,(page,context) in enumerate(zip(pages,contexts)):
                    expect(page.locator('#schedule-grid')).to_contain_text('课程 '+('A' if index==0 else 'B'),timeout=15000)
                    expect(page.locator('#online-dialog')).not_to_be_visible();expect(page.locator('#account-label')).to_contain_text('华立同学')
                    user=context.request.get(url+'/api/auth/session').json()['user'];users.append(user)
                    assert user['auth_provider']=='school' and 'STUDENT_' not in str(user)
                    assert context.request.get(url+'/api/timetable/school/session').json()['connected']
                    page.reload();expect(page.locator('#schedule-grid')).to_contain_text('课程 '+('A' if index==0 else 'B'))
                assert users[0]['id']!=users[1]['id']
                # Another device scans student A again and gets the same durable account.
                repeat=browser.new_context();page=repeat.new_page();page.goto(url);page.locator('#account-open').click();page.locator('#school-signin').click()
                expect(page.locator('#schedule-grid')).to_contain_text('课程 A',timeout=15000)
                assert repeat.request.get(url+'/api/auth/session').json()['user']['id']==users[0]['id']
                assert len(state.accounts.list_users())==2
                assert contexts[1].request.get(url+'/api/timetable').json()['schedule']['courses'][0]['name']=='课程 B'
                page.screenshot(path=str(out/'signed-in.png'))
                repeat.request.post(url+'/api/auth/logout',data={},headers={'X-Omini-Client':'web'})
                assert contexts[1].request.get(url+'/api/timetable/school/session').json()['connected']
                # Guest browser binding prevents a stolen flow ID from completing sign-in.
                guest=browser.new_context();response=guest.request.post(url+'/api/auth/school/start',data={'year':'2026','term':'3'},headers={'X-Omini-Client':'web'});assert response.ok
                stranger=browser.new_context();denied=stranger.request.post(url+'/api/auth/school/poll',data={'id':response.json()['id']},headers={'X-Omini-Client':'web'})
                assert denied.status==400
                guest.request.post(url+'/api/auth/school/cancel',data={'id':response.json()['id']},headers={'X-Omini-Client':'web'})
                assert not errors,errors;browser.close()
            print('PASS guest school QR: no email registration, two student accounts, automatic course import, persistent login, same student across devices and browser-bound guest flows')
        finally:server.shutdown();server.server_close();thread.join(timeout=3)


if __name__=='__main__':main()
