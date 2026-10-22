"""Per-process proxy profiles and the pinned, local CLIProxyAPI integration."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

from assistant_data import read_protected, write_protected
from codex_quota import USAGE_URL, parse_codex_quota


ENGINE_VERSION = "8.0.35"
ENGINE_SHA256 = {
    "amd64": "175a79c47705408c4ee95ff78cafb50a0542a010d3fac333162f10f0b9dbea88",
    "aarch64": "75dce0ae34fe1325b7461bdd6843cac7e2e72c639dac226119f14207ca2ca625",
}
ENGINE_REPO = "https://github.com/router-for-me/CLIProxyAPI"
ASSISTANTS = {
    "codex": ("Codex", "https://api.openai.com/v1/models"),
    "gemini": ("Gemini CLI", "https://generativelanguage.googleapis.com/"),
    "antigravity": ("Antigravity 反重力", "https://antigravity.google/"),
    "claude": ("Claude Code", "https://api.anthropic.com/"),
}
PROXY_KEYS = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
SCHEDULING = {
    "round-robin": ("轮询分配（按会话）", "新会话轮流分配；可开启同一会话保持账号。"),
    "balanced": ("雨露均沾（逐请求均分）", "所有可用成员逐请求轮询，不保持会话；均分请求数量，单次请求消耗的额度可能不同。"),
    "fill-first": ("优先账号（按优先级用满）", "先使用优先级较高的成员，额度冷却或不可用时切换；同级优先首个可用账号。"),
    "weighted-round-robin": ("加权轮询（按权重分配）", "按成员权重分配，例如 2:1 约分配两倍请求；开启会话保持时按新会话分配。"),
}


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def default_settings() -> dict:
    return {
        "profiles": {
            name: {"mode": "custom", "proxy": "http://127.0.0.1:7890",
                   "no_proxy": "localhost,127.0.0.1,::1", "program": "",
                   "args": [], "cwd": str(Path.home()), "desktop": False}
            for name in ASSISTANTS
        },
        "pool": {"port": 8317, "proxy": "http://127.0.0.1:7890",
                 "strategy": "round-robin", "session_affinity": True,
                 "member_rules": {},
                 "background": True, "resume_on_launch": True,
                 "api_key": "omini-" + secrets.token_urlsafe(32),
                 "management_key": secrets.token_urlsafe(40), "model": ""},
    }


class AssistantStore:
    """Keep proxy passwords and local keys out of general settings and sources."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "settings.json"
        self.data = default_settings()
        self.error = ""
        self.writable = True
        try:
            saved, self.error = read_protected(self.path, {},
                lambda value: isinstance(value.get("profiles", {}), dict)
                and isinstance(value.get("pool", {}), dict)
                and all(isinstance(item, dict) for item in value.get("profiles", {}).values()))
            for name in ASSISTANTS:
                self.data["profiles"][name].update(saved.get("profiles", {}).get(name, {}))
            self.data["pool"].update(saved.get("pool", {}))
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as error:
            self.error = str(error)
            self.writable = False

    def save(self, data: dict) -> None:
        if not self.writable:
            raise ValueError(self.error)
        write_protected(self.path, data, recovered=bool(self.error))
        self.data = copy.deepcopy(data)
        self.error = ""


def validate_proxy(value: str, *, allow_empty=False, assistant="") -> str:
    value = value.strip()
    if not value and allow_empty:
        return ""
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ("http", "https", "socks5", "socks5h")
                 and parsed.hostname and parsed.port and 1 <= parsed.port <= 65535
                 and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
                 and not any(c.isspace() or ord(c) < 32 for c in value))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("请填写完整代理地址，例如 http://127.0.0.1:7890（需包含端口）。")
    if assistant == "claude" and parsed.scheme.startswith("socks"):
        raise ValueError("Claude Code 使用 HTTP/HTTPS 代理，请填写代理软件的 HTTP 或混合端口。")
    return value


def proxy_environment(profile: dict, base=None) -> dict[str, str]:
    """An inherited lowercase proxy must never override the chosen profile."""
    environment = dict(os.environ if base is None else base)
    mode = profile.get("mode", "inherit")
    if mode not in ("inherit", "direct", "custom"):
        raise ValueError("请选择有效的代理模式。")
    if mode == "inherit":
        return environment
    environment = {k: v for k, v in environment.items() if k.lower() not in PROXY_KEYS}
    bypass = [v.strip() for v in re.split(r"[,\s]+", profile.get("no_proxy", "")) if v.strip()]
    for host in ("localhost", "127.0.0.1", "::1"):
        if host not in bypass:
            bypass.append(host)
    for key in ("NO_PROXY", "no_proxy"):
        environment[key] = ",".join(bypass)
    if mode == "custom":
        proxy = validate_proxy(profile.get("proxy", ""))
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            environment[key] = environment[key.lower()] = proxy
        # Node 24+ can also apply these variables to native fetch requests.
        environment["NODE_USE_ENV_PROXY"] = "1"
    return environment


