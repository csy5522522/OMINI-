from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omini_api.omini_sites import SiteError
from omini_api.xhhai_file_utils import write_json_atomic


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 2,
    "api_base": "https://email.buygptpuls.com",
    "api_key": "",
    "domain": "",
    "prefix": "omini",
    "random_length": 8,
    "domain_level": 3,
    "poll_interval": 3.0,
    "timeout": 180.0,
}


@dataclass(frozen=True)
class CodeReceipt:
    code: str
    email: str
    subject: str
    received_at: str
    elapsed_seconds: float
    poll_count: int
    email_count: int


def mask_email(value: str) -> str:
    text = str(value or "").strip()
    if "@" not in text:
        return "***"
    local, domain = text.rsplit("@", 1)
    shown = local[:2] if local else ""
    return f"{shown}***@{domain}"


def load_receiver_config(path: Path, *, create: bool = False) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            raise SiteError(f"验证码邮箱配置读取失败: {exc}") from exc
        if not isinstance(loaded, dict):
            raise SiteError("验证码邮箱配置必须是 JSON 对象。")
        config.update(loaded)
        if int(loaded.get("version") or 1) < 2 or "domain_level" not in loaded:
            save_receiver_config(path, config)
    elif create:
        save_receiver_config(path, config)
    return config


def validate_receiver_config(config: dict[str, Any], *, require_api_key: bool = True) -> dict[str, Any]:
    normalized = dict(DEFAULT_CONFIG)
    normalized.update(config or {})
    api_base = str(normalized.get("api_base") or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(api_base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SiteError("验证码邮箱 API Base 必须是完整的 HTTP/HTTPS 地址。")
    api_key = str(normalized.get("api_key") or "").strip()
    if require_api_key and not api_key:
        raise SiteError("验证码邮箱服务 API Key 尚未配置。")
    try:
        random_length = int(normalized.get("random_length", 8))
        domain_level = int(normalized.get("domain_level", 3))
        poll_interval = float(normalized.get("poll_interval", 3))
        timeout = float(normalized.get("timeout", 180))
    except (TypeError, ValueError) as exc:
        raise SiteError("验证码邮箱的随机长度、域名级数、轮询间隔和超时必须是数字。") from exc
    if not 1 <= random_length <= 32:
        raise SiteError("验证码邮箱随机长度必须在 1 到 32 之间。")
    if poll_interval <= 0 or timeout <= 0:
        raise SiteError("验证码邮箱轮询间隔和超时必须大于 0。")
    if domain_level not in {2, 3}:
        raise SiteError("验证码邮箱 domain_level 只支持 2 或 3。")
    normalized.update(
        api_base=api_base,
        api_key=api_key,
        domain=str(normalized.get("domain") or "").strip().lstrip("@"),
        prefix=str(normalized.get("prefix") or "").strip(),
        random_length=random_length,
        domain_level=domain_level,
        poll_interval=poll_interval,
        timeout=timeout,
        version=2,
    )
    return normalized


def save_receiver_config(path: Path, config: dict[str, Any]) -> None:
    normalized = validate_receiver_config(config, require_api_key=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, normalized, trailing_newline=True)


def _open(use_proxy: bool, proxy_url: str):
    if use_proxy and proxy_url:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        ).open
    if use_proxy:
        return urllib.request.urlopen
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def _request_json(
    config: dict[str, Any],
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    use_proxy: bool = False,
    proxy_url: str = "",
    timeout: float = 30,
) -> Any:
    config = validate_receiver_config(config)
    url = config["api_base"] + "/" + path.lstrip("/")
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=payload,
        method=method.upper(),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Omini-API-Toolbox/2.0",
            "X-API-Key": config["api_key"],
        },
    )
    try:
        with _open(use_proxy, proxy_url)(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SiteError(f"验证码邮箱服务 HTTP {exc.code}: {detail[:240]}") from exc
    except urllib.error.URLError as exc:
        raise SiteError(f"验证码邮箱服务网络错误: {exc.reason}") from exc
    try:
        return json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        raise SiteError(f"验证码邮箱服务返回的内容不是 JSON: {text[:240]}") from exc


def available_domains(
    config: dict[str, Any], *, use_proxy: bool = False, proxy_url: str = ""
) -> list[str]:
    result = _request_json(
        config,
        "GET",
        "/api/v1/mailboxes/available-domains",
        use_proxy=use_proxy,
        proxy_url=proxy_url,
    )
    values = result.get("domains", result) if isinstance(result, dict) else result
    if not isinstance(values, list):
        raise SiteError("验证码邮箱服务没有返回可用域名列表。")
    domains: list[str] = []
    for item in values:
        value = item.get("domain") if isinstance(item, dict) else item
        if str(value or "").strip():
            domains.append(str(value).strip().lstrip("@"))
    return domains


def _mailbox_from_response(result: Any) -> dict[str, Any]:
    value = result
    if isinstance(value, dict) and isinstance(value.get("data"), dict):
        value = value["data"]
    if isinstance(value, dict) and isinstance(value.get("mailbox"), dict):
        value = value["mailbox"]
    if not isinstance(value, dict):
        raise SiteError("创建邮箱接口没有返回邮箱对象。")
    email = str(value.get("email") or "").strip()
    token = str(value.get("token") or "").strip()
    if not email or not token:
        raise SiteError("创建邮箱接口没有返回有效的 email 和 token。")
    return {**value, "email": email, "token": token}


def create_mailbox(
    config: dict[str, Any], *, use_proxy: bool = False, proxy_url: str = ""
) -> dict[str, Any]:
    config = validate_receiver_config(config)
    body: dict[str, Any] = {
        "prefix": config["prefix"],
        "random_length": config["random_length"],
        "domain_level": config["domain_level"],
    }
    if config["domain"]:
        body["domain"] = config["domain"]
    result = _request_json(
        config, "POST", "/api/v1/mailboxes", body=body,
        use_proxy=use_proxy, proxy_url=proxy_url,
    )
    return _mailbox_from_response(result)


def create_mailboxes(
    config: dict[str, Any],
    count: int,
    *,
    use_proxy: bool = False,
    proxy_url: str = "",
) -> list[dict[str, Any]]:
    config = validate_receiver_config(config)
    total = max(0, int(count))
    if total == 0:
        return []
    indexed: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(total, 10)) as executor:
        futures = {
            executor.submit(create_mailbox, config, use_proxy=use_proxy, proxy_url=proxy_url): index
            for index in range(total)
        }
        for future in as_completed(futures):
            indexed[futures[future]] = future.result()
    created = [indexed[index] for index in range(total)]
    emails = {item["email"].casefold() for item in created}
    tokens = {item["token"] for item in created}
    if len(emails) != len(created) or len(tokens) != len(created):
        raise SiteError("验证码邮箱服务返回了重复邮箱或 token。")
    return created


