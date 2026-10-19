"""Workspace file tools used by the Ollama agent loop."""
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path


def schema(name, description, fields, required):
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": {key: {"type": value} for key, value in fields.items()},
                       "required": required, "additionalProperties": False}}}


TOOLS = [
    schema("list_directory", "列出 path 指定目录中的文件和子目录；path 默认工作目录，完全访问权限下可使用任意本机路径。只列一层。", {"path": "string"}, []),
    schema("read_file", "读取文本文件并返回行号。完全访问权限下可读取任意本机路径。可指定 offset（起始行，默认1）及 limit（默认200，最多1000）。", {"path": "string", "offset": "integer", "limit": "integer"}, ["path"]),
    schema("search_files", "从 path 开始递归查找文件名或文本包含 query 的文件；path 默认工作目录，完全访问权限下可指定任意本机路径，最多30项。", {"query": "string", "path": "string"}, ["query"]),
    schema("write_file", "创建或覆盖 UTF-8 文本文件；完全访问权限下可写入任意本机路径。覆盖前自动备份并返回备份路径。", {"path": "string", "content": "string"}, ["path", "content"]),
    schema("create_directory", "创建目录；完全访问权限下可指定电脑上的任意本机路径。", {"path": "string"}, ["path"]),
    schema("move_path", "移动或重命名文件/文件夹；完全访问权限下 source 和 destination 可使用任意本机路径，目标须尚未存在。", {"source": "string", "destination": "string"}, ["source", "destination"]),
    schema("trash_file", "把单个文件移到玄武Ai备份区并返回可恢复路径；完全访问权限下可指定任意本机文件。", {"path": "string"}, ["path"]),
    schema("run_command", "在 Windows cmd.exe（或当前系统 shell）中执行一条命令，返回退出码和输出。cwd 默认工作目录；完全访问权限下可指定任意本机目录。", {"command": "string", "cwd": "string", "timeout_ms": "integer"}, ["command"]),
]
TOOL_NAMES = {item["function"]["name"] for item in TOOLS}


