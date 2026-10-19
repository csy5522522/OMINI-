"""Huali timetable protocol and HAR import, independent of the Qt UI."""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

HOST = "jw.hualixy.edu.cn"
BASE_URL = f"https://{HOST}"
PAGE_URL = BASE_URL + "/jwglxt/kbcx/xskbcx_cxXskbcxIndex.html?gnmkdm=N2151&layout=default"
CLASS_PAGE_URL = BASE_URL + "/jwglxt/kbdy/bjkbdy_cxBjkbdyIndex.html?gnmkdm=N214505&layout=default"
LOGIN_URL = "https://sso.hualixy.edu.cn/heer_cas/#/login?service=https%3A%2F%2Fjw.hualixy.edu.cn%2Fsso%2Fddlogin"
COURSE_PATH = "/jwglxt/kbcx/xskbcx_cxXsgrkb.html"
PERIOD_PATH = "/jwglxt/kbcx/xskbcx_cxRjc.html"
TERMS = {"3": "第一学期", "12": "第二学期", "16": "第三学期"}
MAX_WEEKS = 60
MAX_PERIODS = 24


def number_ranges(value: str, limit: int, parity: bool = False) -> list[int]:
    """Parse ranges including mixed odd/even weeks, e.g. 1-16周(单),2-8周(双)."""
    result = set()
    text = str(value).replace("，", ",").replace("、", ",")
    text = re.sub(r"[～~—–至]", "-", text)
    for part in text.split(","):
        matches = list(re.finditer(r"(\d+)(?:\s*-\s*(\d+))?", part))
        odd = parity and "单" in part
        even = parity and "双" in part
        if odd and even:
            raise ValueError("周次的单双周格式有误。")
        for match in matches:
            first = int(match[1]); last = int(match[2] or match[1])
            if not 1 <= first <= last <= limit:
                raise ValueError("课表的周次或节次超出支持范围。")
            result.update(n for n in range(first, last + 1)
                          if not odd and not even or odd and n % 2 == 1 or even and n % 2 == 0)
    if not result:
        raise ValueError("课表缺少有效的周次或节次。")
    return sorted(result)


def parse_schedule(payload: dict, year: str, term: str, periods=None) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("kbList"), list):
        raise ValueError("返回内容不是学生课表，请登录教务系统后重新读取。")
    if not re.fullmatch(r"\d{4}", str(year)) or str(term) not in TERMS:
        raise ValueError("请选择有效的学年和学期。")
    courses = []
    for row in payload["kbList"]:
        if not isinstance(row, dict):
            raise ValueError("课程记录格式有误。")
        try:
            weekday = int(row.get("xqj", 0))
            if not 1 <= weekday <= 7:
                raise ValueError()
            slots = number_ranges(row.get("jcs") or row.get("jcor") or row.get("jc", ""), MAX_PERIODS)
            weeks = number_ranges(row.get("zcd", ""), MAX_WEEKS, parity=True)
        except (TypeError, ValueError) as error:
            raise ValueError("有课程缺少有效的星期、节次或周次，请重新导出课表 HAR。") from error
        # Store only display fields; student identity and raw server objects stay out of the cache.
        courses.append({
            "name": str(row.get("kcmc") or "未命名课程"),
            "teacher": str(row.get("xm") or ""), "location": str(row.get("cdmc") or ""),
            "campus": str(row.get("xqmc") or ""), "class_name": str(row.get("jxbmc") or ""),
            "credits": str(row.get("xf") or ""), "weekday": weekday,
            "periods": slots, "weeks": weeks, "week_text": str(row.get("zcd", "")),
        })
    times = {}
    for row in periods or []:
        try:
            slot = int(row.get("jcmc", ""))
        except (ValueError, TypeError, AttributeError):
            continue
        start, end = str(row.get("qssj", "")), str(row.get("jssj", ""))
        if 1 <= slot <= MAX_PERIODS and all(re.fullmatch(r"\d{2}:\d{2}", t) for t in (start, end)):
            times[str(slot)] = {"start": start, "end": end}
    return {"schema": 1, "year": str(year), "term": str(term), "courses": courses,
            "period_times": times, "updated_at": datetime.now().isoformat(timespec="seconds")}


def _har_json(entry):
    content = entry.get("response", {}).get("content", {})
    value = content.get("text", "")
    if content.get("encoding") == "base64":
        value = base64.b64decode(value, validate=True).decode("utf-8-sig")
    return json.loads(value)