def redact(value: str, secret_values=()) -> str:
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    for secret in sorted((str(v) for v in secret_values if v), key=len, reverse=True):
        value = value.replace(secret, "[已隐藏]")
    value = re.sub(r"(?i)(https?://|socks5h?://)[^/\s@]+@", r"\1[已隐藏]@", value)
    value = re.sub(r"(?i)(Bearer\s+)[\w.\-]+", r"\1[已隐藏]", value)
    value = re.sub(r"(?i)((?:access_token|refresh_token|id_token|code|state|api_key|secret-key)[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,}]+", r"\1[已隐藏]", value)
    return value


def detect_program(name: str, desktop=False) -> str:
    if name not in ASSISTANTS:
        raise ValueError("助手类型有误。")
    local = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    appdata = Path(os.environ.get("APPDATA", Path.home()))
    candidates = []
    if name == "antigravity":
        candidates += [local / "Programs/Antigravity/Antigravity.exe",
                       Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Antigravity/Antigravity.exe"]
    elif name == "codex" and desktop:
        candidates += [local / "Programs/Codex/Codex.exe"]
        if sys.platform == "win32":
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-AppxPackage -Name '*Codex*' | Select-Object -ExpandProperty InstallLocation"],
                capture_output=True, timeout=12, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            for directory in result.stdout.decode("utf-8", "replace").splitlines():
                for suffix in ("app/Codex.exe", "Codex.exe"):
                    candidates.append(Path(directory.strip()) / suffix)
    else:
        command = {"codex": "codex", "gemini": "gemini", "claude": "claude"}[name]
        for suffix in (".exe", ".cmd", ""):
            found = shutil.which(command + suffix)
            if found:
                candidates.append(Path(found))
        candidates += [appdata / f"npm/{command}.cmd", Path.home() / f".local/bin/{command}.exe"]
    return next((str(p.resolve()) for p in candidates if p.is_file()), "")


def validate_profile(name: str, profile: dict) -> dict:
    result = copy.deepcopy(profile)
    if result.get("mode") == "custom":
        result["proxy"] = validate_proxy(result.get("proxy", ""), assistant=name)
    if not isinstance(result.get("args", []), list) or not all(isinstance(v, str) and "\x00" not in v for v in result.get("args", [])):
        raise ValueError("附加参数请每行填写一个。")
    proxy_environment(result, {})
    return result


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def powershell_environment(profile: dict) -> str:
    if profile["mode"] == "inherit":
        return "# 使用此 PowerShell 窗口已有的代理环境\n"
    lines = ["Remove-Item Env:HTTP_PROXY,Env:HTTPS_PROXY,Env:ALL_PROXY,Env:NO_PROXY -ErrorAction SilentlyContinue"]
    for key, value in proxy_environment(profile, {}).items():
        # Windows environment variable names are case insensitive.
        if key == key.upper():
            lines.append(f"$env:{key} = {ps_quote(value)}")
    return "\n".join(lines) + "\n"


def pool_codex_home(root: Path, pool: dict) -> Path:
    """A dedicated Codex profile works for CLI and desktop without global edits."""
    directory = Path(root) / "codex-pool"
    directory.mkdir(parents=True, exist_ok=True)
    values = [f'model = {json.dumps(pool["model"], ensure_ascii=False)}',
              'model_provider = "omini_pool"', '', '[model_providers.omini_pool]',
              'name = "Omini 本地账号池"',
              f'base_url = "http://127.0.0.1:{int(pool["port"])}/v1"',
              'wire_api = "responses"', 'env_key = "OMINI_CODEX_POOL_KEY"',
              'requires_openai_auth = false', '']
    target = directory / "config.toml"
    temporary = target.with_suffix(".tmp")
    temporary.write_text("\n".join(values), encoding="utf-8")
    temporary.replace(target)
    return directory


def resolve_cli_program(name: str, program: Path, args: list[str]) -> tuple[str, list[str]]:
    """Run npm's JS entry directly so cmd.exe never expands user arguments."""
    if program.suffix.lower() not in (".cmd", ".bat"):
        return str(program), args
    entries = {"codex": "@openai/codex/bin/codex.js", "gemini": "@google/gemini-cli/dist/index.js",
               "claude": "@anthropic-ai/claude-code/cli.js"}
    entry = program.parent / "node_modules" / entries.get(name, "")
    node = program.parent / "node.exe"
    node_path = str(node) if node.is_file() else shutil.which("node.exe") or shutil.which("node")
    if entry.is_file() and node_path:
        return node_path, [str(entry), *args]
    # A user-chosen batch launcher can accept plain flags; arbitrary shell text
    # requires the native .exe or npm entry route above.
    if any(re.search(r'[&|<>%^!"\r\n]', value) for value in [str(program), *args]):
        raise ValueError("该批处理启动器的参数含命令符号，请选择原生 .exe 或标准 npm 启动器。")
    return str(program), args


def launch_assistant(name: str, profile: dict, *, root: Path, pool=None, terminal=True) -> int:
    profile = validate_profile(name, profile)
    program = Path(profile.get("program", ""))
    if not program.is_file():
        raise ValueError("请先检测或选择助手的程序文件。")
    cwd = Path(profile.get("cwd", "") or Path.home()).expanduser()
    if not cwd.is_dir():
        raise ValueError("工作目录不存在，请重新选择。")
    environment = proxy_environment(profile)
    args = list(profile.get("args", []))
    gui = name == "antigravity" or (name == "codex" and profile.get("desktop"))
    if gui and profile["mode"] == "custom":
        proxy = urlsplit(profile["proxy"])
        if proxy.username or proxy.password:
            raise ValueError("桌面客户端的代理启动参数使用无密码地址；请使用本机 HTTP 代理端口。")
        args += ["--proxy-server=" + profile["proxy"], "--proxy-bypass-list=localhost;127.0.0.1;[::1]"]
    elif gui and profile["mode"] == "direct":
        args.append("--no-proxy-server")
    if pool is not None:
        if name != "codex":
            raise ValueError("请选择 Codex 客户端使用账号池。")
        if not str(pool.get("model", "")).strip():
            raise ValueError("请先读取模型列表并选择模型。")
        environment["CODEX_HOME"] = str(pool_codex_home(root, pool))
        environment["OMINI_CODEX_POOL_KEY"] = pool["api_key"]
        # The local gateway must stay reachable even if the parent has a wildcard proxy.
        for key in ("NO_PROXY", "no_proxy"):
            environment[key] = "localhost,127.0.0.1,::1"
    actual_program, args = resolve_cli_program(name, program, args) if not gui else (str(program), args)
    if sys.platform == "win32" and not gui and terminal:
        # JSON avoids cmd.exe expansion of paths, &, %, quotes, TOML and passwords.
        # Only the payload path is passed to powershell; credentials stay in env.
        directory = Path(root) / "launchers"
        directory.mkdir(parents=True, exist_ok=True)
        payload = directory / (uuid.uuid4().hex + ".json")
        batch = Path(actual_program).suffix.lower() in (".cmd", ".bat")
        atomic_json(payload, {"program": actual_program, "args": args, "cwd": str(cwd),
                              "arguments": subprocess.list2cmdline(args)})
        script = (f"$p = {ps_quote(str(payload))}\n"
                  "$d = Get-Content -LiteralPath $p -Raw -Encoding UTF8 | ConvertFrom-Json\n"
                  "Remove-Item -LiteralPath $p -Force\n"
                  "Set-Location -LiteralPath $d.cwd\n")
        if batch:
            script += "$a = @($d.args)\n& $d.program @a\n"
        else:
            script += ("$i = New-Object System.Diagnostics.ProcessStartInfo\n"
                       "$i.FileName = $d.program\n$i.Arguments = $d.arguments\n"
                       "$i.WorkingDirectory = $d.cwd\n$i.UseShellExecute = $false\n"
                       "$c = [System.Diagnostics.Process]::Start($i)\n$c.WaitForExit()\n")
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        command = ["powershell.exe", "-NoLogo", "-NoProfile", "-NoExit", "-EncodedCommand", encoded]
        flags = subprocess.CREATE_NEW_CONSOLE
    else:
        command = [actual_program, *args]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        process = subprocess.Popen(command, cwd=str(cwd), env=environment, creationflags=flags,
                                   stdin=subprocess.DEVNULL if gui else None,
                                   stdout=subprocess.DEVNULL if gui else None,
                                   stderr=subprocess.DEVNULL if gui else None)
    except OSError:
        if sys.platform == "win32" and not gui and terminal:
            payload.unlink(missing_ok=True)
        raise
    return process.pid


def test_connection(name: str, profile: dict, timeout=10) -> dict:
    profile = validate_profile(name, profile)
    mode = profile["mode"]
    with requests.Session() as session:
        session.trust_env = mode == "inherit"
        if mode == "custom":
            proxy = profile["proxy"]
            session.proxies.update({"http": proxy, "https": proxy})
        started = time.monotonic()
        try:
            response = session.get(ASSISTANTS[name][1], timeout=(min(timeout, 5), timeout),
                                   allow_redirects=False, stream=True)
            with response:
                status = response.status_code
            elapsed = round((time.monotonic() - started) * 1000)
            # A 401 confirms transport/TLS only; it does not validate the login.
            transport = 200 <= status < 400 or status in (401, 404, 405)
            return {"ok": transport, "status": status, "elapsed_ms": elapsed,
                    "message": f"{'网络可达' if transport else '已收到响应，请检查代理或服务限制'} · HTTP {status} · {elapsed} ms；登录和模型调用另行验证。"}
        except requests.RequestException as error:
            raise ValueError("连接测试失败：" + redact(str(error), [profile.get("proxy", "")])) from None


def engine_path(root: Path) -> Path | None:
    roots = [Path(root) / "bin"]
    for ancestor in Path(root).parents:
        if ancestor.name == "account-workspaces": roots.append(ancestor.parent / "ai-assistants/bin")
    roots.append(Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "tools")
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).parent / "tools")
    for directory in roots:
        for name in ("cli-proxy-api.exe", "CLIProxyAPI.exe", "cliproxyapi.exe"):
            path = directory / name
            if path.is_file():
                return path
    return None


