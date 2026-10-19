"""Packaged Notes/theme visual checks; run with isolated APPDATA only."""
from datetime import datetime, timezone
import json
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


def run_notes_test(app_module):
    qt = QApplication([])
    window = app_module.MainWindow(); window.show(); window.nav.setCurrentRow(3)
    page = window.icloud_page
    stamp = datetime.now(timezone.utc).isoformat()
    page.store.merge({"folders": ["备忘录", "工作"], "notes": [
        dict(id="fixture:one", title="今天的灵感", body="把重要的事情，记在这里。\n\n今日安排\n\n☐ 整理本周工作计划\n☐ 阅读与学习\n☐ 晚间运动\n\n这些内容只保存在本机。云端笔记请在官方在线页面编辑。",
             snippet="", folder="备忘录", modified=stamp, pinned=True, locked=False, complete=True, deleted=False, edited=False),
        dict(id="fixture:two", title="项目会议", body="讨论设计进度与下一步安排。", snippet="", folder="工作", modified=stamp,
             pinned=False, locked=False, complete=True, deleted=False, edited=False),
        dict(id="fixture:three", title="锁定备忘录", body="", snippet="", folder="备忘录", modified=stamp,
             pinned=False, locked=True, complete=False, deleted=False, edited=False)]})
    page.refresh_folders(); page.tabs.setCurrentWidget(page.local)
    directory = app_module.APP_DIR
    result = {"version": app_module.APP_VERSION, "sidebar": window.stack.currentWidget() is page,
              "offline_notes": page.notes.count(), "theme_button": False, "browser": False}
    outcome = [1]

    def light():
        window.settings["theme"] = "深色"; window.apply_theme()
        window.grab().save(str(directory / "notes-dark.png"))
        window.theme_button.click()
        result["theme_button"] = window.settings["theme"] == "浅色" and window.settings_page.theme.currentText() == "浅色"
        QTimer.singleShot(150, minimum)

    def minimum():
        window.grab().save(str(directory / "notes-light.png"))
        window.resize(980, 680)
        QTimer.singleShot(150, browser)

    def browser():
        window.grab().save(str(directory / "notes-minimum.png"))
        result["minimum_width"] = window.width()
        import icloud_notes_page
        original = icloud_notes_page.NOTES_URL; icloud_notes_page.NOTES_URL = "about:blank"
        page.open_online(); icloud_notes_page.NOTES_URL = original
        page.web.setHtml("<!doctype html><meta charset='utf-8'><title>Notes browser fixture</title><p>Official web surface fixture</p>")
        QTimer.singleShot(400, finish)

    def finish():
        result["browser"] = bool(page.web and page.profile.isOffTheRecord())
        result["encrypted_cache"] = "今天的灵感" not in page.store.path.read_text(encoding="utf-8")
        outcome[0] = 0 if (result["sidebar"] and result["offline_notes"] == 3 and result["theme_button"]
                          and result["browser"] and result["encrypted_cache"] and result["minimum_width"] == 980) else 1
        (directory / "notes-smoke.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        window.quitting = True; window.close()

    QTimer.singleShot(200, light)
    qt.exec()
    return outcome[0]