class FileTools:
    def __init__(self, workspace, data_directory, full_access=False):
        self.root = Path(workspace).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError("工作目录不存在，请在设置中选择文件夹")
        self.backups = Path(data_directory).resolve() / "backups"
        self.full_access = bool(full_access)

    def path(self, value="."):
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("路径格式有误")
        value = Path(value)
        if not self.full_access and ":" in str(value)[len(value.drive):]:
            raise ValueError("路径格式有误")
        resolved = (value if value.is_absolute() else self.root / value).resolve()
        if not self.full_access and not resolved.is_relative_to(self.root):
            raise ValueError("路径超出所选工作目录")
        return resolved

    def backup(self, target):
        self.backups.mkdir(parents=True, exist_ok=True)
        copy = self.backups / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}-{target.name}"
        shutil.copy2(target, copy)
        return str(copy)

    @staticmethod
    def text(target):
        if target.stat().st_size > 2_000_000:
            raise ValueError("文本文件超过 2 MB，请拆分后读取")
        raw = target.read_bytes()
        if b"\0" in raw[:8192]:
            raise ValueError("这是二进制文件，请上传为附件或选择文本文件")
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                return raw.decode(encoding)
            except UnicodeError:
                pass
        raise ValueError("文件编码暂不支持，请使用 UTF-8 文本")

    def execute(self, name, arguments, cancel=None):
        try:
            if name not in TOOL_NAMES or not isinstance(arguments, dict):
                raise ValueError("工具名或参数有误")
            if cancel and cancel.is_set():
                raise ValueError("操作已停止")
            if name == "run_command":
                command = arguments.get("command")
                if not isinstance(command, str) or not command.strip() or len(command) > 8000:
                    raise ValueError("命令为空或超过 8000 个字符")
                cwd = self.path(arguments.get("cwd", "."))
                if not cwd.is_dir():
                    raise ValueError("命令工作目录不存在")
                timeout_ms = max(1000, min(120000, int(arguments.get("timeout_ms", 30000))))
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                process = subprocess.Popen(
                    command,
                    cwd=str(cwd),
                    shell=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=flags,
                )
                output_parts = deque()
                output_state = {"size": 0, "total": 0}
                output_lock = threading.Lock()

                def collect_output():
                    while True:
                        chunk = process.stdout.read(4096)
                        if not chunk:
                            return
                        with output_lock:
                            output_parts.append(chunk)
                            output_state["size"] += len(chunk)
                            output_state["total"] += len(chunk)
                            while output_state["size"] > 30000 and output_parts:
                                excess = output_state["size"] - 30000
                                first = output_parts[0]
                                if len(first) <= excess:
                                    output_state["size"] -= len(output_parts.popleft())
                                else:
                                    output_parts[0] = first[excess:]
                                    output_state["size"] -= excess

                reader = threading.Thread(target=collect_output, daemon=True)
                reader.start()
                deadline = time.monotonic() + timeout_ms / 1000
                while True:
                    if cancel and cancel.is_set():
                        if process.poll() is None:
                            process.kill()
                        process.wait()
                        reader.join(timeout=2)
                        with output_lock:
                            output = "".join(output_parts)
                            truncated = output_state["total"] > 30000
                        return {"ok": False, "error": "命令已停止", "command": command,
                                "cwd": str(cwd), "output": output, "truncated": truncated}
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if process.poll() is None:
                            process.kill()
                        process.wait()
                        reader.join(timeout=2)
                        with output_lock:
                            output = "".join(output_parts)
                            truncated = output_state["total"] > 30000
                        return {"ok": False, "error": "命令运行超过时间限制", "command": command,
                                "cwd": str(cwd), "timeout_ms": timeout_ms, "output": output,
                                "truncated": truncated}
                    try:
                        process.wait(timeout=min(0.25, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                reader.join(timeout=2)
                with output_lock:
                    output = "".join(output_parts)
                    truncated = output_state["total"] > 30000
                return {"ok": process.returncode == 0, "command": command, "cwd": str(cwd),
                        "exit_code": process.returncode, "output": output,
                        "truncated": truncated}
            target = self.path(arguments.get("path", "."))
            if name == "list_directory":
                entries = []
                for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                    if len(entries) >= 300:
                        break
                    entries.append({"name": child.name, "type": "directory" if child.is_dir() else "file",
                                    "size": child.stat().st_size if child.is_file() else None})
                result = {"path": str(target), "entries": entries, "limit": 300}
            elif name == "read_file":
                lines = self.text(target).splitlines()
                start = max(0, int(arguments.get("offset", 1))-1)
                limit = max(1, min(1000, int(arguments.get("limit", 200))))
                result = {"path": str(target), "total_lines": len(lines),
                          "content": "\n".join(f"{i+1}|{line}" for i, line in enumerate(lines[start:start+limit], start))[:60000]}
            elif name == "search_files":
                query = str(arguments["query"]).lower()
                if not query:
                    raise ValueError("搜索关键词为空")
                hits, scanned = [], 0
                for base, folders, files in os.walk(target, followlinks=False):
                    folders[:] = [f for f in folders if f not in (".git", "node_modules", ".venv", "__pycache__") and not (Path(base)/f).is_symlink()]
                    for filename in files:
                        if cancel and cancel.is_set():
                            raise ValueError("搜索已停止")
                        scanned += 1
                        if scanned > 2000 or len(hits) >= 30:
                            break
                        item = self.path(str(Path(base)/filename))
                        match = query in filename.lower()
                        snippet = ""
                        if not match and item.stat().st_size < 250_000:
                            try:
                                for number, line in enumerate(self.text(item).splitlines(), 1):
                                    if query in line.lower():
                                        match, snippet = True, f"{number}|{line[:220]}"
                                        break
                            except (ValueError, OSError):
                                pass
                        if match:
                            display_path = str(item) if self.full_access else str(item.relative_to(self.root))
                            hits.append({"path": display_path, "match": snippet})
                    if scanned > 2000 or len(hits) >= 30:
                        break
                result = {"matches": hits, "scanned": scanned, "limit": 30}
            elif name == "write_file":
                content = arguments["content"]
                if not isinstance(content, str) or len(content.encode("utf-8")) > 1_000_000:
                    raise ValueError("单次写入限 1 MB 文本")
                if target == self.root:
                    raise ValueError("请选择文件路径")
                if target.exists() and not target.is_file():
                    raise ValueError("写入目标已存在且不是文件")
                saved = self.backup(target) if target.exists() else None
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                try:
                    temporary.write_text(content, encoding="utf-8")
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
                result = {"path": str(target), "bytes": target.stat().st_size, "backup": saved}
            elif name == "create_directory":
                target.mkdir(parents=True, exist_ok=True)
                result = {"path": str(target), "created": True}
            elif name == "move_path":
                source = self.path(arguments["source"])
                destination = self.path(arguments["destination"])
                if source == self.root or destination == self.root or destination.is_relative_to(source):
                    raise ValueError("移动路径有误")
                if destination.exists():
                    raise ValueError("目标已存在，请选择新的名称")
                destination.parent.mkdir(parents=True, exist_ok=True)
                source.rename(destination)
                result = {"source": str(source), "destination": str(destination)}
            else:
                if not target.is_file():
                    raise ValueError("移入备份区仅支持单个文件")
                saved = self.backup(target)
                target.unlink()
                result = {"path": str(target), "backup": saved}
            return {"ok": True, **result}
        except (OSError, ValueError, TypeError, KeyError) as error:
            return {"ok": False, "error": str(error)}
