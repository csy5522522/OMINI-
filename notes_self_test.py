"""Packaged native TXT editor and theme checks; use isolated APPDATA."""
import json
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from text_notes import read_text_file


def run_notes_test(app_module):
    qt = QApplication([])
    window = app_module.MainWindow(); window.show(); window.nav.setCurrentRow(3)
    page = window.notes_page
    path = page.store.create("离线笔记验证", "今天的安排\n本地 TXT 文件。")
    page.refresh_files(path.name)
    page.editor.setPlainText("离线编辑内容 ☀\n可直接用记事本打开。")
    result = {"version": app_module.APP_VERSION, "sidebar": window.stack.currentWidget() is page}
    outcome = [1]

    def finish():
        result['plain_txt_saved'] = page.flush() and read_text_file(path)[0] == page.editor.get_content()
        window.grab().save(str(app_module.APP_DIR / "notes-dark.png"))
        window.theme_button.click()
        result['theme_button'] = not page.dark and window.settings_page.theme.currentText() == "浅色"
        window.grab().save(str(app_module.APP_DIR / "notes-light.png"))
        result['no_embedded_browser'] = not hasattr(page, 'web')
        result['ok'] = all(result[k] for k in ('sidebar', 'plain_txt_saved', 'theme_button', 'no_embedded_browser'))
        app_module.save_json(app_module.APP_DIR / 'notes-smoke.json', result)
        outcome[0] = 0 if result['ok'] else 1
        window.quitting = True; window.close()

    QTimer.singleShot(1100, finish)
    qt.exec()
    return outcome[0]
