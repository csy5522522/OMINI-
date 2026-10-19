"""Local Douyin probe. Reports metadata/status only, never Cookie values."""
from __future__ import annotations

import http.cookiejar
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import make_runtime_cookie_file, redact_log_line
from curl_cffi import requests


def main() -> None:
    source = "https://v.douyin.com/tr651XRKHfk/"
    original = Path(r"C:\Users\SeanCao\Documents\cookies.txt")
    temporary = make_runtime_cookie_file(str(original), source, "douyin-probe")
    cookie_path = temporary or original
    try:
        jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
        with cookie_path.open(encoding="utf-8-sig") as cookie_stream:
            jar._really_load(cookie_stream, str(cookie_path), True, True)
        result = subprocess.run(
            [str(Path(__file__).resolve().parents[1] / "tools" / "yt-dlp.exe"),
             "--simulate", "--dump-single-json", "--no-playlist", "--socket-timeout", "15",
             "--cookies", str(cookie_path), source],
            capture_output=True, encoding="utf-8", errors="replace", timeout=75,
        )
        print("yt-dlp exit:", result.returncode)
        if result.returncode == 0:
            info = json.loads(result.stdout)
            print("metadata:", {key: info.get(key) for key in ("id", "title", "duration")})
            print("formats:", len(info.get("formats", [])))
        else:
            for line in result.stderr.splitlines():
                print(redact_log_line(line))

        with requests.Session(impersonate="chrome", cookies=jar) as session:
            response = session.get(source, timeout=20)
            print("share redirect:", response.status_code, urlparse(response.url).path,
                  "bytes:", len(response.content))
            match = re.search(r"/(?:video|note)/(\d+)", response.url)
            if not match:
                print("No video ID in redirect")
                return
            video_id = match[1]
            mobile_ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1"
            for url, extra_headers in (
                (f"https://www.douyin.com/video/{video_id}", {}),
                (f"https://www.iesdouyin.com/share/video/{video_id}/", {"User-Agent": mobile_ua}),
                (f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={video_id}", {}),
            ):
                response = session.get(url, timeout=20, headers={"Referer": "https://www.douyin.com/", **extra_headers})
                body = response.text
                markers = [marker for marker in ("RENDER_DATA", "_ROUTER_DATA", "__NEXT_DATA__",
                           "aweme_detail", "play_addr", "video_id", "验证码", "验证", "Forbidden")
                           if marker in body]
                print("page:", urlparse(url).hostname, urlparse(url).path, response.status_code,
                      "bytes:", len(response.content), "markers:", markers, "final path:", urlparse(response.url).path)
                script_ids = re.findall(r'<script[^>]+id=["\x27]([^"\x27]+)', body)
                print("script_ids:", script_ids[:20])
                render = re.search(r'<script[^>]+id=["\x27]RENDER_DATA["\x27][^>]*>(.*?)</script>', body, re.S)
                if render:
                    try:
                        data = json.loads(unquote(render[1]))
                        print("render payload:", len(render[1]), "root keys:", list(data)[:20])
                        describe_data(data)
                    except (json.JSONDecodeError, TypeError) as error:
                        print("render payload parse:", type(error).__name__)
                for name in ("_ROUTER_DATA", "RENDER_DATA"):
                    pos = body.find(name)
                    if pos >= 0:
                        # Print syntax around the variable only; avoid embedded user data.
                        snippet = body[max(0, pos - 40):pos + 120]
                        snippet = re.sub(r"[A-Za-z0-9_\-]{24,}", "[opaque]", snippet)
                        print("data syntax:", snippet)
                router = re.search(r'window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*;?\s*</script>', body, re.S)
                if router:
                    try:
                        describe_data(json.loads(router[1]))
                    except json.JSONDecodeError:
                        print("router JSON parse failed")
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def describe_data(data: object, path: str = "root", depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(data, dict):
        if any(key in data for key in ("aweme_id", "awemeId", "play_addr", "playAddr", "video")):
            print("video-shaped object:", path, "keys:", list(data)[:30])
            for key in ("aweme_id", "awemeId", "desc", "duration"):
                if key in data and isinstance(data[key], (str, int, float)):
                    print("metadata field:", key, str(data[key])[:120])
        for key, value in data.items():
            describe_data(value, f"{path}.{key}", depth + 1)
    elif isinstance(data, list):
        for index, value in enumerate(data[:10]):
            describe_data(value, f"{path}[{index}]", depth + 1)


if __name__ == "__main__":
    main()
