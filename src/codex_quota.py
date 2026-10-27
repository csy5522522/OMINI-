"""Normalize Codex usage snapshots without assuming every plan has both windows."""
from __future__ import annotations

from datetime import datetime
import math
import time


USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (ValueError, TypeError, OverflowError):
        return None


def reset_timestamp(window, now):
    value = window.get("reset_at", window.get("resetAt"))
    parsed = number(value)
    if parsed is not None and parsed > 0:
        if parsed > 10**12: parsed /= 1000
        return int(parsed) if parsed < 253402300800 else None
    if isinstance(value, str):
        try:
            stamp = int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
            return stamp if 0 < stamp < 253402300800 else None
        except (ValueError, OverflowError, OSError):
            pass
    offset = number(window.get("reset_after_seconds", window.get("resetAfterSeconds")))
    if offset is not None and 0 <= offset < 10**9:
        return int(now + offset)
    return None


def parse_codex_quota(payload, *, now=None):
    if not isinstance(payload, dict):
        raise ValueError("额度响应格式有误，请稍后重试。")
    now = int(time.time()) if now is None else int(now)
    rate = payload.get("rate_limit", payload.get("rateLimit"))
    if rate is not None and not isinstance(rate, dict):
        raise ValueError("额度窗口格式有误，请稍后重试。")
    if rate is None and not any(key in payload for key in ("plan_type", "planType", "rate_limit", "rateLimit")):
        raise ValueError("响应未包含 Codex 额度信息。")
    result = {"five_hour": None, "weekly": None, "other_windows": [], "checked_at": now,
              "plan": str(payload.get("plan_type", payload.get("planType", "")))[:40], "error": ""}
    for position, keys in enumerate((("primary_window", "primaryWindow"), ("secondary_window", "secondaryWindow"))):
        raw = (rate or {}).get(keys[0], (rate or {}).get(keys[1]))
        if not isinstance(raw, dict): continue
        seconds = number(raw.get("limit_window_seconds", raw.get("limitWindowSeconds")))
        if seconds is None:
            minutes = number(raw.get("window_minutes", raw.get("windowMinutes")))
            if minutes is not None: seconds = minutes * 60
        used = number(raw.get("used_percent", raw.get("usedPercent")))
        if used is not None and not 0 <= used <= 100: used = None
        window = {"used_percent": used, "remaining_percent": 100 - used if used is not None else None,
                  "reset_at": reset_timestamp(raw, now), "window_seconds": seconds}
        # A free plan may return a weekly primary window; a monthly window is not weekly.
        kind = {18000: "five_hour", 604800: "weekly"}.get(seconds)
        if seconds is None: kind = "five_hour" if position == 0 else "weekly"
        if kind and result[kind] is None:
            result[kind] = window
        else:
            result["other_windows"].append(window)
    return result


def quota_cell(snapshot, kind):
    if not snapshot:
        return "待查询", "点击「查询所选额度」或「查询全部额度」。"
    window = snapshot.get(kind)
    if not window:
        return ("查询失败" if snapshot.get("error") else "未返回"), snapshot.get("error") or "此账号未返回该周期额度。"
    remaining = window.get("remaining_percent")
    text = f"剩余 {remaining:g}%" if remaining is not None else "未返回百分比"
    reset = window.get("reset_at")
    if reset:
        label = datetime.fromtimestamp(reset).astimezone().strftime("%m-%d %H:%M")
        text += f"\n{label} 重置"
    checked = datetime.fromtimestamp(snapshot["checked_at"]).astimezone().strftime("%m-%d %H:%M:%S")
    tip = f"查询于 {checked}；额度为该时刻快照。"
    if snapshot.get("plan"): tip += "\n套餐：" + snapshot["plan"]
    if remaining is not None: tip += f"\n已用 {window['used_percent']:g}%，剩余 {remaining:g}%。"
    if snapshot.get("error") or snapshot.get("cached"):
        text = "上次 " + text
    if snapshot.get("error"):
        tip += "\n本次查询失败：" + snapshot["error"]
    elif snapshot.get("cached"):
        tip += "\n已从本地恢复；可启动服务后查询最新额度。"
    return text, tip
