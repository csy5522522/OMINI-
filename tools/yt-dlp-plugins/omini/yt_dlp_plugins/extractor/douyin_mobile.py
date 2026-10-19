"""Resolve public Douyin videos from the site's mobile share-page metadata."""
from yt_dlp.extractor.tiktok import DouyinIE
from yt_dlp.utils import ExtractorError, determine_ext

# Register note URLs separately: yt-dlp's lazy extractor keeps the original
# DouyinIE URL matcher even when its implementation is overridden.
__all__ = ["OminiDouyinNoteIE"]

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 "
    "Mobile/15E148 Safari/604.1"
)


def find_video_detail(data, video_id):
    """Find only the requested video's published metadata, ignoring app config."""
    if isinstance(data, dict):
        if str(data.get("aweme_id")) == video_id and (
            isinstance(data.get("video"), dict) or data.get("images") or data.get("image_post_info")
        ):
            return data
        for value in data.values():
            detail = find_video_detail(value, video_id)
            if detail:
                return detail
    elif isinstance(data, list):
        for value in data:
            detail = find_video_detail(value, video_id)
            if detail:
                return detail
    return None


class _OminiDouyinIE(DouyinIE, plugin_name="omini-mobile"):
    _VALID_URL = r'https?://(?:(?:www\.)?douyin\.com/(?:video|note)/|(?:www\.)?iesdouyin\.com/share/(?:video|note)/)(?P<id>[0-9]+)'

    def _image_playlist(self, detail, video_id, share_url):
        images = detail.get("images") or (detail.get("image_post_info") or {}).get("images") or []
        entries = []
        title = detail.get("desc") or f"抖音图文 {video_id}"
        for index, image in enumerate(images, 1):
            image = image.get("display_image") or image
            urls = image.get("url_list") or image.get("download_url_list") or []
            formats = [{"url": url, "ext": determine_ext(url, "jpg"), "format_id": f"image-{i}"}
                       for i, url in enumerate(urls) if isinstance(url, str) and url.startswith(("https://", "http://"))]
            if formats:
                entries.append({"id": f"{video_id}-{index:03}", "title": f"{title[:100]} [{video_id}] - 图片{index:03}",
                                "formats": formats, "http_headers": {"User-Agent": MOBILE_USER_AGENT, "Referer": share_url}})
        if entries:
            self.to_screen(f"识别到图文作品，下载 {len(entries)} 张图片（不是视频）")
            return self.playlist_result(entries, video_id, title)

    def _real_extract(self, url):
        video_id = self._match_id(url)
        kind = "note" if "/note/" in url else "video"
        share_url = f"https://www.iesdouyin.com/share/{kind}/{video_id}/"
        for attempt in range(3):
            if attempt:
                self._sleep(1, video_id)
            webpage = self._download_webpage(
                share_url, video_id, note=f"Reading public mobile share page ({attempt + 1}/3)",
                headers={"User-Agent": MOBILE_USER_AGENT}, fatal=False,
            )
            if not webpage:
                continue
            router_data = self._search_json(
                r"window\._ROUTER_DATA\s*=", webpage, "mobile share data", video_id,
                fatal=False, default={},
            )
            detail = find_video_detail(router_data, video_id)
            if detail:
                images = self._image_playlist(detail, video_id, share_url)
                if images:
                    return images
                # The share-page schema uses null where the app API uses [].
                detail["video"]["bit_rate"] = detail["video"].get("bit_rate") or []
                info = self._parse_aweme_video_app(detail)
                if info.get("formats"):
                    info["http_headers"] = {
                        "User-Agent": MOBILE_USER_AGENT,
                        "Referer": share_url,
                    }
                    return info
        # Login-only or changed share pages still use yt-dlp's normal extractor
        # with the user's explicitly selected Cookie source.
        if kind == "note":
            raise ExtractorError("OMINI_BROWSER_REQUIRED: 图文作品需要浏览器解析；公开分享页暂未返回图片。", expected=True)
        return super()._real_extract(f"https://www.douyin.com/video/{video_id}")


class OminiDouyinNoteIE(DouyinIE):
    IE_NAME = "douyin:note+omini"
    _VALID_URL = r'https?://(?:(?:www\.)?douyin\.com/note/|(?:www\.)?iesdouyin\.com/share/note/)(?P<id>[0-9]+)'
    _real_extract = _OminiDouyinIE._real_extract
    _image_playlist = _OminiDouyinIE._image_playlist
