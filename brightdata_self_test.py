"""Isolated, offline packaged-GUI verification for Bright Data."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase
from brightdata import BrightDataClient, BrightDataStore, build_request


def run_brightdata_test(app_module):
    qt = QApplication.instance() or QApplication([])
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        for filename in ("msyh.ttc", "msyhbd.ttc"):
            font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / filename
            if font.is_file():
                QFontDatabase.addApplicationFont(str(font))
    qt.setApplicationName(app_module.APP_NAME)
    originals = {key: getattr(app_module, key) for key in
                 ("APP_DIR", "TASKS_FILE", "SETTINGS_FILE", "LOG_DIR", "TEMP_COOKIE_DIR")}
    report = {}
    window = None
    try:
        with tempfile.TemporaryDirectory(prefix="omini-brightdata-smoke-") as directory:
            root = Path(directory)
            for key, relative in {"APP_DIR": ".", "TASKS_FILE": "tasks.json",
                                  "SETTINGS_FILE": "settings.json", "LOG_DIR": "logs",
                                  "TEMP_COOKIE_DIR": "runtime-cookies"}.items():
                setattr(app_module, key, root / relative)
            window = app_module.MainWindow()
            window.resize(1240, 840)
            index = window.stack.indexOf(window.brightdata_page)
            window.nav.setCurrentRow(index)
            window.show()
            qt.processEvents()
            page = window.brightdata_page
            report["sidebar_page"] = index == 5 and window.stack.currentWidget() is page
            report["button_tools"] = page.tabs.count() == 5 and page.search_button.isEnabled() and page.scrape_button.isEnabled() and page.find_button.isEnabled() and page.progress_button.isEnabled()
            report["batch_tools"] = build_request("search", "one\ntwo")[0] == "search_engine_batch"
            fixture = "https://mcp.brightdata.com/mcp?token=TOKEN-fixture"
            page.endpoint.setText(fixture)
            page.save_button.click()
            report["encrypted_persistence"] = (
                BrightDataStore(root / "brightdata.json").endpoint == fixture
                and "TOKEN-fixture" not in (root / "brightdata.json").read_text(encoding="utf-8"))
            report["token_input"] = page.token.text() == "TOKEN-fixture" and page.token.accessibleName() == "Bright Data Token"
            page.queries.setPlainText("公开网页资料\n产品文档")
            page.output.setPlainText("# 示例结果\n\n搜索、网页正文和批量结果会显示在这里。\n\n完成后可复制或导出 Markdown、文本和 JSON。")
            page.status.setText("连接已配置 · 每批最多 10 条 · 准备就绪")
            qt.processEvents()
            image_path = os.environ.get("OMINI_SELF_TEST_IMAGE")
            if image_path:
                report["screenshot"] = window.grab().save(image_path)
                page.dataset.setText("dataset-fixture")
                page.load_details({"name": "示例商品采集器", "input_fields": [
                    {"name": "url", "type": "url", "required": True},
                    {"name": "zipcode", "type": "text", "required": False},
                    {"name": "all_variations", "type": "boolean", "required": False}],
                    "sample_input": [{"url": "https://example.com", "zipcode": "94107"}]},
                    {"dataset_id": "dataset-fixture", "method": "collect_by_url"})
                page.update_action_state()
                report["scraper_form"] = len(page.fields) == 3 and page.run_button.isEnabled()
                page.tabs.setCurrentIndex(2)
                qt.processEvents()
                report["catalog_screenshot"] = window.grab().save(str(Path(image_path).with_name(Path(image_path).stem + "-catalog.png")))
            window.quitting = True
            window.close()
            qt.processEvents()
            report["clean_shutdown"] = not window.isVisible()
        if os.environ.get("OMINI_SELF_TEST_BRIGHTDATA_LIVE") == "1":
            endpoint = BrightDataStore(originals["APP_DIR"] / "brightdata.json").endpoint
            client = BrightDataClient(endpoint, timeout=30)
            try:
                client.initialize()
                names = {tool["name"] for tool in client.list_tools()}
                report["live_connection"] = {"search_engine", "scrape_as_markdown", "search_engine_batch", "scrape_batch",
                    "search_scrapers", "get_scraper_details", "run_scraper", "get_scraper_progress",
                    "get_scraper_results", "refresh_scrapers", "ask_brightdata_assistant"} <= names
            finally:
                client.close()
    except Exception as error:
        report["error"] = type(error).__name__
        if window:
            window.quitting = True
            window.close()
    finally:
        for key, value in originals.items():
            setattr(app_module, key, value)
    log_path = os.environ.get("OMINI_SELF_TEST_LOG")
    if log_path:
        Path(log_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report and all(value is True for value in report.values()) else 1
