"""Public note/video routing, photo batches and browser-result boundaries."""
import importlib.util
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from yt_dlp import YoutubeDL
from yt_dlp.utils import ExtractorError

from douyin_browser import media_info
from tests.test_queue import QueueButtonsTest
import app

spec = importlib.util.spec_from_file_location("omini_note_test", Path(__file__).resolve().parents[1] / "tools/yt-dlp-plugins/omini/yt_dlp_plugins/extractor/douyin_mobile.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class VideoExtractorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.requests.append((self.path, dict(self.headers)))
                if self.path == "/gone":
                    self.send_error(404)
                    return
                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Range", "bytes 0-0/1024")
                self.send_header("Content-Length", "1")
                self.end_headers()
                self.wfile.write(b"\x00")

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.requests.clear()
        self.ydl = YoutubeDL({"quiet": True, "socket_timeout": 5, "proxy": ""})
        self.addCleanup(self.ydl.close)
        self.ie = plugin._OminiDouyinIE(self.ydl)
        self.id = "7699017575996378395"
        self.url = f"https://www.douyin.com/video/{self.id}"

    def page(self, *paths):
        detail = {"aweme_id": self.id, "desc": "Playable video", "video": {
            "play_addr": {"url_list": [self.origin + path for path in paths]},
            "bit_rate": None, "duration": 15000,
        }}
        return "window._ROUTER_DATA = " + json.dumps({"item_list": [detail]})

    def test_404_share_address_falls_back_without_reloading_same_page(self):
        web_info = {"id": self.id, "formats": [{"url": "https://cdn.example/video.mp4"}]}
        with patch.object(self.ie, "_download_webpage", return_value=self.page("/gone")) as page, \
                patch.object(plugin.DouyinIE, "_real_extract", return_value=web_info) as fallback:
            result = self.ie._real_extract(self.url)
        self.assertIs(result, web_info)
        page.assert_called_once()
        fallback.assert_called_once_with(self.url)

    def test_206_video_survives_404_candidate_and_keeps_download_headers(self):
        with patch.object(self.ie, "_download_webpage", return_value=self.page("/gone", "/video")), \
                patch.object(plugin.DouyinIE, "_real_extract") as fallback:
            result = self.ie._real_extract(self.url)
        fallback.assert_not_called()
        self.assertEqual([fmt["url"] for fmt in result["formats"]], [self.origin + "/video"])
        self.assertEqual(result["duration"], 15)
        for _, headers in self.requests:
            self.assertEqual(headers["Range"], "bytes=0-0")
            self.assertEqual(headers["User-Agent"], plugin.MOBILE_USER_AGENT)
            self.assertEqual(headers["Referer"], f"https://www.iesdouyin.com/share/video/{self.id}/")
        self.assertNotIn("Range", result["http_headers"])


class NoteExtractorTest(unittest.TestCase):
    def setUp(self):
        self.ie = plugin.OminiDouyinNoteIE(YoutubeDL({"quiet": True}))
        self.id = "7694572876925841509"

    def test_note_and_mobile_url_matching(self):
        for url in [f"https://www.douyin.com/note/{self.id}?previous_page=app_code_link",
                    f"https://www.iesdouyin.com/share/note/{self.id}/"]:
            self.assertTrue(self.ie.suitable(url))
            self.assertEqual(self.ie._match_id(url), self.id)
        self.assertFalse(self.ie.suitable("https://example.com/note/123"))

    def test_all_images_no_video_required(self):
        detail = {"aweme_id": self.id, "desc": "Fixture photo batch", "images": [
            {"url_list": ["https://p3.douyinpic.com/one.jpeg"]},
            {"url_list": ["https://p3.douyinpic.com/two.jpeg"]}]}
        html = "window._ROUTER_DATA = " + json.dumps({"loaderData": {"note": {"item_list": [detail]}}})
        with patch.object(self.ie, "_download_webpage", return_value=html):
            result = self.ie._real_extract(f"https://www.douyin.com/note/{self.id}")
        self.assertEqual(result["_type"], "playlist")
        self.assertEqual(len(result["entries"]), 2)
        self.assertNotEqual(result["entries"][0]["title"], result["entries"][1]["title"])
        self.assertEqual(result["entries"][0]["formats"][0]["ext"], "jpeg")

    def test_challenge_becomes_browser_request_not_unsupported(self):
        with patch.object(self.ie, "_download_webpage", return_value="<html>verification</html>"), patch.object(self.ie, "_sleep"):
            with self.assertRaisesRegex(ExtractorError, "OMINI_BROWSER_REQUIRED"):
                self.ie._real_extract(f"https://www.douyin.com/note/{self.id}")

    def test_browser_media_validation_and_deduplication(self):
        data = {"url": f"https://www.douyin.com/note/{self.id}", "title": "Public note", "images": [
            "https://p3-pc-sign.douyinpic.com/one.jpeg?biz_tag=aweme_images",
            "https://p3-pc-sign.douyinpic.com/one.jpeg?biz_tag=aweme_images"]}
        info = media_info(data["url"], data)
        self.assertEqual(len(info["entries"]), 1)
        self.assertNotIn("Cookie", info["entries"][0]["http_headers"])
        data["images"] = ["https://example.com/one.jpg"]
        with self.assertRaises(ValueError):
            media_info(data["url"], data)
        data["url"] = "https://example.com/note/123"
        with self.assertRaises(ValueError):
            media_info(data["url"], data)

    def test_browser_playlist_is_not_cleaned_before_download(self):
        task = app.DownloadTask(id="test", type="video", source=f"https://www.douyin.com/note/{self.id}", output_dir=".")
        with patch.object(app, "find_engine", return_value="yt-dlp"):
            command = app.build_command(task, media_info_file="temporary-media.json")
        self.assertIn("--no-clean-info-json", command)
        self.assertEqual(command[-2:], ["--load-info-json", "temporary-media.json"])


class NoteQueueTest(QueueButtonsTest):
    def test_image_type_display_and_temp_cleanup(self):
        task = self.add_video()
        self.manager._on_output(task.id, "[douyin:note+omini] 识别到图文作品，下载 2 张图片（不是视频）")
        self.assertEqual(self.page.table.item(0, 2).text(), "图文")
        self.assertIn("2 张图片", task.title)
        self.assertIn("保存图片", self.page.queue_feedback.text())
        self.until(lambda: bool(self.processes))
        self.processes[-1].complete()
        self.until(lambda: task.status == "completed" and not self.manager.workers)
        info = media_info(task.source, {"url": "https://www.douyin.com/note/123", "images": ["https://p3.douyinpic.com/one.jpg"]})
        self.manager.attach_browser_media_and_retry(task.id, info)
        self.until(lambda: len(self.processes) == 2)
        self.assertTrue((app.APP_DIR / "runtime-media" / f"{task.id}.json").is_file())
        self.processes[-1].complete()
        self.until(lambda: task.status == "completed" and not self.manager.workers)
        self.assertFalse((app.APP_DIR / "runtime-media" / f"{task.id}.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
