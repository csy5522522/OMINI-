"""Exercise direct QR login, CAS exchange and scraping with no browser extension."""
from pathlib import Path
import tempfile
import threading

from playwright.sync_api import sync_playwright, expect

from school_login import SchoolLoginService
from tests.school_login_fixture import FakeSchoolSession
from web_server import WebState, make_server


def main():
    out=Path(__file__).resolve().parents[1]/'artifacts/verification/school-direct';out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='omini-school-direct-') as directory:
        state=WebState(Path(directory));state.school_login=SchoolLoginService(FakeSchoolSession)
        server=make_server(state,'127.0.0.1',0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True);context=browser.new_context();page=context.new_page()
                errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                result=context.request.post(url+'/api/auth/register',data={'email':'direct@example.test','password':'FixturePass-123'},headers={'X-Omini-Client':'web'})
                assert result.ok,result.text()
                page.goto(url);page.locator('#import-open').click();page.locator('[data-import-mode=school]').click()
                page.locator('#school-login').click()
                expect(page.locator('#school-qr')).to_be_visible()
                expect(page.locator('#online-submit')).to_be_disabled()
                expect(page.locator('#school-year')).to_be_disabled()
                expect(page.locator('#school-term')).to_be_disabled()
                assert page.locator('#school-qr').get_attribute('src').startswith('data:image/')
                assert len(context.pages)==1
                assert 'school-connector' not in page.content()
                page.screenshot(path=str(out/'qr-login.png'))
                expect(page.locator('#schedule-grid')).to_contain_text('直接抓取课表',timeout=15000)
                expect(page.locator('#online-dialog')).not_to_be_visible()
                expect(page.locator('#toast')).to_contain_text('已自动保存并导入')
                page.reload()
                expect(page.locator('#schedule-grid')).to_contain_text('直接抓取课表')
                saved=context.request.get(url+'/api/timetable').json();revision=saved['revision']
                school_client=next(iter(state.school_login.flows.values())).client
                reads=len([call for call in school_client.calls if 'cxXsgrkb' in call[1]])
                assert context.request.get(url+'/api/timetable/school/session').json()['connected']
                page.locator('#online-open').click()
                expect(page.locator('#school-connection-status')).to_contain_text('已连接')
                expect(page.locator('#online-submit')).to_be_enabled()
                page.locator('#online-submit').click()
                expect(page.locator('#online-dialog')).not_to_be_visible()
                assert context.request.get(url+'/api/timetable').json()['revision']>=revision
                assert len([call for call in school_client.calls if 'cxXsgrkb' in call[1]])==reads+1
                # A different browser session of the same account cannot read this school session.
                other=browser.new_context();result=other.request.post(url+'/api/auth/login',data={'email':'direct@example.test','password':'FixturePass-123'},headers={'X-Omini-Client':'web'});assert result.ok
                assert not other.request.get(url+'/api/timetable/school/session').json()['connected'];other.close()
                for width in (390,320):
                    page.set_viewport_size({'width':width,'height':900});page.locator('#online-open').click()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.screenshot(path=str(out/f'direct-{width}.png'));page.locator('[data-close=online-dialog]').click()
                response=context.request.post(url+'/api/auth/logout',data={},headers={'X-Omini-Client':'web'});assert response.ok
                assert not state.school_login.flows
                assert not errors,errors
                browser.close()
            print('PASS plain browser: QR display, scan confirmation, CAS ticket exchange, direct course scrape, automatic import, cached online refresh, browser-session isolation, logout cleanup and mobile layouts')
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)


if __name__=='__main__':main()