def import_har(path: str | Path) -> dict:
    try:
        har = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        entries = har["log"]["entries"]
        if not isinstance(entries, list):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError("HAR 文件读取失败，请选择浏览器导出的网络记录。") from error
    candidates = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        request = entry.get("request", {}); url = urlsplit(request.get("url", ""))
        if url.hostname != HOST or url.path != COURSE_PATH or entry.get("response", {}).get("status") != 200:
            continue
        try:
            payload = _har_json(entry)
            if not isinstance(payload, dict) or not isinstance(payload.get("kbList"), list):
                continue
            post = request.get("postData", {})
            params = parse_qs(post.get("text", ""))
            params.update({p["name"]: [p["value"]] for p in post.get("params", [])})
            meta = payload.get("xsxx") or {}
            year = params.get("xnm", [meta.get("XNM", "")])[0]
            term = params.get("xqm", [meta.get("XQM", "")])[0]
            candidates.append((index, year, term, payload))
        except (ValueError, KeyError, TypeError, UnicodeError):
            continue
    if not candidates:
        raise ValueError("HAR 中没有学校课表响应。请在教务系统查询课表后，导出含响应内容的 HAR。")
    index, year, term, payload = candidates[-1]
    periods = []
    # Match the same semester and campus rather than using another query's bell times.
    campus = str(payload["kbList"][0].get("xqh_id", "")) if payload["kbList"] else ""
    matching = []
    for pos, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        request = entry.get("request", {}); url = urlsplit(request.get("url", ""))
        if url.hostname != HOST or url.path != PERIOD_PATH or entry.get("response", {}).get("status") != 200:
            continue
        post = request.get("postData", {}); params = parse_qs(post.get("text", ""))
        params.update({p["name"]: [p["value"]] for p in post.get("params", [])})
        if params.get("xnm") != [year] or params.get("xqm") != [term]:
            continue
        if campus and params.get("xqh_id") != [campus]:
            continue
        try:
            value = _har_json(entry)
            if isinstance(value, list):
                matching.append((abs(pos - index), value))
        except (ValueError, UnicodeError):
            pass
    if matching:
        periods = min(matching, key=lambda item: item[0])[1]
    return parse_schedule(payload, year, term, periods)


def normalize_cookie(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("cookie:"):
        value = value[7:].strip()
    if not value or any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise ValueError("请填写有效的教务系统 Cookie 请求头。")
    for part in value.split(";"):
        name, separator, content = part.strip().partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z0-9_!#$%&'*+.^`|~-]+", name) or not content:
            raise ValueError("Cookie 格式应为 name=value; name=value。")
    return value


class SchoolRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urlsplit(newurl)
        if url.scheme != "https" or url.hostname != HOST or url.port not in (None, 443):
            raise ValueError("教务系统跳转到登录服务，请点击「登录并读取」更新会话。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_schedule(cookie: str, year: str, term: str, cancelled=lambda: False, opener=None) -> dict:
    cookie = normalize_cookie(cookie)
    if not re.fullmatch(r"\d{4}", str(year)) or str(term) not in TERMS:
        raise ValueError("请选择有效的学年和学期。")
    opener = opener or build_opener(SchoolRedirectHandler())

    def post(path, values):
        if cancelled():
            raise RuntimeError("课表读取已停止。")
        request = Request(BASE_URL + path + "?gnmkdm=N2151", data=urlencode(values).encode("ascii"), headers={
            "Cookie": cookie, "Referer": PAGE_URL, "Origin": BASE_URL,
            "User-Agent": "Mozilla/5.0 OminiTimetable/1.0", "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8", "Accept": "application/json",
        })
        try:
            with opener.open(request, timeout=20) as response:
                data = response.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise ValueError("课表响应过大，请重新登录后重试。")
            return json.loads(data.decode("utf-8-sig"))
        except HTTPError as error:
            if error.code in (401, 403):
                raise ValueError("登录会话已过期，请点击「登录并读取」。") from error
            raise RuntimeError(f"教务系统请求失败（HTTP {error.code}），请稍后重试。") from error
        except (URLError, TimeoutError, OSError) as error:
            raise RuntimeError("教务系统连接失败，请检查网络后重试。") from error
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("登录会话已过期或返回登录页面，请点击「登录并读取」。") from error

    payload = post(COURSE_PATH, {"xnm": year, "xqm": term, "kzlx": "ck", "xsdm": "", "kclbdm": "", "kclxdm": ""})
    schedule = parse_schedule(payload, year, term)
    if cancelled():
        return schedule
    rows = payload["kbList"]
    campus = str(rows[0].get("xqh_id", "1")) if rows else "1"
    try:
        periods = post(PERIOD_PATH, {"xnm": year, "xqm": term, "xqh_id": campus})
        if isinstance(periods, list):
            schedule = parse_schedule(payload, year, term, periods)
    except (ValueError, RuntimeError):
        schedule["warning"] = "课表已读取，节次时间未获取；可稍后刷新。"
    return schedule


def validate_cache(value: dict) -> dict:
    """Validate and normalize cached data before it reaches table indexing."""
    if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("courses"), list):
        raise ValueError("课表缓存格式有误，请重新导入。")
    rows = []
    for course in value["courses"]:
        slots, weeks = course["periods"], course["weeks"]
        if not slots or not weeks or any(type(n) is not int for n in slots + weeks):
            raise ValueError("课表缓存格式有误，请重新导入。")
        rows.append({"kcmc": course["name"], "xm": course.get("teacher", ""), "cdmc": course.get("location", ""),
                     "xqmc": course.get("campus", ""), "jxbmc": course.get("class_name", ""), "xf": course.get("credits", ""),
                     "xqj": course["weekday"], "jcs": ",".join(map(str, slots)), "zcd": ",".join(map(str, weeks))})
    periods = [{"jcmc": k, "qssj": t["start"], "jssj": t["end"]} for k, t in value.get("period_times", {}).items()]
    result = parse_schedule({"kbList": rows}, value["year"], value["term"], periods)
    for old, course in zip(value["courses"], result["courses"]):
        course["week_text"] = str(old.get("week_text", course["week_text"]))
    result["updated_at"] = str(value.get("updated_at", ""))
    return result
