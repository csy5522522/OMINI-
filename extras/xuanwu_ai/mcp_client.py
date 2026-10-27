"""Small MCP client for stdio and Streamable HTTP servers."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests


PROTOCOL_VERSION = "2025-11-25"
MAX_TOOL_COUNT = 100


class MCPError(ValueError):
    pass


def _safe_name(value):
    normalized = re.sub(r"[^a-zA-Z0-9_]", "_", str(value)).strip("_") or "server"
    if len(normalized) > 42:
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
        normalized = normalized[:33] + "_" + digest
    return normalized


def _schema(value):
    if not isinstance(value, dict) or value.get("type") != "object":
        return {"type": "object", "properties": {}, "additionalProperties": True}
    result = {"type": "object", "properties": {}}
    properties = value.get("properties", {})
    if isinstance(properties, dict):
        for key, definition in list(properties.items())[:80]:
            if not isinstance(key, str) or not isinstance(definition, dict):
                continue
            field = {k: definition[k] for k in ("type", "description", "enum", "items") if k in definition}
            if field.get("type") not in ("string", "number", "integer", "boolean", "array", "object"):
                field["type"] = "string"
            result["properties"][key[:100]] = field
    required = value.get("required", [])
    if isinstance(required, list):
        result["required"] = [key for key in required if isinstance(key, str) and key in result["properties"]]
    if "additionalProperties" in value:
        result["additionalProperties"] = bool(value["additionalProperties"])
    else:
        result["additionalProperties"] = False
    return result


class _Stdio:
    def __init__(self, config, timeout):
        command = str(config.get("command", "")).strip()
        args = config.get("args", [])
        if not command or not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise MCPError("stdio MCP 需要填写 command 和字符串 args")
        env = {}
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA",
                    "LOCALAPPDATA", "PROGRAMDATA", "HOME", "LANG"):
            if key in os.environ:
                env[key] = os.environ[key]
        configured_env = config.get("env", {})
        if isinstance(configured_env, dict):
            for key, value in configured_env.items():
                if isinstance(key, str) and isinstance(value, str):
                    env[key] = value
        executable = shutil.which(command) or command
        launch = [executable, *args]
        if os.name == "nt" and Path(executable).suffix.lower() in (".cmd", ".bat"):
            # npm-based MCP servers are usually exposed as .cmd shims on Windows.
            command_line = subprocess.list2cmdline(launch)
            launch = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", f'"{command_line}"']
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(
                launch, cwd=config.get("cwd") or None, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=flags,
            )
        except OSError as error:
            raise MCPError(f"启动 MCP 服务失败：{error}") from error
        self.timeout = timeout
        self.lines = queue.Queue()
        self.counter = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.process.stdout:
                self.lines.put(line)
        finally:
            self.lines.put(None)

    def request(self, method, params=None, notification=False):
        if self.process.poll() is not None:
            raise MCPError("MCP stdio 进程已退出")
        self.counter += 1
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        request_id = None
        if not notification:
            request_id = self.counter
            body["id"] = request_id
        try:
            self.process.stdin.write(json.dumps(body, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (OSError, BrokenPipeError) as error:
            raise MCPError("写入 MCP stdio 请求失败") from error
        if notification:
            return {}
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty as error:
                raise MCPError(f"MCP {method} 等待超时") from error
            if line is None:
                raise MCPError("MCP stdio 进程已关闭输出")
            try:
                response = json.loads(line)
            except (ValueError, TypeError):
                continue
            if response.get("id") != request_id:
                continue
            if response.get("error"):
                error = response["error"]
                raise MCPError(str(error.get("message", error))[:800])
            result = response.get("result")
            return result if isinstance(result, dict) else {}
        raise MCPError(f"MCP {method} 等待超时")

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


class _HTTP:
    def __init__(self, config, timeout):
        self.url = str(config.get("url", "")).strip()
        parsed = urlsplit(self.url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise MCPError("Streamable HTTP MCP 地址需要使用有效的 http(s) URL")
        self.timeout = timeout
        self.session_id = ""
        self.protocol_version = PROTOCOL_VERSION
        self.counter = 0
        self.session = requests.Session()
        self.session.trust_env = False
        headers = config.get("headers", {})
        self.headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if isinstance(headers, dict):
            for key, value in headers.items():
                if isinstance(key, str) and isinstance(value, str):
                    self.headers[key] = value

    def request(self, method, params=None, notification=False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        request_id = None
        if not notification:
            self.counter += 1
            request_id = self.counter
            body["id"] = request_id
        headers = dict(self.headers)
        if self.session_id:
            headers["MCP-Session-Id"] = self.session_id
            headers["MCP-Protocol-Version"] = self.protocol_version
        try:
            response = self.session.post(self.url, json=body, headers=headers, timeout=self.timeout)
        except requests.RequestException as error:
            raise MCPError(f"MCP HTTP 请求失败：{error}") from error
        if response.headers.get("MCP-Session-Id"):
            self.session_id = response.headers["MCP-Session-Id"]
        if response.headers.get("MCP-Protocol-Version"):
            self.protocol_version = response.headers["MCP-Protocol-Version"]
        if notification and response.status_code in (200, 202, 204):
            return {}
        if response.status_code == 202:
            raise MCPError(f"MCP {method} 请求已接收，但服务器未返回结果")
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            detail = response.text[:500]
            raise MCPError(f"MCP HTTP 返回 {response.status_code}：{detail}") from error
        content_type = response.headers.get("Content-Type", "")
        try:
            if "text/event-stream" in content_type:
                payload = None
                for line in response.iter_lines(decode_unicode=True):
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", "replace")
                    if line.startswith("data:"):
                        try:
                            payload = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        if payload.get("id") == request_id:
                            break
                result = payload or {}
            else:
                result = response.json() if response.content else {}
        except (ValueError, requests.RequestException) as error:
            raise MCPError("MCP HTTP 响应不是有效 JSON-RPC") from error
        if not notification and result.get("id") != request_id:
            raise MCPError(f"MCP {method} 响应编号不匹配")
        if result.get("error"):
            error = result["error"]
            raise MCPError(str(error.get("message", error))[:800])
        payload = result.get("result", {})
        return payload if isinstance(payload, dict) else {}

    def close(self):
        if self.session_id:
            try:
                self.session.delete(self.url, headers={**self.headers, "MCP-Session-Id": self.session_id},
                                    timeout=min(3, self.timeout))
            except requests.RequestException:
                pass
        self.session.close()


class MCPConnection:
    def __init__(self, config, timeout=15):
        self.config = config
        self.transport = config.get("transport", "stdio")
        self.connection = _HTTP(config, timeout) if self.transport == "streamable_http" else _Stdio(config, timeout)

    def initialize(self):
        result = self.connection.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "xuanwu-ai", "version": "1.2.0"},
        })
        version = result.get("protocolVersion")
        if isinstance(version, str) and version:
            if isinstance(self.connection, _HTTP):
                self.connection.protocol_version = version
        self.connection.request("notifications/initialized", notification=True)

    def list_tools(self):
        tools, cursor = [], None
        for _ in range(10):
            params = {"cursor": cursor} if cursor else {}
            result = self.connection.request("tools/list", params)
            page = result.get("tools", [])
            if isinstance(page, list):
                tools.extend(item for item in page if isinstance(item, dict))
            cursor = result.get("nextCursor")
            if not cursor or len(tools) >= MAX_TOOL_COUNT:
                break
        return tools[:MAX_TOOL_COUNT]

    def call_tool(self, name, arguments):
        return self.connection.request("tools/call", {"name": name, "arguments": arguments})

    def close(self):
        self.connection.close()


class MCPManager:
    @staticmethod
    def _connect(config):
        connection = MCPConnection(config, timeout=15)
        try:
            connection.initialize()
            return connection
        except Exception:
            connection.close()
            raise

    def discover(self, servers):
        schemas, mapping, status = [], {}, []
        seen = set()
        for config in servers if isinstance(servers, list) else []:
            if not isinstance(config, dict) or not config.get("enabled", True):
                continue
            server_name = str(config.get("name", "")).strip()
            row = {"name": server_name, "transport": config.get("transport", "stdio"), "tools": []}
            if not server_name:
                row["error"] = "MCP 服务名称为空"
                status.append(row)
                continue
            try:
                connection = self._connect(config)
                try:
                    definitions = connection.list_tools()
                finally:
                    connection.close()
                row["tools"] = [{"name": item.get("name", ""),
                                 "description": str(item.get("description", ""))[:400],
                                 "read_only": bool((item.get("annotations") or {}).get("readOnlyHint"))}
                                for item in definitions if isinstance(item.get("name"), str)]
                for item in definitions:
                    if len(schemas) >= MAX_TOOL_COUNT:
                        break
                    original_name = item.get("name")
                    if not isinstance(original_name, str) or not original_name:
                        continue
                    alias = f"mcp_{_safe_name(server_name)}_{_safe_name(original_name)}"
                    if len(alias) > 64:
                        alias = alias[:55] + "_" + hashlib.sha1(alias.encode()).hexdigest()[:8]
                    if alias in seen:
                        alias = alias[:55] + "_" + hashlib.sha1((server_name + original_name).encode()).hexdigest()[:8]
                    seen.add(alias)
                    mapping[alias] = {"server": config, "tool_name": original_name,
                                      "read_only": bool((item.get("annotations") or {}).get("readOnlyHint"))}
                    schemas.append({"type": "function", "function": {
                        "name": alias,
                        "description": f"[{server_name}] {str(item.get('description', original_name))[:800]}",
                        "parameters": _schema(item.get("inputSchema")),
                    }})
            except Exception as error:
                row["error"] = str(error).splitlines()[0][:500]
            status.append(row)
        return schemas, mapping, status

    def call(self, config, tool_name, arguments):
        connection = self._connect(config)
        try:
            result = connection.call_tool(tool_name, arguments)
        finally:
            connection.close()
        return {"ok": not bool(result.get("isError")), "result": result}
