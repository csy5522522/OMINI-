"""Bright Data button-based tools and encrypted per-user connection settings."""
from __future__ import annotations

import json
import csv
import io
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from campus_network import protect_password, unprotect_password


class BrightDataError(ValueError):
    pass


def token_from_endpoint(endpoint):
    return parse_qs(urlsplit(endpoint).query).get("token", [""])[0]


def endpoint_from_token(value):
    value = value.strip()
    if value.startswith(("http://", "https://")):
        return validate_endpoint(value)
    if not value or len(value) > 4096 or any(char.isspace() for char in value):
        raise BrightDataError("请填写有效的 Token，也可粘贴完整 MCP 地址。")
    return "https://mcp.brightdata.com/mcp?" + urlencode({"token": value})


def validate_endpoint(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "mcp.brightdata.com"
            or parsed.path.rstrip("/") != "/mcp" or parsed.username or parsed.password
            or parsed.fragment or parsed.port not in (None, 443)
            or not parse_qs(parsed.query).get("token")):
        raise BrightDataError("请输入包含 Token 的 Bright Data HTTPS MCP 地址。")
    return value


class BrightDataStore:
    def __init__(self, path):
        self.path = Path(path)
        self.endpoint = ""
        self.error = ""
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.endpoint = validate_endpoint(unprotect_password(data["endpoint_protected"]))
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                self.error = "连接配置读取失败，请重新粘贴地址并保存。"

    def save(self, endpoint):
        endpoint = validate_endpoint(endpoint)
        data = {"version": 1, "endpoint_protected": protect_password(endpoint)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        self.endpoint = endpoint
        self.error = ""


def build_request(kind, text, engine="google", country="", cursor=""):
    entries = [line.strip() for line in text.splitlines() if line.strip()]
    if not entries:
        raise BrightDataError("请先输入关键词。" if kind == "search" else "请先输入网页地址。")
    if len(entries) > 10:
        raise BrightDataError("每批最多 10 条，请分批处理。")
    if kind == "search":
        if engine not in ("google", "bing", "yandex"):
            raise BrightDataError("请选择有效的搜索引擎。")
        country = country.strip().lower()
        if country and (len(country) != 2 or not country.isascii() or not country.isalpha()):
            raise BrightDataError("国家代码需填写两个英文字母，例如 us、uk、cn。")
        queries = [{"query": entry, "engine": engine} for entry in entries]
        if country:
            for query in queries:
                query["geo_location"] = country
        if cursor.strip():
            if len(queries) != 1:
                raise BrightDataError("翻页时请只保留一条搜索关键词。")
            queries[0]["cursor"] = cursor.strip()
        return ("search_engine", queries[0]) if len(queries) == 1 else ("search_engine_batch", {"queries": queries})
    if kind != "scrape":
        raise BrightDataError("操作类型有误。")
    for entry in entries:
        parsed = urlsplit(entry)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise BrightDataError("每行需填写完整的 http(s) 网页地址。")
    return ("scrape_as_markdown", {"url": entries[0]}) if len(entries) == 1 else ("scrape_batch", {"urls": entries})


def result_text(result):
    pieces = []
    for item in result.get("content", []):
        if item.get("type") == "text":
            text = str(item.get("text", ""))
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except (ValueError, TypeError):
                pass
            pieces.append(text)
    if not pieces and result.get("structuredContent") is not None:
        pieces.append(json.dumps(result["structuredContent"], ensure_ascii=False, indent=2))
    return "\n\n".join(pieces) or json.dumps(result, ensure_ascii=False, indent=2)


def result_data(result):
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    for item in result.get("content", []):
        if item.get("type") == "text":
            try:
                data = json.loads(item.get("text", ""))
                if isinstance(data, (dict, list)):
                    return data
            except (ValueError, TypeError):
                pass
    return result


def result_records(data):
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("records", "data", "results", "organic", "organic_results", "items"):
            if isinstance(data.get(key), (list, dict)):
                rows = result_records(data[key])
                if rows:
                    return rows
    return []


def records_csv(records):
    if not records:
        raise BrightDataError("当前结果没有表格记录，请选择 Markdown 或 JSON 导出。")
    columns = list(dict.fromkeys(key for row in records for key in row))
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for row in records:
        writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                         for key, value in row.items()})
    return stream.getvalue()


