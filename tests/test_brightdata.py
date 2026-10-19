"""Real HTTP MCP session checks and button-level checks in isolated state."""
import json
import csv
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QLineEdit

from brightdata import BrightDataClient, BrightDataError, BrightDataStore, build_request, endpoint_from_token
from brightdata_page import BrightDataPage


class MockMCP(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_DELETE(self):
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append(payload)
        method = payload["method"]
        if method != "initialize" and self.headers.get("Mcp-Session-Id") != "session-fixture":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fixture", "version": "1"}}
        elif method == "tools/list":
            if payload["params"].get("cursor"):
                result = {"tools": [{"name": "scrape_as_markdown"}]}
            else:
                result = {"tools": [{"name": "search_engine"}], "nextCursor": "page2"}
        else:
            self.server.protocol_seen = self.headers.get("MCP-Protocol-Version")
            self.server.started.set()
            self.server.release.wait(5)
            name = payload["params"]["name"]
            result = {"content": [{"type": "text", "text": "# 网页正文\n测试内容"}]}
            args = payload["params"]["arguments"]
            data = None
            if name == "search_engine":
                data = {"organic": [{"title": "搜索结果", "link": "https://example.com"}],
                        "next_cursor": "page-two" if not args.get("cursor") else ""}
            elif name == "search_scrapers":
                data = [{"dataset_id": "dataset-fixture", "name": "商品采集器", "domain": "example.com",
                         "collection_methods": ["collect_by_url", "discover_by_keyword"]}]
            elif name == "get_scraper_details":
                key = "keyword" if args["method"].startswith("discover") else "url"
                data = {"name": "商品采集器", "input_fields": [
                    {"name": key, "type": "text" if key == "keyword" else "url", "required": True},
                    {"name": "all_variations", "type": "boolean", "required": False}],
                    "sample_input": [{key: "示例关键词" if key == "keyword" else "https://example.com"}]}
            elif name == "run_scraper":
                data = {"snapshot_id": "snapshot-fixture", "status": "running"}
            elif name == "get_scraper_progress":
                data = {"status": self.server.progress_state}
            elif name == "get_scraper_results":
                data = [{"title": "测试商品", "url": "https://example.com", "tags": ["标签", "中文"]}]
            elif name == "refresh_scrapers":
                data = {"status": "refreshed"}
            elif name == "ask_brightdata_assistant":
                data = {"answer": "官方说明", "dialog_id": "a" * 24}
            if data is not None:
                result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
            if name == "error":
                result["isError"] = True
        response = {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        if self.server.rpc_error:
            response = {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -1, "message": "TOKEN-fixture"}}
        blob = ("event: message\ndata: " + json.dumps(response, ensure_ascii=False) + "\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Mcp-Session-Id", "session-fixture")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)


class BrightDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="omini-brightdata-test-")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockMCP)
        self.server.calls = []
        self.server.rpc_error = False
        self.server.protocol_seen = ""
        self.server.progress_state = "ready"
        self.server.started = threading.Event()
        self.server.release = threading.Event()
        self.server.release.set()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}/mcp?token=TOKEN-fixture"
        self.page = BrightDataPage(Path(self.temp.name) / "config.json")
        self.page.endpoint.setText("https://mcp.brightdata.com/mcp?token=TOKEN-fixture")

    def tearDown(self):
        self.server.release.set()
        self.wait_page()
        self.page.stop()
        self.page.close()
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def wait_page(self):
        deadline = time.monotonic() + 6
        while self.page.worker is not None and time.monotonic() < deadline:
            self.qt.processEvents()
            time.sleep(0.01)
        self.assertIsNone(self.page.worker)

    def local_client(self, endpoint, **kwargs):
        return BrightDataClient(self.base, timeout=3, **kwargs)

    def test_session_negotiation_and_paginated_tools(self):
        client = self.local_client("")
        try:
            client.initialize()
            self.assertEqual([t["name"] for t in client.list_tools()], ["search_engine", "scrape_as_markdown"])
            result = client.call("scrape_as_markdown", {"url": "https://example.com"})
            self.assertIn("网页正文", result["content"][0]["text"])
            self.assertEqual(self.server.protocol_seen, "2024-11-05")
        finally:
            client.close()

    def test_buttons_single_and_batch_tools(self):
        with patch("brightdata_page.BrightDataClient", side_effect=self.local_client):
            self.page.engine.setCurrentIndex(1)
            self.page.country.setText("US")
            for text, expected in (("天气", "search_engine"), ("天气\n新闻", "search_engine_batch")):
                self.page.queries.setPlainText(text)
                self.page.search_button.click()
                self.wait_page()
                sent = self.server.calls[-1]["params"]
                self.assertEqual(sent["name"], expected)
                query = sent["arguments"].get("queries", [sent["arguments"]])[0]
                self.assertEqual(query["engine"], "bing")
                self.assertEqual(query["geo_location"], "us")
            for text, expected in (("https://example.com", "scrape_as_markdown"),
                                   ("https://example.com\nhttps://example.org", "scrape_batch")):
                self.page.urls.setPlainText(text)
                self.page.scrape_button.click()
                self.wait_page()
                self.assertEqual(self.server.calls[-1]["params"]["name"], expected)
            self.assertIn("网页正文", self.page.output.toPlainText())

    def test_validation_sends_no_requests(self):
        self.page.queries.setPlainText("\n".join(str(i) for i in range(11)))
        self.page.search_button.click()
        self.assertIn("10", self.page.status.text())
        self.page.urls.setPlainText("example.com")
        self.page.scrape_button.click()
        self.assertIn("http(s)", self.page.status.text())
        with self.assertRaises(BrightDataError):
            build_request("search", "hello", country="12")
        self.assertEqual(self.server.calls, [])

    def test_saved_connection_is_encrypted_and_reloadable(self):
        self.page.save_button.click()
        stored = self.page.store.path.read_text(encoding="utf-8")
        self.assertNotIn("TOKEN-fixture", stored)
        self.assertNotIn("mcp.brightdata.com", stored)
        self.assertEqual(BrightDataStore(self.page.store.path).endpoint, endpoint_from_token(self.page.token.text()))
        self.assertEqual(self.page.endpoint.echoMode(), QLineEdit.EchoMode.Password)

    def test_raw_token_and_existing_config_migrate_to_token_field(self):
        self.page.token.setText("TOKEN raw&value")
        self.page.save_button.click()
        self.assertFalse(self.page.store.path.exists())
        self.page.token.setText("TOKEN&value")
        self.page.save_button.click()
        self.assertIn("TOKEN%26value", self.page.store.endpoint)
        reloaded = BrightDataPage(self.page.store.path)
        self.assertEqual(reloaded.token.text(), "TOKEN&value")
        self.assertNotIn("TOKEN&value", self.page.store.path.read_text(encoding="utf-8"))
        reloaded.close()

    def test_search_pagination_and_result_link_to_scrape(self):
        with patch("brightdata_page.BrightDataClient", side_effect=self.local_client):
            self.page.queries.setPlainText("商品")
            self.page.search_button.click()
            self.wait_page()
            self.assertTrue(self.page.next_button.isEnabled())
            self.page.next_button.click()
            self.wait_page()
            self.assertEqual(self.server.calls[-1]["params"]["arguments"]["cursor"], "page-two")
            self.assertFalse(self.page.next_button.isEnabled())
        self.page.result_to_scrape(0, 1)
        self.assertEqual(self.page.urls.toPlainText(), "https://example.com")
        self.assertEqual(self.page.tabs.currentIndex(), 1)

    def prepare_scraper(self, discover=False):
        self.page.catalog_query.setText("products")
        self.page.find_button.click()
        self.wait_page()
        self.assertEqual(self.page.dataset.text(), "dataset-fixture")
        if discover:
            self.page.method.setCurrentIndex(1)
        self.page.details_button.click()
        self.wait_page()
        self.assertTrue(self.page.run_button.isEnabled())
        self.assertEqual(self.page.json_inputs.toPlainText(), "[]")

    def test_scraper_required_fields_batch_and_task_recovery(self):
        self.page.auto_poll.setChecked(False)
        with patch("brightdata_page.BrightDataClient", side_effect=self.local_client):
            self.prepare_scraper()
            before = len(self.server.calls)
            self.page.run_button.click()
            self.assertEqual(len(self.server.calls), before)
            self.assertIn("有效记录", self.page.status.text())
            self.page.fields["url"][1].setText("https://example.com/one")
            boolean = self.page.fields["all_variations"][1]
            boolean.setCurrentIndex(boolean.findData(False))
            self.page.add_input_button.click()
            self.page.fields["url"][1].setText("https://example.com/two")
            self.page.add_input_button.click()
            self.page.run_button.click()
            self.wait_page()
            args = self.server.calls[-1]["params"]["arguments"]
            self.assertEqual(len(args["input"]), 2)
            self.assertIs(args["input"][0]["all_variations"], False)
            self.assertNotIn("limit_per_input", args)
            self.assertEqual(self.page.snapshot.text(), "snapshot-fixture")
            self.assertEqual(self.page.tabs.currentIndex(), 3)
            self.page.results_button.click()
            self.wait_page()
        self.assertEqual(self.page.tasks.items[0]["status"], "ready")
        self.assertEqual(self.page.records[0]["title"], "测试商品")
        reloaded = BrightDataPage(self.page.store.path)
        self.assertEqual(reloaded.task_table.rowCount(), 1)
        self.assertEqual(reloaded.tasks.items[0]["snapshot_id"], "snapshot-fixture")
        self.assertFalse(reloaded.poll_timer.isActive())
        reloaded.close()

    def test_discovery_limit_auto_progress_results_and_pause(self):
        with patch("brightdata_page.BrightDataClient", side_effect=self.local_client):
            self.prepare_scraper(discover=True)
            self.page.fields["keyword"][1].setText("商品关键词")
            self.page.record_limit.setValue(3)
            self.page.run_button.click()
            self.wait_page()
            args = self.server.calls[-1]["params"]["arguments"]
            self.assertEqual(args["limit_per_input"], 3)
            self.assertTrue(self.page.poll_timer.isActive())
            self.page.poll_progress()
            deadline = time.monotonic() + 5
            while (self.page.worker is not None or not self.page.records) and time.monotonic() < deadline:
                self.qt.processEvents()
                time.sleep(0.01)
            self.assertEqual(self.page.records[0]["title"], "测试商品")
            self.assertEqual(self.server.calls[-1]["params"]["name"], "get_scraper_results")
            self.assertFalse(self.page.poll_timer.isActive())
            self.page.auto_poll.setChecked(False)
            self.assertFalse(self.page.poll_timer.isActive())

    def test_method_change_invalidates_inputs_and_advanced_validation(self):
        with patch("brightdata_page.BrightDataClient", side_effect=self.local_client):
            self.prepare_scraper()
            self.page.input_tabs.setCurrentIndex(1)
            for text in ('{', '[{}]', '[{"url":"bad-url"}]', '[{"url":"https://example.com","all_variations":"no"}]'):
                before = len(self.server.calls)
                self.page.json_inputs.setPlainText(text)
                self.page.run_button.click()
                self.assertEqual(len(self.server.calls), before)
                self.assertIsNone(self.page.worker)
            self.page.method.setCurrentIndex(1)
            self.assertIsNone(self.page.loaded_details)
            self.assertFalse(self.page.run_button.isEnabled())

    def test_refresh_official_help_and_csv_export(self):
        with patch("brightdata_page.BrightDataClient", side_effect=self.local_client):
            self.page.refresh_button.click()
            self.wait_page()
            self.assertEqual(self.server.calls[-1]["params"]["name"], "refresh_scrapers")
            self.page.help_question.setPlainText("如何设置采集？")
            self.page.dialog_id.setText("a" * 24)
            self.page.help_button.click()
            self.wait_page()
            self.assertEqual(self.server.calls[-1]["params"]["name"], "ask_brightdata_assistant")
            self.assertEqual(self.server.calls[-1]["params"]["arguments"]["dialog_id"], "a" * 24)
            self.page.snapshot.setText("snapshot-fixture")
            self.page.results_button.click()
            self.wait_page()
        target = Path(self.temp.name) / "results.csv"
        with patch("brightdata_page.QFileDialog.getSaveFileName", return_value=(str(target), "CSV 表格 (*.csv)")):
            self.page.export_button.click()
        with target.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "测试商品")
        self.assertEqual(json.loads(rows[0]["tags"]), ["标签", "中文"])

    def test_connect_copy_and_json_export(self):
        with patch("brightdata_page.BrightDataClient", side_effect=self.local_client):
            self.page.test_button.click()
            self.wait_page()
        self.assertIn("2 个工具", self.page.status.text())
        self.page.copy_button.click()
        self.assertIn("scrape_as_markdown", self.qt.clipboard().text())
        target = Path(self.temp.name) / "result.json"
        with patch("brightdata_page.QFileDialog.getSaveFileName", return_value=(str(target), "JSON (*.json)")):
            self.page.export_button.click()
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["tool_count"], 2)

    def test_error_preserves_previous_result_without_leaking_token(self):
        self.page.output.setPlainText("此前结果")
        self.server.rpc_error = True
        with patch("brightdata_page.BrightDataClient", side_effect=self.local_client):
            self.page.test_button.click()
            self.wait_page()
        self.assertNotIn("TOKEN-fixture", self.page.status.text())
        self.assertEqual(self.page.output.toPlainText(), "此前结果")
        self.assertTrue(self.page.test_button.isEnabled())

    def test_shutdown_ignores_late_results(self):
        self.server.release.clear()
        with patch("brightdata_page.BrightDataClient", side_effect=self.local_client):
            self.page.urls.setPlainText("https://example.com")
            self.page.scrape_button.click()
            self.assertTrue(self.server.started.wait(3))
            self.page.stop()
            self.server.release.set()
            self.wait_page()
        self.assertIsNone(self.page.last_result)


if __name__ == "__main__":
    unittest.main()
