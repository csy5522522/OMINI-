"""Real Chromium/Qt workspace acceptance in temporary accounts and directories.

Run: python -m tests.memo_workspace_check
"""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
import tempfile
import threading
import time

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from account_client import AccountClient
from text_notes_page import TextNotesPage

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'verification' / 'memo-workspace'


def picture():
    image = Image.new('RGB', (1100, 360), '#e5eee8')
    draw = ImageDraw.Draw(image)
    for i in range(360):
        draw.line((0, i, 1100, i), fill=(229-int(i*.08), 238-int(i*.05), 232-int(i*.045)))
    draw.ellipse((760, -150, 1190, 280), outline='#9db6a6', width=2)
    draw.ellipse((810, -110, 1140, 230), outline='#b6c9bb', width=2)
    font_path = 'C:/Windows/Fonts/segoeui.ttf'
    draw.text((58, 70), 'A LITTLE SPACE', fill='#315c47', font=ImageFont.truetype(font_path, 45))
    draw.text((58, 135), 'FOR YOUR IDEAS.', fill='#315c47', font=ImageFont.truetype(font_path, 45))
    draw.text((61, 268), 'OMINI  /  PERSONAL NOTEBOOK', fill='#718e7c', font=ImageFont.truetype(font_path, 16))
    stream = io.BytesIO(); image.save(stream, 'PNG'); return stream.getvalue()