def install_engine(destination: Path, proxy="", progress=None, cancelled=None) -> Path:
    """Only install the exact official archive whose digest is pinned here."""
    arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "amd64"
    archive_name = f"CLIProxyAPI_{ENGINE_VERSION}_windows_{arch}.zip"
    url = f"{ENGINE_REPO}/releases/download/v{ENGINE_VERSION}/{archive_name}"
    if proxy:
        proxy = validate_proxy(proxy)
    with requests.Session() as session:
        session.trust_env = False
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
        try:
            with session.get(url, timeout=(10, 12), stream=True) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_content(256 * 1024):
                    if cancelled and cancelled():
                        raise ValueError("下载已取消。")
                    body.extend(chunk)
                    if len(body) > 100 * 1024 * 1024:
                        raise ValueError("下载内容超过引擎大小限制。")
                    if progress:
                        progress(f"下载引擎：{len(body) // 1024 // 1024} MB")
        except requests.RequestException as error:
            raise ValueError("引擎下载失败：" + redact(str(error), [proxy])) from None
    if hashlib.sha256(body).hexdigest() != ENGINE_SHA256[arch]:
        raise ValueError("引擎 SHA-256 校验失败，请重新下载。")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        executable = next((n for n in archive.namelist() if Path(n).name.lower() in
                           ("cli-proxy-api.exe", "cliproxyapi.exe")), None)
        if executable is None:
            raise ValueError("发布包未包含 Windows 代理引擎。")
        output = destination / "cli-proxy-api.exe"
        temporary = output.with_suffix(".download")
        temporary.write_bytes(archive.read(executable))
        temporary.replace(output)
        for member in archive.namelist():
            if Path(member).name.upper() in ("LICENSE", "LICENSE.TXT", "LICENSE.MD"):
                (destination / "CLIProxyAPI-LICENSE.txt").write_bytes(archive.read(member))
    (destination / "CLIProxyAPI-release.txt").write_text(
        f"CLIProxyAPI v{ENGINE_VERSION}\n{url}\nSHA256: {ENGINE_SHA256[arch]}\nSource: {ENGINE_REPO}\nLicense: MIT\n",
        encoding="utf-8")
    return output


