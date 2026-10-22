"""Stable feature IDs shared by the desktop and web settings."""
from __future__ import annotations

DESKTOP_FEATURES = {
    "downloads": "下载中心",
    "campus": "校园网自动登录",
    "timetable": "学校课表",
    "notes": "超级备忘录",
    "brightdata": "Bright Data",
    "sync": "文件夹同步",
    "omini_api": "Omini API",
    "ai_assistants": "AI 编程助手",
    "more": "更多工具",
}
WEB_FEATURES = {key: DESKTOP_FEATURES[key] for key in ("downloads", "timetable", "notes", "ai_assistants")}
PAGE_FEATURES = ("downloads", "campus", "timetable", "notes", None,
                 "brightdata", "sync", "omini_api", "ai_assistants", "more")


def enabled_features(settings, available=DESKTOP_FEATURES):
    saved = settings.get("features", {})
    if not isinstance(saved, dict):
        saved = {}
    return {key: saved.get(key, True) if type(saved.get(key, True)) is bool else True
            for key in available}


def validate_features(value, available):
    if not isinstance(value, dict) or any(key not in available or type(flag) is not bool
                                          for key, flag in value.items()):
        raise ValueError("功能选项应为有效的勾选状态。")
    return dict(value)