def until(condition, seconds=20):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        QTest.qWait(20)
    assert condition(), 'Qt condition timed out'


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    report = {'checks': [], 'screenshots': []}
    qt = QApplication.instance() or QApplication([])
    qt.setFont(QFont('Microsoft YaHei UI', 10))
    with tempfile.TemporaryDirectory(prefix='omini-workspace-') as temporary:
        directory = Path(temporary)
        environment = os.environ.copy(); environment.update(OMINI_ADMIN_PASSWORD='Fixture-Admin-123', OMINI_WEB_PASSWORD='', PYTHONIOENCODING='utf-8')
        process = subprocess.Popen([sys.executable, 'web_server.py', '--port', '0', '--data-dir', str(directory/'server')],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8', env=environment,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        ready = threading.Event(); server = {}; output = []
        def read_output():
            for line in process.stdout:
                output.append(line)
                match = re.search(r'Local URL: (http://\S+)', line)
                if match: server['url'] = match[1]; ready.set()
        threading.Thread(target=read_output, daemon=True).start()
        assert ready.wait(20), ''.join(output)
        url = server['url']
        native = None
        try:
            client = AccountClient(url)
            client.request('register', {'email':'workspace@example.test', 'password':'FixturePassword-123'})
            raw = picture()
            asset = client.api('/api/memos/assets', {'name':'想法.png','data':base64.b64encode(raw).decode()})
            token = f"![想法.png](omini-asset://{asset['id']})"
            content = '把零散的想法，慢慢写成自己的世界。\n\n今天想记住的三件事：\n留一点时间散步，读几页喜欢的书，记录突然出现的灵感。\n\n' + token + '\n\n文字与图片，在同一页自然生长。'
            memo = client.api('/api/memos', {'title':'我的世界','content':content})['memo']
            client.api('/api/memos', {'title':'关于下一次旅行','content':'想去海边走走，看看没有安排的日落。'})
            client.api('/api/memos', {'title':'随手记下的灵感','content':'好的想法，有时来自一次不经意的停留。'})
            class Account:
                user = {'id': client.request('session')['user']['id'], 'email':'workspace@example.test'}
                token = client.token
                class Changed:
                    def connect(self, callback): pass
                account_changed = Changed()
            account = Account(); account.url = url
            native = TextNotesPage(directory/'notes.json', account=account)
            native.resize(1240, 880); native.show()
            until(lambda: native.worker is None and native.context is not None)
            native.sync_timer.stop()
            path = next(path for path in native.store.files() if native.store.for_path(path)['title'] == '我的世界')
            native.refresh_files(path.name)
            assert native.editor.get_content() == content
            assert '\ufffc' in native.editor.toPlainText()
            assert len(native.findChildren(__import__('memo_editor').MemoEditor)) == 1
            report['checks'].append('Qt received the same legacy text/image content from HTTP sync')

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={'width':1600,'height':1040}, device_scale_factor=1)
                page = context.new_page(); errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(url+'/#notes')
                page.locator('#account-open').click()
                page.locator('#account-email').fill('workspace@example.test')
                page.locator('#account-password').fill('FixturePassword-123')
                page.locator('#login-form').evaluate('(form)=>form.requestSubmit()')
                page.wait_for_function('() => auth.user !== null')
                page.locator('.memo-item').filter(has_text='我的世界').click()
                page.wait_for_function("() => document.querySelector('.memo-inline-image img')?.naturalWidth > 0")
                assert page.evaluate('memoEditor.getContent()') == content
                assert page.locator('#memo-preview').count() == 0
                assert page.locator('#memo-content [contenteditable=true]').count() == 1
                assert not page.locator('#memo-content').inner_text().__contains__('omini-asset://')
                from memo_editor import IMAGE_OBJECT
                assert native.editor.document().documentLayout().handlerForObject(IMAGE_OBJECT) is not None
                for theme in ('light','dark'):
                    page.evaluate('(theme)=>setTheme(theme)',theme)
                    screenshot = DEST / f'web-{theme}.png'; page.screenshot(path=screenshot, full_page=True)
                    report['screenshots'].append(str(screenshot))
                    native.apply_theme(theme=='dark'); QTest.qWait(80)
                    screenshot = DEST / f'desktop-{theme}.png'; native.grab().save(str(screenshot)); report['screenshots'].append(str(screenshot))
                page.locator('.ProseMirror').focus(); page.keyboard.press('Control+End')
                page.locator('#memo-image').set_input_files({'name':'新增图片.png','mimeType':'image/png','buffer':raw})
                extra_token = f"![新增图片.png](omini-asset://{asset['id']})"
                page.wait_for_function('() => document.querySelectorAll(".memo-inline-image").length === 2')
                assert page.evaluate('memoEditor.getContent()') == content + extra_token
                page.keyboard.press('Backspace'); assert page.evaluate('memoEditor.getContent()') == content
                report['checks'].append('Image file uploads insert an editable atom at the current caret')
                # Keep a real selection and long scroll position through a save.
                page.locator('.ProseMirror').click()
                page.keyboard.press('Control+End'); page.keyboard.press('Enter'); page.keyboard.insert_text('中文补充 😀')
                page.keyboard.press('Enter'); page.keyboard.press('Enter'); page.keyboard.insert_text('第二段')
                expected = content+'\n中文补充 😀\n\n第二段'
                assert page.evaluate('memoEditor.getContent()') == expected, repr(page.evaluate('memoEditor.getContent()'))
                selection = page.evaluate('memoEditor.view.state.selection.from')
                page.keyboard.press('Control+s')
                page.wait_for_function('() => !memoDirty && !memoBusy')
                assert page.evaluate('memoEditor.view.state.selection.from') == selection
                assert client.api('/api/memos')['memos'][0]['content'] == expected
                native.sync_now(); until(lambda: native.worker is None)
                assert native.editor.get_content() == expected
                report['checks'].append('Real Chromium keyboard input and Ctrl+S round trip to the native editor')

                # Native edit travels back to the same web document.
                cursor = native.editor.textCursor(); cursor.movePosition(QTextCursor.MoveOperation.End); native.editor.setTextCursor(cursor)
                native.editor.insertPlainText('\n客户端补充'); assert native.flush()
                native.sync_now(); until(lambda: native.worker is None)
                page.evaluate('loadMemos(true)')
                assert page.evaluate('memoEditor.getContent()') == expected+'\n客户端补充'
                report['checks'].append('Native changes round trip back to the same Chromium document')

                # Delete the image atom and restore it using the real editor history.
                page.evaluate("""()=>{const view=memoEditor.view;let pos;view.state.doc.descendants((node,p)=>{if(node.type.name==='image')pos=p;});view.dispatch(view.state.tr.delete(pos,pos+1));}""")
                assert token not in page.evaluate('memoEditor.getContent()')
                page.locator('.ProseMirror').focus(); page.keyboard.press('Control+z')
                assert token in page.evaluate('memoEditor.getContent()')
                page.keyboard.press('Control+Shift+z'); assert token not in page.evaluate('memoEditor.getContent()')
                page.keyboard.press('Control+z'); page.keyboard.press('Control+s'); page.wait_for_function('() => !memoDirty && !memoBusy')
                report['checks'].append('Real Chromium image deletion, undo and redo preserve image tokens')

                # Real clipboard inserts literal text, never arbitrary rich HTML.
                page.evaluate("""()=>{const transfer=new DataTransfer();transfer.setData('text/plain','<img src=x onerror=alert(1)>');transfer.setData('text/html','<img src=x onerror=alert(1)>');document.querySelector('.ProseMirror').dispatchEvent(new ClipboardEvent('paste',{clipboardData:transfer,bubbles:true,cancelable:true}));}""")
                assert '<img src=x onerror=alert(1)>' in page.evaluate('memoEditor.getContent()')
                assert page.locator('.ProseMirror img[onerror]').count() == 0
                page.keyboard.press('Control+s'); page.wait_for_function('() => !memoDirty && !memoBusy')
                report['checks'].append('Pasted HTML remains literal text; image resources are account scoped')

                # Composition must leave the pending edit out of the save pipeline.
                page.evaluate("document.querySelector('.ProseMirror').dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true,data:'输入'}))")
                assert page.evaluate('saveMemo()') is False
                page.evaluate("document.querySelector('.ProseMirror').dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:'输入'}))")
                page.wait_for_function('() => !memoComposing')
                report['checks'].append('Composition events defer the save pipeline')

                # Phone navigation uses one pane at a time and has no overflow.
                page.set_viewport_size({'width':390,'height':844}); page.evaluate("setTheme('light')")
                page.locator('#memo-back').click(); assert page.locator('.memo-sidebar').is_visible()
                page.locator('.memo-item').filter(has_text='我的世界').click(); assert not page.locator('.memo-sidebar').is_visible()
                assert page.locator('.memo-main').is_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                screenshot = DEST/'web-mobile.png'; page.screenshot(path=screenshot, full_page=True); report['screenshots'].append(str(screenshot))
                page.locator('#memo-more').click(); assert page.locator('#memo-menu').is_visible()
                page.keyboard.press('Escape'); assert not page.locator('#memo-menu').is_visible()
                report['checks'].append('390px viewport switches list/document panes without horizontal overflow')

                # Trash is visibly read only and restores through the same API.
                page.on('dialog',lambda dialog:dialog.accept())
                page.locator('#memo-more').click(); page.locator('#memo-delete').click()
                page.wait_for_function('() => memoCurrent?.deleted === true')
                assert page.locator('.ProseMirror').get_attribute('contenteditable') == 'false'
                page.locator('#memo-restore').click(); page.wait_for_function('() => memoCurrent?.deleted === false')
                assert page.locator('.ProseMirror').get_attribute('contenteditable') == 'true'
                report['checks'].append('Trash shows read-only inline images and restores a new snapshot')
                page.locator('#memo-more').click(); page.locator('#memo-history').click()
                page.locator('#memo-history-dialog').wait_for(state='visible')
                page.locator('.memo-version').last.locator('button').click()
                page.locator('#memo-history-dialog').wait_for(state='hidden')
                assert page.evaluate('memoEditor.getContent()') == content
                native.sync_now(); until(lambda: native.worker is None, seconds=60)
                assert native.editor.get_content() == content
                report['checks'].append('History menu restores the first text/image snapshot and syncs it to Qt')
                assert errors == [], errors
                report['checks'].append('No browser runtime errors')
                browser.close()
            native.stop(); native.close(); native.deleteLater(); native = None; qt.processEvents()
        finally:
            if native:
                until(lambda: native.worker is None)
                native.stop(); native.close(); native.deleteLater(); qt.processEvents()
            process.terminate(); process.wait(timeout=15); process.stdout.close()
    report['ok'] = True
    (DEST/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        DEST.mkdir(parents=True, exist_ok=True)
        (DEST/'report.json').write_text(json.dumps({'ok':False,'error':traceback.format_exc()},ensure_ascii=False,indent=2),encoding='utf-8')
        raise