def pool_config(root: Path, pool: dict) -> dict:
    port = int(pool["port"])
    if not 1024 <= port <= 65535:
        raise ValueError("本地端口需为 1024～65535。")
    routing = routing_config(pool)
    if not pool.get("api_key") or not pool.get("management_key"):
        raise ValueError("本地代理密钥缺失，请重新保存账号池设置。")
    return {"config-version": 8, "server": {"host": "127.0.0.1", "port": port},
            "access": {"api-keys": [pool["api_key"]]},
            "management": {"allow-remote": False, "secret-key": pool["management_key"],
                           "disable-control-panel": True},
            "oauth": {"auth-dir": str((Path(root) / "auths").resolve())},
            "requests": {"proxy-url": validate_proxy(pool.get("proxy", ""), allow_empty=True)},
            "routing": {**routing, "session-affinity-ttl": "1h", "retry": {"request-retry": 2, "max-retry-interval": 10}},
            "observability": {"logs": {"logging-to-file": False, "debug": False,
                                       "request-log": False, "error-logs-max-files": 1}}}


def routing_config(pool: dict) -> dict:
    strategy = pool.get("strategy", "round-robin")
    if strategy not in SCHEDULING:
        raise ValueError("请选择有效的账号调度方式。")
    return {"strategy": "round-robin" if strategy == "balanced" else strategy,
            "session-affinity": False if strategy == "balanced" else bool(pool.get("session_affinity", True))}


