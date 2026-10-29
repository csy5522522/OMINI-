import base64
import io
import unittest

from timetable_table import import_table


class TableImportTest(unittest.TestCase):
    def test_csv_quoted_fields_and_odd_weeks(self):
        result = import_table('\ufeff课程名称,星期,节次,周次,教师,教室\n"设计,基础",周一,1-2,1-8周(单),张老师,A201', 'test.csv', '2026', '3')
        course = result['courses'][0]
        self.assertEqual(course['name'], '设计,基础')
        self.assertEqual(course['weeks'], [1, 3, 5, 7])
        self.assertEqual(course['periods'], [1, 2])
        self.assertEqual(course['weekday'], 1)

    def test_xlsx_and_tsv(self):
        from openpyxl import Workbook
        book = Workbook()
        book.active.append(['课程名称', '星期', '节次', '周次'])
        book.active.append(['程序设计', 7, '3-4', '2-8周(双)'])
        buffer = io.BytesIO(); book.save(buffer); book.close()
        excel = import_table(base64.b64encode(buffer.getvalue()).decode(), '课表.xlsx', 2026, '12')
        tsv = import_table('课程名称\t星期\t节次\t周次\n程序设计\t周日\t3-4\t2-8周(双)', '课表.tsv', 2026, '12')
        self.assertEqual(excel['courses'], tsv['courses'])

    def test_errors_identify_columns_and_row(self):
        for text, message in [('名称,星期\n数学,1', '缺少列'), ('课程名称,星期,节次,周次\n数学,8,1-2,1-16', '第 2 行'), ('课程名称,星期,节次,周次', '没有课程')]:
            with self.assertRaisesRegex(ValueError, message):
                import_table(text, 'test.csv', 2026, '3')

    def test_size_limit(self):
        with self.assertRaisesRegex(ValueError, '5000'):
            import_table('课程名称,星期,节次,周次\n' + '数学,1,1-2,1-16\n' * 5001, 'test.csv', 2026, '3')
