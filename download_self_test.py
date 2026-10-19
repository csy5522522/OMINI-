"""Opt-in end-to-end check of the source or packaged desktop application."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton


def run_download_test(application) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test-download", action="store_true")
    parser.add_argument("--source", required=True)
    parser.add_argument("--cookie")
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--quality", default="最佳")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--ui-submit", action="store_true", help="Submit by clicking the actual video-page button")
    parser.add_argument("--allow-images", action="store_true", help="Accept downloaded Douyin note images, not just MP4")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    report = Path(args.report).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    original_cookie = Path(args.cookie).read_bytes() if args.cookie else None
    started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="omini-download-check-") as state:
        # Never resume or rewrite the user's existing queue during this check.
        application.APP_DIR = Path(state)
        application.SETTINGS_FILE = Path(state) / "settings.json"
        application.TASKS_FILE = Path(state) / "tasks.json"
        application.LOG_DIR = Path(state) / "logs"
        application.TEMP_COOKIE_DIR = Path(state) / "runtime-cookies"
        qt = QApplication([sys.argv[0]])
        qt.setApplicationName(application.APP_NAME)
        window = application.MainWindow()
        window.show()
        window.manager.cookie_required.disconnect(window.download_page.request_cookie)
        events = []
        browser_events = []
        log_lines = []
        window.manager.cookie_required.connect(lambda _, message: events.append(message))
        window.manager.task_log.connect(lambda _, line: log_lines.append(line))
        window.manager.browser_required.connect(lambda task_id: browser_events.append(task_id))
        window.download_page.video_url.setText(args.source)
        window.download_page.video_dir.edit.setText(str(output))
        window.download_page.video_quality.setCurrentText(args.quality)
        if args.ui_submit:
            if args.cookie:
                parser.error("--ui-submit checks the initial public/no-cookie submission; omit --cookie")
            qt.processEvents()
            button = next(button for button in window.download_page.tabs.widget(0).findChildren(QPushButton)
                          if button.text() == "加入下载队列")
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            task = window.manager.tasks[0]
        else:
            task = window.manager.add("video", args.source, str(output), {
                "quality": args.quality, "metadata": True, "thumbnail": True,
                "playlist": False, **({"cookies": args.cookie} if args.cookie else {}),
            })
        timed_out = False

        def timeout() -> None:
            nonlocal timed_out
            if task.status in {"running", "queued"} or window.download_page.browser_dialogs:
                timed_out = True
                window.manager.cancel(task.id)
                for dialog in list(window.download_page.browser_dialogs.values()):
                    dialog.reject()

        def check_finished() -> None:
            if task.status not in {"completed", "failed", "cancelled"} or window.manager.workers or window.download_page.browser_dialogs:
                return
            timer.stop()
            files = [
                {"path": str(path), "bytes": path.stat().st_size}
                for path in output.iterdir() if path.is_file() and path.stat().st_size > 0
                and path.suffix.lower() in ({".mp4", ".jpg", ".jpeg", ".png", ".webp"} if args.allow_images else {".mp4"})
            ]
            cookie_unchanged = not args.cookie or Path(args.cookie).read_bytes() == original_cookie
            history = json.loads(application.TASKS_FILE.read_text(encoding="utf-8"))
            success = task.status == "completed" and bool(files) and cookie_unchanged and not timed_out
            result = {
                "success": success, "version": application.APP_VERSION,
                "frozen": bool(getattr(sys, "frozen", False)),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "task": asdict(task), "files": files,
                "cookie_file_unchanged": cookie_unchanged,
                "temporary_cookies_removed": not any(application.TEMP_COOKIE_DIR.iterdir()),
                "history_status": history[0]["status"],
                "authentication_prompts": len(events), "timed_out": timed_out,
                "browser_parsing_windows": len(browser_events),
                "submitted_by_button": args.ui_submit,
                "selected_new_task": window.download_page.current_task_id == task.id,
                "log": log_lines,
            }
            report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            window.grab().save(str(report.with_suffix(".png")))
            window.close()
            qt.exit(0 if success else 1)

        timer = QTimer()
        timer.timeout.connect(check_finished)
        timer.start(100)
        QTimer.singleShot(args.timeout * 1000, timeout)
        return qt.exec()