def member_rule(pool: dict, row: dict) -> dict:
    rule = pool.get("member_rules", {}).get(row["name"], {})
    priority, weight = rule.get("priority", row.get("priority", 0)), rule.get("weight", row.get("weight", 1))
    if (isinstance(priority, bool) or not isinstance(priority, int) or not -1000000 <= priority <= 1000000
            or isinstance(weight, bool) or not isinstance(weight, int) or not 1 <= weight <= 1000000):
        raise ValueError("成员优先级需为整数，权重需为 1～1000000 的整数。")
    return {"priority": priority, "weight": weight}


def remember_member_rules(pool: dict, rows: list[dict]) -> dict:
    value = copy.deepcopy(pool)
    rules = value.setdefault("member_rules", {})
    for row in rows:
        if row["name"] not in rules:
            rules[row["name"]] = member_rule(value, row)
    return value


def available_port(port: int) -> None:
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", int(port)))
        except OSError:
            raise ValueError(f"本地端口 {port} 已被占用，请选择其他端口。") from None


def normalize_codex_credential(path: Path) -> tuple[str, bytes]:
    path = Path(path)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("凭据文件过大，请选择 Codex 或 CLIProxyAPI 的 JSON 凭据。")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError()
        tokens = data.get("tokens", data)
        if (not isinstance(tokens, dict)
                or not all(isinstance(tokens.get(key), str) and tokens[key].strip()
                           for key in ("access_token", "refresh_token"))):
            raise ValueError()
        if data.get("type", "codex") != "codex":
            raise ValueError()
        normalized = {key: tokens[key] for key in ("access_token", "refresh_token", "id_token", "account_id") if key in tokens}
        normalized.update({"type": "codex", "disabled": bool(data.get("disabled", False))})
        for key in ("email", "expired", "last_refresh", "proxy_url"):
            if key in data:
                normalized[key] = data[key]
        jwt = str(tokens.get("id_token") or tokens["access_token"])
        try:
            part = jwt.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
            if not normalized.get("email") and claims.get("email"):
                normalized["email"] = claims["email"]
            if not normalized.get("expired") and isinstance(claims.get("exp"), (int, float)):
                normalized["expired"] = datetime.fromtimestamp(claims["exp"], timezone.utc).isoformat()
        except (ValueError, IndexError, TypeError, OverflowError, OSError):
            pass
        identity = normalized.get("account_id") or normalized.get("email") or normalized["refresh_token"]
        filename = "codex-" + hashlib.sha256(str(identity).encode()).hexdigest()[:20] + ".json"
        return filename, json.dumps(normalized, ensure_ascii=False).encode("utf-8")
    except (ValueError, TypeError, KeyError):
        raise ValueError("凭据格式有误，请选择含 access_token 与 refresh_token 的 Codex OAuth JSON。") from None


