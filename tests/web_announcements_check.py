"""Chromium acceptance for simplified navigation, announcements, and admin CRUD."""
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import sync_playwright, expect
from account_client import AccountClient
from tests.account_sync_check import Service
from tests.test_account_sync import schedule

DEST = Path(tempfile.gettempdir()) / 'omini-web-updates-20261028'


def main():
    DEST.mkdir(exist_ok=True)
    report = {'checks': [], 'screenshots': [], 'errors': []}
    previous = os.environ.get('OMINI_ADMIN_PASSWORD')
    os.environ['OMINI_ADMIN_PASSWORD'] = 'admin123'
    with tempfile.TemporaryDirectory(prefix='omini-announcements-') as temporary:
        service = Service(Path(temporary))
        try:
            client = AccountClient(service.url)
            client.request('register', {'email': 'reader@example.test', 'password': 'FixturePass-123'})
            client.api('/api/sync/schedule', {'revision': 0, 'data': {'schedule': schedule(), 'selected_week': 1}})
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                admin_context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                admin = admin_context.new_page()
                admin.on('pageerror', lambda error: report['errors'].append(str(error)))
                admin.goto(service.url + '/admin')
                expect(admin.locator('#admin-login')).to_be_visible()
                admin.locator('#admin-password').fill('admin123')
                admin.locator('#admin-login').evaluate('(form)=>form.requestSubmit()')
                expect(admin.locator('#admin-panel')).to_be_visible()
                title = '新公告 <script>alert(1)</script>'
                content = '<img src=x onerror=alert(1)>\n第二行公告内容'
                admin.locator('#announcement-title').fill(title)
                admin.locator('#announcement-content').fill(content)
                admin.locator('#announcement-save').click()
                expect(admin.locator('#announcement-status')).to_have_text('公告已发布。')
                expect(admin.locator('#admin-announcements-list h3')).to_have_text(title)
                guest_context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                guest = guest_context.new_page()
                guest.on('pageerror', lambda error: report['errors'].append(str(error)))
                guest.on('console', lambda message: report['errors'].append(message.text) if message.type == 'error' else None)
                traffic = []
                guest.on('request', lambda req: traffic.append(req.url) if '/api/' in req.url else None)
                guest.goto(service.url)
                guest.wait_for_function('()=>connected')
                expect(guest.locator('#page-timetable')).to_be_visible()
                expect(guest.locator('#announcement-banner-title')).to_have_text(title)
                assert guest.locator('#page-ai_assistants, .workspace, .nav-label, .semester-card, .today-card').count() == 0
                assert guest.locator('text=偏好设置').count() == 0
                expect(guest.locator('#settings-open')).to_have_text('设置')
                guest.locator('#announcement-banner').click()
                expect(guest.locator('#page-announcements')).to_be_visible()
                expect(guest.locator('#announcements-list h2')).to_have_text(title)
                expect(guest.locator('#announcements-list .announcement-content')).to_have_text(content)
                assert guest.locator('#announcements-list img, #announcements-list script').count() == 0
                report['checks'].append('Public announcements show text safely; retired assistant, workspace and schedule cards are absent')
                for width in (1440, 900, 390, 320):
                    guest.set_viewport_size({'width': width, 'height': 900})
                    assert guest.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                    expect(guest.locator('[data-page=announcements]')).to_be_visible()
                    path = DEST / f'announcements-{width}.png'
                    guest.screenshot(path=str(path), full_page=True)
                    report['screenshots'].append(str(path))
                guest.set_viewport_size({'width': 1440, 'height': 1000})
                admin.get_by_role('button', name='编辑', exact=True).click()
                admin.locator('#announcement-title').fill('更新后的公告')
                admin.locator('#announcement-content').fill('管理员修改后的正文')
                admin.locator('#announcement-save').click()
                expect(admin.locator('#announcement-status')).to_have_text('公告已更新。')
                guest.locator('#announcements-refresh').click()
                expect(guest.locator('#announcements-list h2')).to_have_text('更新后的公告')
                admin.reload()
                expect(admin.locator('#admin-announcements-list h3')).to_have_text('更新后的公告')
                path = DEST / 'admin-desktop.png'
                admin.screenshot(path=str(path), full_page=True)
                report['screenshots'].append(str(path))
                admin.set_viewport_size({'width': 390, 'height': 844})
                assert admin.evaluate('document.documentElement.scrollWidth <= innerWidth')
                admin.screenshot(path=str(DEST / 'admin-mobile.png'), full_page=True)
                admin.on('dialog', lambda dialog: dialog.accept())
                admin.get_by_role('button', name='删除', exact=True).click()
                expect(admin.locator('#announcement-status')).to_have_text('公告已删除。')
                guest.locator('#announcements-refresh').click()
                expect(guest.locator('#announcements-status')).to_contain_text('暂无公告')
                expect(guest.locator('#announcement-banner')).to_be_hidden()
                report['checks'].append('Admin /admin login, publish, edit, reload persistence and delete update public views; desktop/mobile layout fits')
                guest.locator('[data-page=timetable]').click()
                guest.locator('#account-open').click()
                guest.locator('#account-email').fill('reader@example.test')
                guest.locator('#account-password').fill('FixturePass-123')
                guest.locator('#login-form').evaluate('(form)=>form.requestSubmit()')
                guest.wait_for_function('()=>auth.user && typeof cloudReady!=="undefined" && cloudReady && !cloudBusy')
                expect(guest.locator('.course-card')).to_have_count(1)
                guest.locator('#settings-open').click()
                guest.locator('#settings-sync-now').click()
                guest.wait_for_function('()=>!cloudBusy')
                assert not any('/api/sync/assistants' in url or 'resources=assistants' in url for url in traffic)
                assert all('resources=preferences,schedule' in url for url in traffic if '/api/sync?' in url)
                report['checks'].append('Existing account and timetable load; web sync requests only preferences and schedule')
                assert report['errors'] == [], report['errors']
                browser.close()
        finally:
            service.close()
            if previous is None: os.environ.pop('OMINI_ADMIN_PASSWORD', None)
            else: os.environ['OMINI_ADMIN_PASSWORD'] = previous
    (DEST / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
