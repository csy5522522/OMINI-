"""Read-only Tailscale inventory and Syncthing endpoint normalization."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit


class TailscaleError(ValueError):
    pass


def normalize_addresses(values):
    """Accept bare IPv4/IPv6/MagicDNS names, retaining explicit engine addresses."""
    if isinstance(values, str):
        values = re.split(r"[,，;；\n]+", values)
    result = []
    for value in values or ["dynamic"]:
        value = value.strip()
        if not value:
            continue
        if value == "dynamic":
            normalized = value
        elif "://" in value:
            parsed = urlsplit(value)
            if parsed.scheme not in ("tcp", "tcp4", "tcp6", "quic", "quic4", "quic6", "relay"):
                raise TailscaleError("连接地址请使用 IP、MagicDNS 名称，或 tcp://、quic://、relay:// 地址。")
            if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise TailscaleError("连接地址格式有误，请填写对方的 IP 或主机名。")
            try:
                port = parsed.port
            except ValueError as error:
                raise TailscaleError("连接端口应为 1～65535。") from error
            if port == 0:
                raise TailscaleError("连接端口应为 1～65535。")
            if parsed.scheme.startswith(("tcp", "quic")):
                if parsed.path not in ("", "/"):
                    raise TailscaleError("同步地址只需要主机和端口，请删除网页路径。")
                host = parsed.hostname.rstrip(".")
                normalized = _tcp_address(host, port or 22000, parsed.scheme)
                if parsed.query:
                    normalized += "?" + parsed.query
            else:
                normalized = value
        else:
            try:
                address = ipaddress.ip_address(value.strip("[]"))
            except ValueError:
                address = None
            if address:
                normalized = _tcp_address(str(address), 22000)
            else:
                parsed = urlsplit("tcp://" + value)
                try:
                    port = parsed.port
                except ValueError as error:
                    raise TailscaleError("IPv6 地址带端口时请使用 [IPv6]:端口 格式。") from error
                host = (parsed.hostname or "").rstrip(".")
                try:
                    valid_host = bool(ipaddress.ip_address(host))
                except ValueError:
                    valid_host = bool(re.fullmatch(r"[a-zA-Z0-9_.-]+", host))
                if (not valid_host or parsed.username or parsed.password
                        or parsed.path not in ("", "/") or parsed.query or parsed.fragment or port == 0):
                    raise TailscaleError("请填写有效的 IP、MagicDNS 名称，或 主机:端口。")
                normalized = _tcp_address(host, port or 22000)
        if normalized not in result:
            result.append(normalized)
    return result or ["dynamic"]


def _tcp_address(host, port, scheme="tcp"):
    if ":" in host:
        host = "[" + host + "]"
    return f"{scheme}://{host}:{port}"


def _node(node):
    addresses = []
    for value in node.get("TailscaleIPs") or []:
        try:
            addresses.append(str(ipaddress.ip_address(value)))
        except ValueError:
            pass
    return {"name": node.get("HostName") or node.get("DNSName", "").split(".")[0],
            "dns": node.get("DNSName", "").rstrip("."), "ips": addresses,
            "online": bool(node.get("Online")), "os": node.get("OS", "")}


def parse_status(data):
    return {"state": data.get("BackendState", "Unknown"), "self": _node(data.get("Self") or {}),
            "peers": sorted((_node(node) for node in (data.get("Peer") or {}).values()),
                            key=lambda node: (not node["online"], node["name"].casefold()))}


def read_status():
    candidates = [shutil.which("tailscale")]
    if os.name == "nt":
        candidates.append(str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Tailscale/tailscale.exe"))
    else:
        candidates.extend(["/Applications/Tailscale.app/Contents/MacOS/Tailscale", "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale"])
    binary = next((path for path in candidates if path and Path(path).is_file()), None)
    if not binary:
        raise TailscaleError("未找到 Tailscale 命令行。可直接在连接地址填写对方 Tailscale IP 或 MagicDNS 名称。")
    try:
        result = subprocess.run([binary, "status", "--json"], capture_output=True, timeout=8,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            raise TailscaleError("Tailscale 状态读取失败，请确认已启动并登录。")
        return parse_status(json.loads(result.stdout.decode("utf-8")))
    except subprocess.TimeoutExpired as error:
        raise TailscaleError("Tailscale 状态读取超时；可手动填写对方地址。") from error
    except (OSError, ValueError) as error:
        if isinstance(error, TailscaleError):
            raise
        raise TailscaleError("Tailscale 状态读取失败；可手动填写对方地址。") from error
