"""Offline timetable and external browser routing in the actual packaged EXE."""
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from school_timetable import CLASS_PAGE_URL, LOGIN_URL, parse_schedule


def run_timetable_test(app_module):
    qt = QApplication([])
    schedule = parse_schedule({'kbList': [{'kcmc': '课表验证', 'xqj': '1', 'jcs': '1-2', 'zcd': '1-16周(单)'}]}, '2026', '3')
    app_module.save_json(app_module.APP_DIR / 'school-timetable.json', schedule)
    window = app_module.MainWindow(); window.show(); window.nav.setCurrentRow(2)
    page = window.timetable_page; page.week.setCurrentIndex(page.week.findData(1))
    result = {'version': app_module.APP_VERSION, 'sidebar': window.stack.currentWidget() is page,
              'offline_courses': page.list.rowCount() == 1 and page.grid.rowSpan(0, 0) == 2}
    outcome = [1]

    def finish():
        with patch('timetable_page.QDesktopServices.openUrl', return_value=True) as opener:
            page.login()
            result['external_login'] = opener.call_args.args[0].toString() == LOGIN_URL
            result['no_embedded_browser'] = not hasattr(page.login_dialog, 'web')
            page.login_dialog.reject()
            page.open_class_query()
            result['external_class_query'] = opener.call_args.args[0].toString() == CLASS_PAGE_URL
        result['ok'] = all(v for k, v in result.items() if k != 'version')
        app_module.save_json(app_module.APP_DIR / 'timetable-smoke.json', result)
        outcome[0] = 0 if result['ok'] else 1
        window.quitting = True; window.close()

    QTimer.singleShot(200, finish)
    qt.exec()
    return outcome[0]
