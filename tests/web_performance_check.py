"""Chromium verifies cache reuse, deferred features and recoverable load failures."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from tests.account_sync_check import Service

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'verification' / 'web-performance'


def resources(page):
    return page.evaluate('''() => performance.getEntriesByType('resource')
        .filter(e => /\\.(js|css)(\\?|$)/.test(e.name))
        .map(e => ({path:new URL(e.name).pathname,transfer:e.transferSize,bytes:e.decodedBodySize}))''')


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    report = {'checks': []}
    with tempfile.TemporaryDirectory(prefix='omini-web-performance-') as temporary:
        service = Service(Path(temporary))
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                page = context.new_page()
                errors, requests = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: requests.append(urlsplit(request.url).path))
                page.goto(service.url)
                page.wait_for_function('() => connected')
                cold = resources(page)
                assert {r['path'] for r in cold} == {'/app.js', '/style.css'}, cold
                report['cold_resources'] = cold
                report['checks'].append('First timetable visit fetches only base app.js and style.css')

                page.goto(service.url + '/?visit=2')
                page.wait_for_function('() => connected')
                warm = resources(page)
                assert {r['path'] for r in warm} == {'/app.js', '/style.css'}, warm
                assert all(r['transfer'] == 0 and r['bytes'] > 0 for r in warm), warm
                report['warm_resources'] = warm
                report['checks'].append('Later navigation reuses both versioned JS/CSS files without transferring their bodies')

                page.locator('[data-page="notes"]').click()
                page.evaluate('route();route()')
                page.wait_for_function('() => typeof loadMemos === "function" && !document.querySelector("#page-notes").hasAttribute("data-feature-pending")')
                assert '/account-sync.js' not in requests
                assert requests.count('/memo-editor.js') == 1, requests
                assert requests.count('/draft-store.js') == 1, requests
                page.locator('[data-page="ai_assistants"]').click()
                page.wait_for_function('() => typeof loadAccountSync === "function" && !document.querySelector("#page-ai_assistants").hasAttribute("data-feature-pending")')
                assert requests.count('/draft-store.js') == 1, requests
                assert requests.count('/account-sync.js') == 1, requests
                assert '/account-sync.css' in requests
                assert not page.locator('#login-dialog').is_visible()
                report['checks'].append('Notes and assistants load on entry; repeated routing shares dependencies and avoids duplicate scripts')
                context.close()

                # A slow feature may finish later, without holding navigation or login hostage.
                context = browser.new_context()
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                delayed = []
                page.route('**/memo-editor.js*', lambda route: delayed.append(route))
                page.goto(service.url + '/#notes', wait_until='domcontentloaded')
                page.wait_for_function('() => document.querySelector("#page-notes").hasAttribute("data-feature-pending")')
                page.wait_for_timeout(100)
                page.locator('#account-open').click()
                assert page.locator('#login-dialog').is_visible()
                page.keyboard.press('Escape')
                page.locator('[data-page="timetable"]').click()
                page.locator('#page-timetable').wait_for(state='visible')
                page.wait_for_timeout(100)
                assert delayed
                delayed[0].continue_()
                page.wait_for_function('() => typeof loadMemos === "function"')
                assert page.locator('#page-timetable').is_visible()
                assert not page.locator('#page-notes').is_visible()
                report['checks'].append('Delayed editor downloads leave login and navigation usable, and do not reopen an abandoned page')
                context.close()

                for feature, asset, function in (('notes', 'memo-editor.js', 'loadMemos'),
                                                  ('ai_assistants', 'account-sync.css', 'loadAccountSync')):
                    context = browser.new_context()
                    page = context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    attempts = []
                    def intermittent(route):
                        attempts.append(route.request.url)
                        if len(attempts) == 1:
                            route.abort('failed')
                        else:
                            route.continue_()
                    page.route('**/' + asset + '*', intermittent)
                    page.goto(service.url)
                    page.wait_for_function('() => connected')
                    page.locator(f'[data-page="{feature}"]').click()
                    retry = page.locator(f'#page-{feature} .feature-loading button')
                    retry.wait_for()
                    assert '加载失败' in page.locator(f'#page-{feature} .feature-loading').inner_text()
                    retry.click()
                    page.wait_for_function(f'() => typeof {function} === "function" && !document.querySelector("#page-{feature}").hasAttribute("data-feature-pending")')
                    assert len(attempts) == 2, (asset, attempts)
                    assert not page.locator(f'#page-{feature} .feature-loading').is_visible()
                    context.close()
                report['checks'].append('Failed editor scripts and assistant styles show a working retry without reloading the page')
                assert errors == [], errors
                report['checks'].append('All lazy-loading paths finish without browser runtime errors')
                browser.close()
        finally:
            service.close()
    report['ok'] = True
    (DEST / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