def validate_scraper_inputs(details, inputs):
    if not isinstance(inputs, list) or not inputs or any(not isinstance(item, dict) or not item for item in inputs):
        raise BrightDataError("采集输入需至少包含一条有效记录。")
    for number, record in enumerate(inputs, 1):
        for field in details.get("input_fields", []):
            key = field.get("name", "")
            value = record.get(key)
            if field.get("required") and (value is None or value == ""):
                raise BrightDataError(f"第 {number} 条输入缺少必填项：{key}")
            if value is None or value == "":
                continue
            kind = field.get("type")
            if kind == "url" and (not isinstance(value, str) or urlsplit(value).scheme not in ("http", "https") or not urlsplit(value).hostname):
                raise BrightDataError(f"第 {number} 条输入的 {key} 需填写完整网页地址。")
            if kind == "boolean" and not isinstance(value, bool):
                raise BrightDataError(f"第 {number} 条输入的 {key} 需选择是或否。")
            if kind in ("integer", "number", "float") and (isinstance(value, bool) or not isinstance(value, (int, float)) or (kind == "integer" and not isinstance(value, int))):
                raise BrightDataError(f"第 {number} 条输入的 {key} 需填写数字。")
    return inputs


class BrightDataTasks:
    def __init__(self, path):
        self.path = Path(path)
        self.items = []
        self.error = ""
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    raise ValueError()
                self.items = [item for item in data if isinstance(item, dict) and item.get("snapshot_id")]
            except (OSError, ValueError, TypeError):
                self.error = "任务记录读取失败，原文件已保留；可手动填写任务 ID 查询。"

    def update(self, snapshot_id, **values):
        if self.error:
            raise BrightDataError(self.error)
        item = next((item for item in self.items if item["snapshot_id"] == snapshot_id), None)
        if item is None:
            item = {"snapshot_id": snapshot_id}
            self.items.insert(0, item)
        for key in ("status", "dataset_id", "method", "name", "started"):
            if key in values:
                item[key] = values[key]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.items, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        return item


class BrightDataClient:
    def __init__(self, endpoint, timeout=90, cancelled=None):
        self.endpoint = endpoint
        self.timeout = timeout
        self.cancelled = cancelled or threading.Event()
        self.session = requests.Session()
        self.session.trust_env = False
        self.session_id = ""
        self.protocol = "2025-11-25"
        self.counter = 0

    def request(self, method, params=None, notification=False):
        if self.cancelled.is_set():
            raise BrightDataError("任务已停止。")
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notification:
            self.counter += 1
            body["id"] = self.counter
        headers = {"Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers.update({"Mcp-Session-Id": self.session_id, "MCP-Protocol-Version": self.protocol})
        try:
            with self.session.post(self.endpoint, json=body, headers=headers,
                                   timeout=(10, self.timeout), stream=True, allow_redirects=False) as response:
                if response.headers.get("Mcp-Session-Id"):
                    self.session_id = response.headers["Mcp-Session-Id"]
                if notification and response.status_code in (200, 202, 204):
                    return {}
                if response.status_code != 200:
                    reasons = {401: "请检查连接 Token。", 403: "请检查账户权限和服务配额。",
                               404: "服务或会话不存在，请重试连接。", 429: "请求过于频繁，请稍后重试。"}
                    raise BrightDataError(f"服务返回 HTTP {response.status_code}。" + reasons.get(response.status_code, "请稍后重试。"))
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    payload = None
                    for line in response.iter_lines():
                        if self.cancelled.is_set():
                            raise BrightDataError("任务已停止。")
                        if line.startswith(b"data:"):
                            try:
                                candidate = json.loads(line[5:].strip())
                            except ValueError:
                                continue
                            if isinstance(candidate, dict) and candidate.get("id") == body.get("id"):
                                payload = candidate
                                break
                else:
                    payload = response.json()
                if not isinstance(payload, dict) or payload.get("id") != body.get("id"):
                    raise BrightDataError("服务响应不完整，请重试。")
                if payload.get("error"):
                    raise BrightDataError("服务处理请求失败，请检查输入、账户配额或稍后重试。")
                return payload.get("result", {})
        except requests.Timeout as error:
            raise BrightDataError("请求超时，请稍后重试或减少批量条数。") from error
        except requests.RequestException as error:
            raise BrightDataError("网络连接失败，请检查网络后重试。") from error
        except (ValueError, TypeError) as error:
            if isinstance(error, BrightDataError):
                raise
            raise BrightDataError("服务响应格式有误，请稍后重试。") from error

    def initialize(self):
        result = self.request("initialize", {"protocolVersion": self.protocol, "capabilities": {},
                              "clientInfo": {"name": "omini-toolbox", "version": "1.0.12"}})
        self.protocol = result.get("protocolVersion", self.protocol)
        self.request("notifications/initialized", notification=True)

    def list_tools(self):
        tools, cursor = [], None
        for _ in range(10):
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
        raise BrightDataError("服务工具列表过长，请稍后重试。")

    def call(self, name, arguments):
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self):
        if self.session_id and not self.cancelled.is_set():
            try:
                self.session.delete(self.endpoint, headers={"Mcp-Session-Id": self.session_id,
                                    "MCP-Protocol-Version": self.protocol}, timeout=(2, 3), allow_redirects=False)
            except requests.RequestException:
                pass
        self.session.close()