class PoolClient:
    """Management and API calls never inherit proxy variables or follow redirects."""

    def __init__(self, pool: dict):
        self.pool = copy.deepcopy(pool)
        self.base = f"http://127.0.0.1:{int(pool['port'])}"

    def request(self, method: str, path: str, *, management=True, **kwargs):
        key = self.pool["management_key" if management else "api_key"]
        prefix = "/v8/management" if management else "/v1"
        timeout = kwargs.pop("timeout", (2, 12))
        with requests.Session() as session:
            session.trust_env = False
            try:
                with session.request(method, self.base + prefix + path,
                                     headers={"Authorization": "Bearer " + key},
                                     timeout=timeout, allow_redirects=False, **kwargs) as response:
                    if not 200 <= response.status_code < 300:
                        raise ValueError(f"本地代理返回 HTTP {response.status_code}，请检查运行状态和账号。")
                    return response.json()
            except requests.exceptions.JSONDecodeError:
                raise ValueError("本地代理响应格式有误。") from None
            except requests.RequestException:
                raise ValueError("本地代理连接失败，请先启动账号池服务。") from None

    def accounts(self) -> list[dict]:
        return [row for row in self.request("GET", "/credentials").get("files", [])
                if row.get("provider", row.get("type")) == "codex"]

    def quota(self, row: dict) -> dict:
        index = row.get("auth_index")
        if not index:
            raise ValueError("账号缺少额度查询标识，请先刷新账号。")
        headers = {"Authorization": "Bearer $TOKEN$", "Accept": "application/json",
                   "User-Agent": "codex_cli_rs/0.134.0"}
        claims = row.get("id_token")
        account = claims.get("chatgpt_account_id") if isinstance(claims, dict) else None
        account = account or row.get("account_id")
        if account: headers["Chatgpt-Account-Id"] = str(account)
        response = self.request("POST", "/requests/api-call", timeout=(2, 30), json={
            "auth_index": str(index), "method": "GET", "url": USAGE_URL, "header": headers})
        status = response.get("status_code")
        if status != 200:
            message = {401: "登录已过期，请重新登录此成员。", 403: "服务拒绝查询，请检查出口代理或账号权限。",
                       429: "额度查询过于频繁，请稍后重试。"}.get(status, "额度服务暂时不可用，请稍后重试。")
            raise ValueError(f"HTTP {status}：{message}")
        try:
            body = response.get("body")
            return parse_codex_quota(json.loads(body) if isinstance(body, str) else body)
        except (json.JSONDecodeError, TypeError):
            raise ValueError("额度服务未返回有效 JSON，请检查网络后重试。") from None

    def apply_scheduling(self, pool: dict, rows=None, cancelled=lambda: False) -> list[dict]:
        routing = routing_config(pool)
        rows = self.accounts() if rows is None else rows
        changes = []
        for row in rows:
            rule = member_rule(pool, row)
            effective = {"priority": rule["priority"] if pool["strategy"] == "fill-first" else 0,
                         "weight": rule["weight"]}
            if any(row.get(key, 0 if key == "priority" else 1) != value for key, value in effective.items()):
                changes.append((row, effective))
        previous = self.request("GET", "/config/routing")
        applied = []
        try:
            for row, fields in changes:
                if cancelled(): raise ValueError("调度更新已取消。")
                # Record before sending: a timeout may occur after the server commits.
                applied.append(row)
                self.request("PATCH", "/credentials/fields", json={"name": row["name"], **fields})
            if cancelled(): raise ValueError("调度更新已取消。")
            self.request("PATCH", "/config/routing", json=routing)
        except ValueError:
            for row in reversed(applied):
                try:
                    self.request("PATCH", "/credentials/fields", json={"name": row["name"],
                        "priority": row.get("priority"), "weight": row.get("weight")})
                except ValueError:
                    pass
            try: self.request("PUT", "/config/routing", json=previous)
            except ValueError: pass
            raise
        return self.accounts()

    def models(self) -> list[str]:
        # Use credential-specific models to exclude other imported providers.
        rows = self.accounts()
        models = set()
        for row in rows:
            if row.get("disabled"):
                continue
            data = self.request("GET", "/credentials/models", params={"name": row["name"]})
            models.update(m.get("id") or m.get("name") for m in data.get("models", []) if m.get("id") or m.get("name"))
        return sorted(models)

    def import_account(self, path: Path) -> None:
        name, data = normalize_codex_credential(path)
        self.request("POST", "/credentials", params={"name": name}, data=data)

    def set_disabled(self, name: str, disabled: bool) -> None:
        self.request("PATCH", "/credentials/status", json={"name": name, "disabled": disabled})

    def remove(self, name: str) -> None:
        self.request("DELETE", "/credentials", params={"name": name})

    def begin_login(self) -> dict:
        return self.request("GET", "/oauth/auth-url", params={"provider": "codex", "is_webui": "true"})

    def login_status(self, state: str) -> dict:
        return self.request("GET", "/oauth/status", params={"state": state})

    def cancel_login(self, state: str) -> dict:
        return self.request("DELETE", "/oauth/session", params={"state": state})
