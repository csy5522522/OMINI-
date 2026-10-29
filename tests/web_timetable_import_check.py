"""Real-service acceptance for timetable import, navigation and session handoff."""
import io
from pathlib import Path
import tempfile

from openpyxl import Workbook
from playwright.sync_api import sync_playwright, expect
from tests.account_sync_check import Service


def main():
    destination = Path(__file__).resolve().parents[1] / 'artifacts/verification/timetable-import'
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='omini-import-') as temporary:
        service = Service(Path(temporary))
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(service.url)
                page.locator('#account-open').click()
                page.locator('[data-auth-mode=register]').click()
                page.locator('#account-email').fill('import@example.test')
                page.locator('#account-password').fill('FixturePass-123')
                page.locator('#account-confirm').fill('FixturePass-123')
                page.locator('#auth-submit').click()
                expect(page.locator('#login-dialog')).not_to_be_visible()
                assert page.locator('.sidebar [data-page=announcements]').count() == 0
                expect(page.locator('.topbar [data-page=announcements]')).to_be_visible()
                page.locator('#sidebar-toggle').click()
                expect(page.locator('#sidebar-toggle')).to_have_attribute('aria-expanded', 'false')
                page.reload()
                expect(page.locator('#sidebar-toggle')).to_have_attribute('aria-expanded', 'false')
                expect(page.locator('#connection')).to_contain_text('服务已连接')
                page.locator('#sidebar-toggle').click()
                page.locator('#import-open').click()
                expect(page.locator('#school-login')).not_to_be_visible()
                csv = '课程名称,星期,节次,周次,教师,教室\n数据结构,周三,3-4,1-16周,李老师,A201'
                page.locator('#table-file').set_input_files({'name':'课程.csv','mimeType':'text/csv','buffer':csv.encode()})
                expect(page.locator('#import-dialog')).not_to_be_visible()
                expect(page.locator('#schedule-grid')).to_contain_text('数据结构')
                page.locator('#import-open').click()
                page.locator('#table-file').set_input_files({'name':'错误.csv','mimeType':'text/csv','buffer':'名称,星期\n数学,8'.encode()})
                expect(page.locator('#import-status')).to_contain_text('缺少列')
                expect(page.locator('#schedule-grid')).to_contain_text('数据结构')
                book = Workbook(); book.active.append(['课程名称','星期','节次','周次'])
                book.active.append(['Excel课程', 1, '1-2', '1-16'])
                data = io.BytesIO(); book.save(data); book.close()
                page.locator('#table-file').set_input_files({'name':'课程.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':data.getvalue()})
                expect(page.locator('#import-dialog')).not_to_be_visible()
                expect(page.locator('#schedule-grid')).to_contain_text('Excel课程')
                page.locator('#online-open').click()
                page.locator('#online-form details summary').click()
                page.locator('#school-cookie').fill('JSESSIONID=FIXTURE')
                expect(page.locator('#school-cookie')).to_have_value('JSESSIONID=FIXTURE')
                page.locator('[data-close=online-dialog]').click()
                expect(page.locator('#school-cookie')).to_have_value('')
                page.locator('#online-open').click()
                expect(page.locator('#school-cookie')).to_have_value('')
                page.locator('[data-close=online-dialog]').click()
                for width in (1440, 900, 390, 320):
                    page.set_viewport_size({'width':width,'height':1000})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                    page.locator('#import-open').click()
                    for mode in ('table','school','file'):
                        page.locator(f'[data-import-mode={mode}]').click()
                        expect(page.locator(f'#import-{mode}-panel')).to_be_visible()
                    page.screenshot(path=str(destination / f'import-{width}.png'))
                    page.locator('[data-close=import-dialog]').click()
                    page.locator('#sidebar-toggle').click()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                    page.locator('#sidebar-toggle').click()
                page.locator('#account-open').click(); page.locator('#account-logout').click()
                expect(page.locator('#account-label')).to_contain_text('登录')
                expect(page.locator('#settings-dialog')).not_to_be_visible()
                page.locator('#online-open').click()
                expect(page.locator('#school-cookie')).to_have_value('')
                assert not errors, errors
                assert page.locator('a[href*="school-connector"]').count() == 0
                browser.close()
            print('PASS CSV/XLSX imports, invalid import preserves data, three import choices, cookie refill/reset, navigation, 320–1440px layouts')
        finally:
            service.close()


if __name__ == '__main__':
    main()
