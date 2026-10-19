#!/usr/bin/env python3
"""
Register one account through the public registration API used by:
https://api.xhhai.top/register
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


_configure_utf8_stdio()


DEFAULT_API_BASE = "https://api.xhhai.top/api/v1"
ALLOWED_EMAIL_DOMAINS = ("qq.com", "gmail.com")
DEFAULT_EMAIL_DOMAIN = "qq.com"
MIN_EMAIL_DIGITS = 8
MAX_EMAIL_DIGITS = 12
MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 10
DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ApiError(RuntimeError):
    pass


def metric_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def emit_metric(metric_sink: Any | None, event: dict[str, Any]) -> None:
    if metric_sink is None:
        return
    if hasattr(metric_sink, "record"):
        metric_sink.record(event)
        return
    metric_sink(event)


def safe_url_path(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return parsed.path or "/"


def make_digit_string(min_length: int = MIN_EMAIL_DIGITS, max_length: int = MAX_EMAIL_DIGITS) -> str:
    length = min_length + secrets.randbelow(max_length - min_length + 1)
    first = secrets.choice("123456789")
    rest = "".join(secrets.choice(string.digits) for _ in range(length - 1))
    return first + rest


def make_email(domain: str = DEFAULT_EMAIL_DOMAIN) -> str:
    normalized_domain = domain.strip().lower().lstrip("@")
    if not re.fullmatch(r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}", normalized_domain):
        raise ApiError(f"Invalid email domain: {domain}")
    return f"{make_digit_string()}@{normalized_domain}"


def validate_email_rule(email: str) -> str:
    normalized = email.strip().lower()
    local, sep, domain = normalized.partition("@")
    if not sep:
        raise ApiError("Email must include @.")
    if not re.fullmatch(r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}", domain):
        raise ApiError(f"Invalid email domain: {domain}")
    if not local.isdigit() or not (MIN_EMAIL_DIGITS <= len(local) <= MAX_EMAIL_DIGITS):
        raise ApiError("Email local part must be 8-12 digits.")
    return normalized


def make_password(length: int | None = None) -> str:
    if length is None:
        length = MIN_PASSWORD_LENGTH + secrets.randbelow(MAX_PASSWORD_LENGTH - MIN_PASSWORD_LENGTH + 1)
    if not (MIN_PASSWORD_LENGTH <= length <= MAX_PASSWORD_LENGTH):
        raise ValueError(f"Password length must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH}.")
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def validate_password_rule(password: str) -> str:
    if not (MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH):
        raise ApiError(f"Password length must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH}.")
    return password


def pressure_hint(status_code: int | None, elapsed_ms: float, error_type: str | None) -> str:
    if status_code in (429, 502, 503, 504):
        return "high"
    if error_type == "network_error":
        return "high"
    if elapsed_ms >= 5000:
        return "high"
    if elapsed_ms >= 2000:
        return "elevated"
    return "normal"


def request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: int = 30,
    metric_sink: Any | None = None,
    metric_context: dict[str, Any] | None = None,
    op: str | None = None,
    use_system_proxy: bool = False,
) -> Any:
    started = time.perf_counter()
    status_code: int | None = None
    ok = False
    error_type: str | None = None
    error_message: str | None = None
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Language": "zh",
            "Origin": "https://api.xhhai.top",
            "Referer": "https://api.xhhai.top/register",
            "User-Agent": "Mozilla/5.0 xhhai-register-script/1.0",
        },
    )

    try:
        try:
            opener = urllib.request.urlopen if use_system_proxy else DIRECT_OPENER.open
            with opener(req, timeout=timeout) as response:
                status_code = response.status
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            body = exc.read().decode("utf-8", errors="replace")
            error_type = "http_error"
            error_message = parse_error_message(body) or f"HTTP {exc.code}"
            raise ApiError(error_message) from exc
        except urllib.error.URLError as exc:
            error_type = "network_error"
            error_message = f"Network error: {exc.reason}"
            raise ApiError(error_message) from exc

        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            error_type = "json_error"
            error_message = f"Non-JSON response: {body[:300]}"
            raise ApiError(error_message) from exc

        if isinstance(result, dict) and "code" in result:
            if result.get("code") == 0:
                ok = True
                return result.get("data")
            error_type = "api_error"
            error_message = str(result.get("message") or result.get("detail") or result)
            raise ApiError(error_message)

        ok = True
        return result
    finally:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        event: dict[str, Any] = {
            "event": "http_request",
            "ts": metric_timestamp(),
            "op": op or safe_url_path(url),
            "method": method,
            "path": safe_url_path(url),
            "status_code": status_code,
            "ok": ok,
            "elapsed_ms": elapsed_ms,
            "error_type": error_type,
            "error_message": error_message,
            "pressure_hint": pressure_hint(status_code, elapsed_ms, error_type),
        }
        if metric_context:
            event.update(metric_context)
        emit_metric(metric_sink, event)


def parse_error_message(body: str) -> str | None:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return body[:300] if body else None

    if isinstance(parsed, dict):
        return str(
            parsed.get("message")
            or parsed.get("detail")
            or parsed.get("error")
            or parsed
        )
    return str(parsed)


def get_public_settings(
    api_base: str,
    *,
    metric_sink: Any | None = None,
    metric_context: dict[str, Any] | None = None,
    use_system_proxy: bool = False,
) -> dict[str, Any]:
    settings = request_json(
        "GET",
        f"{api_base.rstrip('/')}/settings/public",
        metric_sink=metric_sink,
        metric_context=metric_context,
        op="settings_check",
        use_system_proxy=use_system_proxy,
    )
    if not isinstance(settings, dict):
        raise ApiError("Unexpected public settings response.")
    return settings


def validate_settings(settings: dict[str, Any], invitation_code: str | None) -> None:
    if settings.get("registration_enabled") is False:
        raise ApiError("This site has disabled registration.")
    if settings.get("turnstile_enabled"):
        raise ApiError("Turnstile is enabled. Please register in the browser.")
    if settings.get("email_verify_enabled"):
        raise ApiError("Email verification is enabled. This script does not handle it.")
    if settings.get("invitation_code_enabled") and not invitation_code:
        raise ApiError("Invitation code is required. Pass --invitation-code.")


def resolve_credentials(args: argparse.Namespace) -> tuple[str, str]:
    email = validate_email_rule(args.email) if args.email else make_email(args.email_domain)
    password = validate_password_rule(args.password) if args.password else make_password()
    return email, password


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    email, password = resolve_credentials(args)
    payload: dict[str, Any] = {
        "email": email,
        "password": password,
    }

    optional_fields = {
        "promo_code": args.promo_code,
        "invitation_code": args.invitation_code,
        "aff_code": args.aff_code,
    }
    for key, value in optional_fields.items():
        if value:
            payload[key] = value

    return payload


def register(
    api_base: str,
    payload: dict[str, Any],
    *,
    metric_sink: Any | None = None,
    metric_context: dict[str, Any] | None = None,
    use_system_proxy: bool = False,
) -> dict[str, Any]:
    result = request_json(
        "POST",
        f"{api_base.rstrip('/')}/auth/register",
        payload,
        metric_sink=metric_sink,
        metric_context=metric_context,
        op="register_account",
        use_system_proxy=use_system_proxy,
    )
    if not isinstance(result, dict):
        raise ApiError("Unexpected registration response.")
    return result


def save_session(path: Path, payload: dict[str, Any], result: dict[str, Any]) -> None:
    session = {
        "email": payload.get("email"),
        "password": payload.get("password"),
        "access_token": result.get("access_token"),
        "refresh_token": result.get("refresh_token"),
        "expires_in": result.get("expires_in"),
        "user": result.get("user"),
    }
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


def mask_token(token: Any) -> str:
    if not isinstance(token, str) or not token:
        return ""
    if len(token) <= 16:
        return "*" * len(token)
    return f"{token[:8]}...{token[-8:]}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="注册 1 个 xhhai 账号，只保存注册返回的登录信息。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python xhhai_register.py --email-domain qq.com --save-session session.json\n"
            "  python xhhai_register.py --email 12345678@qq.com --password abc123 --show-token\n\n"
            "账号规则:\n"
            "  邮箱必须是 8-12 位数字 + @qq.com 或 @gmail.com。\n"
            "  密码必须是 6-10 位字符；不传时自动生成。\n"
            "  默认不走系统代理；VPN/代理需要接管系统 HTTP 代理时加 --use-system-proxy。"
        ),
    )
    parser.add_argument("--email", help="指定邮箱。必须是 8-12 位数字 + @qq.com 或 @gmail.com；不传则自动生成。")
    parser.add_argument(
        "--email-domain",
        default=DEFAULT_EMAIL_DOMAIN,
        help="自动生成邮箱时使用的域名。默认: qq.com。",
    )
    parser.add_argument("--password", help="指定密码，长度必须是 6-10 位；不传则自动生成。")
    parser.add_argument("--promo-code", help="可选，注册促销码。")
    parser.add_argument("--invitation-code", help="可选，邀请码；站点开启邀请码时需要。")
    parser.add_argument("--aff-code", help="可选，推广/联盟码。")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="接口基础地址。默认: %(default)s。")
    parser.add_argument("--use-system-proxy", action="store_true", help="使用 Windows/环境变量里的系统代理或 VPN HTTP 代理路线。")
    parser.add_argument(
        "--skip-settings-check",
        action="store_true",
        help="跳过注册开关、验证码、邮箱验证等公开设置检查。",
    )
    parser.add_argument(
        "--save-session",
        type=Path,
        help="把注册返回的 access_token、refresh_token、用户信息保存到指定 JSON。",
    )
    parser.add_argument(
        "--show-token",
        action="store_true",
        help="注册成功后在屏幕上显示脱敏后的 access_token。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_base = args.api_base.rstrip("/")

    try:
        if not args.skip_settings_check:
            settings = get_public_settings(api_base, use_system_proxy=args.use_system_proxy)
            validate_settings(settings, args.invitation_code)

        payload = build_payload(args)
        result = register(api_base, payload, use_system_proxy=args.use_system_proxy)

        user = result.get("user") or {}
        email = user.get("email") or payload["email"]
        print(f"Registration succeeded: {email}")
        print(f"Password: {payload['password']}")

        if args.show_token:
            print(f"Access token: {mask_token(result.get('access_token'))}")

        if args.save_session:
            save_session(args.save_session, payload, result)
            print(f"Session saved: {args.save_session}")

        return 0
    except ApiError as exc:
        print(f"Registration failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCanceled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
