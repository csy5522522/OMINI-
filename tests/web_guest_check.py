"""Real Chromium guest navigation and first synced memo acceptance in isolation.

Run: python -m tests.web_guest_check
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

import requests
from playwright.sync_api import sync_playwright

from account_client import AccountClient
from memo_sync import sync_memos
from tests.account_sync_check import Service
from text_notes import TextNotesStore, read_text_file

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'verification' / 'web-guest'


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    report = {'checks': [], 'screenshots': []}
    with tempfile.TemporaryDirectory(prefix='omini-web-guest-') as temporary:
        root = Path(temporary)
        service = Service(root)
        try:
            client = AccountClient(service.url)
            client.request('register', {'email': 'web-guest@example.test', 'password': 'FixturePassword-123'})
            desktop = root / 'desktop-notes'
            store = TextNotesStore(desktop)
            note = store.create('客户端已同步的备忘录', '首次打开即可看到正文。\n客户端和网页使用同一个账号。')
            sync_memos(desktop, service.url, client.token)

            with requests.Session() as anonymous:
                anonymous.trust_env = False
                for endpoint in ('/api/state', '/api/memos', '/api/timetable', '/api/sync'):
                    response = anonymous.get(service.url + endpoint, timeout=5)
                    assert response.status_code == 401, (endpoint, response.status_code)
                    assert '首次打开即可看到正文' not in response.text
                response = anonymous.post(service.url + '/api/memos', json={'title': 'visitor'},
                    headers={'X-Omini-Client': 'web'}, timeout=5)
                assert response.status_code == 401
            report['checks'].append('Guest browsing retains authentication on private read and write APIs')

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={'width': 1600, 'height': 1040})
                page = context.new_page()
                errors, api_requests = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: api_requests.append(request.url.split('/api/', 1)[1])
                    if '/api/' in request.url else None)
                page.goto(service.url)
                page.wait_for_function('() => connected')
                assert not page.locator('#login-dialog').is_visible()
                page.wait_for_timeout(2300)
                assert api_requests == ['auth/session'], api_requests
                page.locator('[data-page="notes"]').click()
                page.wait_for_function('() => typeof loadMemos === "function" && !document.querySelector("#page-notes").hasAttribute("data-feature-pending")')
                assert not page.locator('#login-dialog').is_visible()
                assert page.locator('#memo-list').inner_text() == '登录后显示你的备忘录'
                page.locator('#account-open').click()
                assert page.locator('#login-dialog').is_visible()
                page.keyboard.press('Escape')
                assert not page.locator('#login-dialog').is_visible()
                page.locator('#account-open').click()
                page.locator('[data-close="login-dialog"]').click()
                assert not page.locator('#login-dialog').is_visible()
                report['checks'].append('First visit and guest notes view stay browsable; optional login closes with Escape or close')

                for width in (1600, 1280, 1100, 900, 720, 390, 320):
                    page.set_viewport_size({'width': width, 'height': 900 if width > 720 else 844})
                    page.wait_for_timeout(80)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                    boxes = []
                    for selector in ('#account-open', '#chatgpt-reset-open', '#api-relay-link'):
                        item = page.locator(selector)
                        assert item.is_visible(), (width, selector)
                        box = item.bounding_box()
                        assert box and box['x'] >= 0 and box['x'] + box['width'] <= width + 1, (width, selector, box)
                        boxes.append(box)
                    for index, a in enumerate(boxes):
                        for b in boxes[index + 1:]:
                            overlap = (min(a['x'] + a['width'], b['x'] + b['width']) > max(a['x'], b['x']) + 1
                                and min(a['y'] + a['height'], b['y'] + b['height']) > max(a['y'], b['y']) + 1)
                            assert not overlap, (width, boxes)
                    if width in (1600, 900, 390, 320):
                        screenshot = DEST / f'guest-{width}.png'
                        page.screenshot(path=screenshot, full_page=True)
                        report['screenshots'].append(str(screenshot))
                report['checks'].append('320–1600px: login and both shortcuts remain visible without overlap or horizontal overflow')

                page.locator('#chatgpt-reset-open').click()
                assert page.locator('#chatgpt-reset-dialog').is_visible()
                assert '即将上线' in page.locator('#chatgpt-reset-dialog').inner_text()
                page.locator('[data-close="chatgpt-reset-dialog"]').click()
                assert api_requests == ['auth/session'], api_requests
                link = page.locator('#api-relay-link')
                assert link.get_attribute('href') == 'https://api.omini.cn'
                assert link.get_attribute('target') == '_blank'
                assert 'noopener' in link.get_attribute('rel')
                report['checks'].append('ChatGPT reset stays a placeholder without API calls; relay links to api.omini.cn')

                page.set_viewport_size({'width': 1600, 'height': 1040})
                page.locator('#account-open').click()
                page.locator('#account-email').fill('web-guest@example.test')
                page.locator('#account-password').fill('FixturePassword-123')
                page.locator('#login-form').evaluate('(form) => form.requestSubmit()')
                page.wait_for_function('() => typeof memoLoaded !== "undefined" && memoLoaded && !memoLoading && memoCurrent')
                assert page.locator('#memo-title').input_value() == '客户端已同步的备忘录'
                assert page.evaluate('memoEditor.getContent()') == store.for_path(note)['content']
                assert not page.locator('#login-dialog').is_visible()
                page.reload()
                page.wait_for_function('() => typeof memoLoaded !== "undefined" && memoLoaded && !memoLoading && memoCurrent')
                assert page.evaluate('memoEditor.getContent()') == store.for_path(note)['content']
                assert not page.locator('#login-dialog').is_visible()
                report['checks'].append('Desktop-synced memo title and body appear immediately after login and authenticated reload')

                store.save(note, '客户端刚刚更新的正文', read_text_file(note)[1])
                sync_memos(desktop, service.url, client.token)
                page.locator('#memo-refresh').click()
                page.wait_for_function('() => memoEditor.getContent() === "客户端刚刚更新的正文"')
                page.locator('[data-page="timetable"]').click()
                store.save(note, '切回页面立即读取新内容', read_text_file(note)[1])
                sync_memos(desktop, service.url, client.token)
                page.locator('[data-page="notes"]').click()
                page.wait_for_function('() => memoEditor.getContent() === "切回页面立即读取新内容"')
                report['checks'].append('Manual refresh and returning to notes both display later desktop updates')

                page.locator('#account-open').click()
                page.locator('#account-logout').click()
                page.wait_for_function('() => !auth.authenticated')
                assert not page.locator('#login-dialog').is_visible()
                assert page.evaluate('memoEditor.getContent()') == ''
                assert page.locator('.memo-item').count() == 0
                assert client.request('session')['user']['email'] == 'web-guest@example.test'
                assert client.api('/api/memos')['memos'][0]['content'] == '切回页面立即读取新内容'
                report['checks'].append('Web logout clears its private view while retaining the desktop session and synced data')
                assert errors == [], errors
                report['checks'].append('No Chromium runtime errors')
                browser.close()
        finally:
            service.close()
    report['ok'] = True
    (DEST / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
