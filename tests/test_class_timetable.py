"""External browser routing and preservation of the offline personal timetable."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from school_timetable import CLASS_PAGE_URL, LOGIN_URL, parse_schedule
from timetable_page import TimetablePage
from tests.test_timetable import payload


class ClassTimetableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.qt = QApplication.instance() or QApplication([])

    def test_external_query_and_login_leave_offline_cache_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'cache.json'
            cache.write_text(json.dumps(parse_schedule(payload(), '2026', '3')), encoding='utf-8')
            original = cache.read_bytes()
            page = TimetablePage({}, lambda _: None, cache, lambda _: self.fail('Offline cache was rewritten'))
            page.show()
            with patch('class_timetable_page.QDesktopServices.openUrl', return_value=True) as opener:
                page.open_class_query()
                self.assertEqual(opener.call_args.args[0].toString(), CLASS_PAGE_URL)
                self.assertFalse(page.import_button.isVisible())
                page.class_page.login()
                self.assertEqual(opener.call_args.args[0].toString(), LOGIN_URL)
                page.class_page.stop(); opener.reset_mock()
                page.class_page.open_query(); opener.assert_not_called()
            page.tabs.setCurrentWidget(page.grid)
            self.assertTrue(page.import_button.isVisible())
            self.assertEqual(page.list.rowCount(), 2)
            self.assertEqual(cache.read_bytes(), original)
            page.stop(); page.close(); page.deleteLater(); QTest.qWait(20)

    def test_missing_system_browser_displays_copyable_url(self):
        with tempfile.TemporaryDirectory() as directory:
            page = TimetablePage({}, lambda _: None, Path(directory)/'cache.json', lambda _: None)
            with patch('class_timetable_page.QDesktopServices.openUrl', return_value=False):
                page.class_page.open_query()
            self.assertIn(CLASS_PAGE_URL, page.class_page.status.text())
            self.assertFalse(hasattr(page.class_page, 'main_view'))
            page.stop(); page.deleteLater(); self.qt.processEvents()
