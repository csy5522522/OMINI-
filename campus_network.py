"""Ruijie ePortal client and Windows credential/Wi-Fi helpers.

The HAR is a protocol reference only. Never reuse its encrypted password,
MAC, IP, userIndex or redirect query for a new connection.
"""
from __future__ import annotations

import base64
import ctypes
import html
import json
import re
import subprocess
import sys
from urllib.error import URLError
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


DEFAULT_PORTAL = "http://10.24.127.3/eportal/success.jsp"
PROBE_URL = "http://www.msftconnecttest.com/connecttest.txt"


def decode_portal_content(data: bytes, charset: str = "utf-8") -> str:
    try:
        return data.decode(charset)
    except (UnicodeDecodeError, LookupError):
        # This portal also serves GBK messages without a charset declaration.
        return data.decode("gb18030", errors="replace")


def wifi_ssids() -> tuple[str, ...]:
    if sys.platform != "win32":
        return ()
    result = subprocess.run(
        ["netsh", "wlan", "show", "interfaces"], capture_output=True,
        timeout=6, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError("Wi-Fi 检测失败，请检查 Windows WLAN 服务与位置权限。")
    output = result.stdout.decode("utf-8", errors="replace")
    if "\ufffd" in output:
        output = result.stdout.decode("mbcs", errors="replace")
    return tuple(re.findall(r"^\s*SSID\s*:\s*(.+?)\s*$", output, re.M))


def protect_password(password: str) -> str:
    return _dpapi(password.encode("utf-8"), False)


def unprotect_password(blob: str) -> str:
    if not blob:
        return ""
    return _dpapi(base64.b64decode(blob, validate=True), True).decode("utf-8")


def _dpapi(data: bytes, decrypt: bool):
    if sys.platform != "win32":
        raise RuntimeError("保存密码需要 Windows 本机加密服务。")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    fn.restype = ctypes.c_int
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise RuntimeError("Windows 密码加密服务失败，请重新输入密码。")
    try:
        value = ctypes.string_at(output.data, output.size)
        return value if decrypt else base64.b64encode(value).decode("ascii")
    finally:
        kernel.LocalFree(output.data)


def encrypt_portal_password(password: str, mac: str, exponent: str, modulus: str) -> str:
    """Match RSAUtils.encryptedString(reverse(password + '>' + raw query MAC))."""
    n, e = int(modulus, 16), int(exponent, 16)
    chunk_size = 2 * ((n.bit_length() - 1) // 16)
    if chunk_size < 2 or e < 1:
        raise ValueError("认证页面公钥格式错误。")
    raw = (password + ">" + (mac or "111111111")).encode("utf-16-le")
    units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)][::-1]
    units += [0] * ((-len(units)) % chunk_size)
    blocks = []
    for offset in range(0, len(units), chunk_size):
        block = units[offset:offset + chunk_size]
        value = sum((block[i] + (block[i + 1] << 8)) << (8 * i)
                    for i in range(0, chunk_size, 2))
        encrypted = format(pow(value, e, n), "x")
        blocks.append(encrypted.zfill((len(encrypted) + 3) // 4 * 4))
    return " ".join(blocks)


class PortalRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Authentication POSTs must stay on their configured origin.
        if req.data is not None and urlsplit(req.full_url).netloc != urlsplit(newurl).netloc:
            raise ValueError("认证接口跳转地址发生变化，请检查门户地址。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PortalClient:
    def __init__(self, portal_url: str = DEFAULT_PORTAL):
        parsed = urlsplit(portal_url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
            raise ValueError("请输入完整的校园网门户 HTTP/HTTPS 地址。")
        self.portal_url = portal_url.strip()
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.login_url = self.origin + "/eportal/index.jsp" if parsed.path.endswith("/success.jsp") else self.portal_url
        fields = parse_qs(parsed.query)
        self.user_index = fields.get("userIndex", [""])[0] if parsed.path.endswith("/success.jsp") else ""
        self.keepalive_interval = self._interval(fields.get("keepaliveInterval", [0])[0])
        self.opener = build_opener(ProxyHandler({}), PortalRedirects())

    @staticmethod
    def _interval(value) -> int:
        try:
            return max(0, int(value))
        except (ValueError, TypeError):
            return 0

    @property
    def browser_url(self) -> str:
        if self.user_index:
            return self.origin + "/eportal/success.jsp?" + urlencode({
                "userIndex": self.user_index, "keepaliveInterval": self.keepalive_interval,
            })
        # The browser button honors the configured page until a new login
        # returns a current session. Authentication discovery uses login_url.
        return self.portal_url

    def fetch(self, url: str, data: bytes | None = None) -> tuple[str, str]:
        request = Request(url, data=data, headers={
            "User-Agent": "Mozilla/5.0 OminiCampus/1.0",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            **({"Referer": self.portal_url, "Origin": self.origin} if data is not None else {}),
        })
        try:
            with self.opener.open(request, timeout=8) as response:
                content = response.read(2 * 1024 * 1024)
                return response.geturl(), decode_portal_content(content, response.headers.get_content_charset() or "utf-8")
        except (URLError, OSError) as error:
            # Exception URLs may contain current-session tokens; keep logs generic.
            raise RuntimeError("校园网请求失败或超时，请检查连接与门户地址。") from error

    def call(self, method: str, fields: dict[str, str], double: bool = False) -> dict:
        def encode(value):
            encoded = quote(str(value), safe="~!*'()-._")
            return quote(encoded, safe="~!*'()-._") if double else encoded
        body = "&".join(f"{key}={encode(value)}" for key, value in fields.items()).encode("ascii")
        _, content = self.fetch(self.origin + "/eportal/InterFace.do?method=" + method, body)
        try:
            result = json.loads(content)
        except ValueError as error:
            raise RuntimeError("校园网返回了非 JSON 内容，请检查门户地址。") from error
        if not isinstance(result, dict):
            raise RuntimeError("校园网接口响应格式错误。")
        return result

    def discover(self) -> tuple[str, bool]:
        candidates = [PROBE_URL, self.login_url]
        for url in candidates:
            try:
                final, content = self.fetch(url)
            except RuntimeError:
                continue
            if url == PROBE_URL and content.strip() == "Microsoft Connect Test":
                return "", True
            for target in [final] + [html.unescape(match) for match in re.findall(
                    r"(?:location(?:\.href)?\s*=|location\.replace\(|url\s*=)\s*['\"]([^'\"]+)",
                    content, re.I)]:
                parsed = urlsplit(urljoin(final, target))
                if parsed.netloc == urlsplit(self.origin).netloc and "wlanuserip=" in parsed.query:
                    return parsed.query, False
        raise RuntimeError("尚未获取当前连接的认证参数，请连接校园网或填写浏览器中的完整登录页地址。")

    def login(self, username: str, password: str, service: str = "", cancelled=lambda: False) -> str:
        def check():
            if cancelled():
                raise RuntimeError("登录任务已停止。")
        check()
        if self.user_index:
            try:
                online = self.call("getOnlineUserInfo", {"userIndex": self.user_index})
            except RuntimeError:
                online = {}
            check()
            if online.get("result") == "success":
                return "校园网已在线，无需重复登录。"
            self.user_index = ""
        query, online = self.discover()
        check()
        if online:
            return "网络已连通，无需重复登录。"
        if not username.strip() or not password:
            raise ValueError("请先填写校园网账号和密码。")
        original_password = password
        info = self.call("pageInfo", {"queryString": query})
        check()
        if info.get("validCodeUrl"):
            raise RuntimeError("当前认证需要验证码，请点击「打开校园网页」完成验证。")
        encrypted = str(info.get("passwordEncrypt", "false")).lower() == "true"
        if encrypted:
            match = re.search(r"(?:^|&)mac=([^&]*)", query, re.I)
            password = encrypt_portal_password(password, match.group(1) if match else "",
                                               info["publicKeyExponent"], info["publicKeyModulus"])
        result = self.call("login", {
            "userId": username.strip(), "password": password, "service": service,
            "queryString": query, "operatorPwd": "", "operatorUserId": "", "validcode": "",
            "passwordEncrypt": "true" if encrypted else "false",
        }, double=True)
        check()
        if result.get("result") != "success":
            message = str(result.get("message") or "请检查账号、密码或服务名称。")
            # Avoid echoing credentials or session parameters supplied by the server.
            for secret in (original_password, password, query, quote(query, safe="")):
                if secret: message = message.replace(secret, "[已隐藏]")
            message = re.sub(r"(?i)(password|queryString|userIndex)\s*[=:]\s*\S+", "[已隐藏]", message)
            raise RuntimeError("登录失败：" + message[:180])
        self.user_index = str(result.get("userIndex") or "")
        self.keepalive_interval = self._interval(result.get("keepaliveInterval", 0))
        return "校园网登录成功。"
