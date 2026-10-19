"""Packaged UI/browser smoke test. Run only with an isolated APPDATA directory."""
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWidgets import QApplication

from school_timetable import BASE_URL, HOST, PAGE_URL, parse_schedule


def run_timetable_test(app_module):
    qt = QApplication([])
    schedule = parse_schedule({"kbList": [{"kcmc": "课表验证示例", "xm": "示例教师", "cdmc": "示例教室",
        "xqj": "1", "jcs": "1-2", "zcd": "1-16周(单)"}]}, "2026", "3")
    app_module.save_json(app_module.APP_DIR / "school-timetable.json", schedule)
    window = app_module.MainWindow(); window.show(); window.nav.setCurrentRow(2)
    page = window.timetable_page
    page.week.setCurrentIndex(page.week.findData(1))
    report = {"version": app_module.APP_VERSION, "sidebar_verified": window.stack.currentWidget() is page,
              "offline_courses": page.list.rowCount(), "course_span": page.grid.rowSpan(0, 0), "browser_verified": False}
    outcome = [1]
    # Use local HTML at the school base URL, real Chromium cookies and JavaScript,
    # and a fixture API. No school HTTP request or account login is made.
    import timetable_page
    original = timetable_page.LOGIN_URL; timetable_page.LOGIN_URL = "about:blank"
    page.login(); timetable_page.LOGIN_URL = original
    dialog = page.login_dialog
    report["browser_profile_ephemeral"] = dialog.profile.isOffTheRecord()
    fetched = []
    captured = parse_schedule({"kbList": [{"kcmc": "自动读取课表示例", "xqj": "1", "jcs": "1-2",
                                           "zcd": "1-16周(单)"}]}, "2026", "3")
    original_fetch = timetable_page.fetch_schedule

    def fixture_fetch(cookie, year, term, cancelled):
        fetched.append(cookie == "JSESSIONID=FIXTURE_TOKEN" and year == "2026" and term == "3")
        return captured

    timetable_page.fetch_schedule = fixture_fetch
    timeout = QTimer(); timeout.setSingleShot(True)
    poll = QTimer(); poll.setInterval(25)
    finished = [False]

    def finish(ok):
        if finished[0]: return
        finished[0] = True
        timeout.stop(); poll.stop()
        report["browser_verified"] = ok
        report["automatic_capture_verified"] = ok and fetched == [True]
        passed = (report["sidebar_verified"] and report["offline_courses"] == 1
                  and report["course_span"] == 2 and report["automatic_capture_verified"]
                  and report["browser_profile_ephemeral"] and report.get("shared_class_profile_verified", False)
                  and report.get("class_preview_verified", False))
        outcome[0] = 0 if passed else 1
        app_module.save_json(app_module.APP_DIR / "timetable-smoke.json", report)
        timetable_page.fetch_schedule = original_fetch
        window.quitting = True; window.close()

    def check():
        if page.worker is None and page.schedule["courses"][0]["name"] == "自动读取课表示例":
            page.tabs.setCurrentWidget(page.class_page)
            ready = page.class_page._ensure_browser()
            report["shared_class_profile_verified"] = ready and page.class_page.profile is page._browser_profile
            if ready:
                preview = page.class_page._make_view("课表预览验证")
                report["class_preview_verified"] = (preview.page().profile() is page._browser_profile
                                                      and page.class_page.browser_tabs.count() == 2)
                page.class_page.close_preview(1)
            finish(True)

    timeout.timeout.connect(lambda: finish(False)); timeout.start(15000)
    poll.timeout.connect(check); poll.start()
    cookie = QNetworkCookie(b"JSESSIONID", b"FIXTURE_TOKEN"); cookie.setDomain(HOST); cookie.setPath("/jwglxt")
    dialog.profile.cookieStore().setCookie(cookie, QUrl(BASE_URL))
    dialog.web.setHtml("<!doctype html><meta charset='utf-8'><title>Timetable browser smoke</title>"
                       '<select id="xnm"><option>2026</option></select><select id="xqm"><option>3</option></select>',
                       QUrl(PAGE_URL))
    qt.exec()
    return outcome[0]