def extract_code(message: Any, pattern: str = r"\b\d{4,8}\b") -> str:
    if not isinstance(message, dict):
        return ""
    direct = str(message.get("code") or message.get("extracted_code") or "").strip()
    if direct:
        return direct
    haystack = "\n".join(
        str(message.get(name) or "")
        for name in ("subject", "body", "body_text", "text", "body_html", "html")
    )
    match = re.search(pattern, haystack)
    return match.group(0) if match else ""


def _mailbox_payload(result: Any) -> tuple[str, list[dict[str, Any]]]:
    value = result.get("data", result) if isinstance(result, dict) else result
    if not isinstance(value, dict):
        return "", []
    email = str(value.get("email") or "").strip()
    raw_emails = value.get("emails")
    emails = [item for item in raw_emails if isinstance(item, dict)] if isinstance(raw_emails, list) else []
    return email, emails


def _message_signature(message: dict[str, Any]) -> str:
    raw = "|".join(
        str(message.get(key) or "")
        for key in ("id", "message_id", "received_at", "subject", "code", "body", "body_text", "text", "html")
    )
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def mailbox_snapshot(
    config: dict[str, Any], mailbox_token: str, *, use_proxy: bool = False, proxy_url: str = ""
) -> set[str]:
    token = urllib.parse.quote(str(mailbox_token), safe="")
    result = _request_json(config, "GET", f"/m/{token}/api", use_proxy=use_proxy, proxy_url=proxy_url)
    _email, emails = _mailbox_payload(result)
    return {_message_signature(item) for item in emails}


def wait_for_code_details(
    config: dict[str, Any],
    mailbox_token: str,
    *,
    code_pattern: str = r"\b\d{4,8}\b",
    use_proxy: bool = False,
    proxy_url: str = "",
    ignore_signatures: set[str] | None = None,
) -> CodeReceipt:
    config = validate_receiver_config(config)
    token = urllib.parse.quote(str(mailbox_token), safe="")
    started = time.monotonic()
    deadline = time.monotonic() + config["timeout"]
    seen = set(ignore_signatures or set())
    poll_count = 0
    email_count = 0
    mailbox_email = ""
    last_received_at = ""
    while True:
        poll_count += 1
        try:
            result = _request_json(
                config,
                "GET",
                f"/m/{token}/api",
                use_proxy=use_proxy,
                proxy_url=proxy_url,
                timeout=min(30.0, config["timeout"]),
            )
        except SiteError as exc:
            if "HTTP 401" in str(exc) or "HTTP 403" in str(exc):
                raise
            result = {}
        response_email, emails = _mailbox_payload(result)
        mailbox_email = response_email or mailbox_email
        email_count = max(email_count, len(emails))
        ordered = sorted(emails, key=lambda item: str(item.get("received_at") or ""), reverse=True)
        if ordered:
            last_received_at = str(ordered[0].get("received_at") or last_received_at)
        for message in ordered:
            signature = _message_signature(message)
            if signature in seen:
                continue
            seen.add(signature)
            code = extract_code(message, code_pattern)
            if code:
                return CodeReceipt(
                    code=code,
                    email=mailbox_email,
                    subject=str(message.get("subject") or ""),
                    received_at=str(message.get("received_at") or ""),
                    elapsed_seconds=time.monotonic() - started,
                    poll_count=poll_count,
                    email_count=len(emails),
                )
        if time.monotonic() >= deadline:
            last_text = f"，最后邮件时间 {last_received_at}" if last_received_at else ""
            raise SiteError(
                f"临时邮箱 {mask_email(mailbox_email)} 在 {config['timeout']:g} 秒内未收到可识别验证码"
                f"（轮询 {poll_count} 次，已收到邮件 {email_count} 封{last_text}）。"
            )
        time.sleep(min(config["poll_interval"], max(deadline - time.monotonic(), 0.01)))


def wait_for_code(
    config: dict[str, Any], mailbox_token: str, *, code_pattern: str = r"\b\d{4,8}\b",
    use_proxy: bool = False, proxy_url: str = "", ignore_signatures: set[str] | None = None,
) -> str:
    return wait_for_code_details(
        config, mailbox_token, code_pattern=code_pattern, use_proxy=use_proxy,
        proxy_url=proxy_url, ignore_signatures=ignore_signatures,
    ).code


def test_connection(
    config: dict[str, Any], *, use_proxy: bool = False, proxy_url: str = ""
) -> str:
    mailbox = create_mailbox(config, use_proxy=use_proxy, proxy_url=proxy_url)
    return f"连接成功，已创建测试邮箱 {mask_email(str(mailbox['email']))}。"
