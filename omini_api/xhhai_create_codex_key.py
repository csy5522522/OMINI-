#!/usr/bin/env python3
"""
Register one account on api.xhhai.top and create a key named "codex".

The script uses the same public JSON APIs as the Vue frontend:
- POST /api/v1/auth/register
- GET  /api/v1/groups/available
- POST /api/v1/keys
"""

from __future__ import annotations

import argparse
import contextlib
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from omini_api.xhhai_codexpp_sync import (
    DEFAULT_CODEXPP_SETTINGS_PATH,
    DEFAULT_PROFILE_NAME as DEFAULT_CODEXPP_PROFILE_NAME,
    DEFAULT_WEBUI_CUSTOM_PROVIDERS_PATH,
    provider_base_url_from_api_base,
    sync_codexpp_if_present,
)
from omini_api.xhhai_concurrency_policy import DEFAULT_CONCURRENCY_BAND, parse_concurrency_band
from omini_api.xhhai_file_utils import write_json_atomic
from omini_api.xhhai_register import (
    ALLOWED_EMAIL_DOMAINS,
    DEFAULT_API_BASE,
    DEFAULT_EMAIL_DOMAIN,
    ApiError,
    emit_metric,
    get_public_settings,
    make_email,
    make_password,
    metric_timestamp,
    parse_error_message,
    request_json,
    validate_email_rule,
    validate_password_rule,
    validate_settings,
)


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


_configure_utf8_stdio()


DEFAULT_KEY_NAME = "codex"
DEFAULT_METRICS_LOG = Path("xhhai_register_metrics.jsonl")
DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
DEFAULT_GROUP_NAME = "GPT Pro分组-低延迟稳定-线路B"

class JsonlMetricLogger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def safe_url_path(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return parsed.path or "/"


def pressure_hint(status_code: int | None, elapsed_ms: float, error_type: str | None) -> str:
    if status_code in (429, 502, 503, 504):
        return "high"
    if error_type in ("network_error", "rate_limited", "server_unavailable"):
        return "high"
    if elapsed_ms >= 5000:
        return "high"
    if elapsed_ms >= 2000:
        return "elevated"
    return "normal"


def classify_workflow_error(exc: BaseException) -> str:
    if isinstance(exc, ApiError):
        text = str(exc).lower()
        if "429" in text or "too many" in text or "rate" in text:
            return "rate_limited"
        if "502" in text or "503" in text or "504" in text:
            return "server_unavailable"
        if "network error" in text or "timed out" in text or "timeout" in text:
            return "network_error"
        return "api_error"
    return type(exc).__name__


@contextlib.contextmanager
def workflow_step(
    step: str,
    metric_sink: Any | None,
    metric_context: dict[str, Any] | None = None,
) -> Any:
    started = time.perf_counter()
    ok = False
    error_type: str | None = None
    error_message: str | None = None
    try:
        yield
        ok = True
    except Exception as exc:
        error_type = classify_workflow_error(exc)
        error_message = str(exc)
        raise
    finally:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        event: dict[str, Any] = {
            "event": "workflow_step",
            "ts": metric_timestamp(),
            "step": step,
            "ok": ok,
            "elapsed_ms": elapsed_ms,
            "error_type": error_type,
            "error_message": error_message,
            "pressure_hint": pressure_hint(None, elapsed_ms, error_type),
        }
        if metric_context:
            event.update(metric_context)
        emit_metric(metric_sink, event)


def authed_request_json(
    method: str,
    url: str,
    token: str,
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
            "Authorization": f"Bearer {token}",
            "Origin": "https://api.xhhai.top",
            "Referer": "https://api.xhhai.top/keys",
            "User-Agent": "Mozilla/5.0 xhhai-create-codex-key/1.0",
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
            error_message = parse_error_message(body) or f"HTTP {exc.code}: {body[:500]}"
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


def register_account(
    api_base: str,
    email: str,
    password: str,
    *,
    metric_sink: Any | None = None,
    metric_context: dict[str, Any] | None = None,
    use_system_proxy: bool = False,
) -> dict[str, Any]:
    payload = {"email": email, "password": password}
    result = request_json(
        "POST",
        f"{api_base}/auth/register",
        payload,
        metric_sink=metric_sink,
        metric_context=metric_context,
        op="register_account",
        use_system_proxy=use_system_proxy,
    )
    if not isinstance(result, dict):
        raise ApiError("Unexpected registration response.")
    token = result.get("access_token")
    if not isinstance(token, str) or not token:
        raise ApiError("Registration response did not include an access token.")
    return result


def get_available_groups(
    api_base: str,
    token: str,
    *,
    metric_sink: Any | None = None,
    metric_context: dict[str, Any] | None = None,
    use_system_proxy: bool = False,
) -> list[dict[str, Any]]:
    result = authed_request_json(
        "GET",
        f"{api_base}/groups/available",
        token,
        metric_sink=metric_sink,
        metric_context=metric_context,
        op="get_available_groups",
        use_system_proxy=use_system_proxy,
    )
    if not isinstance(result, list):
        raise ApiError("Unexpected groups response.")
    return [item for item in result if isinstance(item, dict)]


def normalize_text(value: Any) -> str:
    return str(value or "").strip().casefold()


def pick_group(groups: list[dict[str, Any]], target_name: str) -> dict[str, Any]:
    wanted = normalize_text(target_name)

    for group in groups:
        if normalize_text(group.get("name")) == wanted:
            return group

    for group in groups:
        if wanted and wanted in normalize_text(group.get("name")):
            return group

    available = ", ".join(str(group.get("name", "")) for group in groups[:20])
    raise ApiError(f"Target group not found: {target_name}. Available: {available}")


def create_key(
    api_base: str,
    token: str,
    name: str,
    group_id: Any,
    *,
    metric_sink: Any | None = None,
    metric_context: dict[str, Any] | None = None,
    use_system_proxy: bool = False,
) -> dict[str, Any]:
    result = authed_request_json(
        "POST",
        f"{api_base}/keys",
        token,
        {"name": name, "group_id": group_id},
        metric_sink=metric_sink,
        metric_context=metric_context,
        op="create_key",
        use_system_proxy=use_system_proxy,
    )
    if not isinstance(result, dict):
        raise ApiError("Unexpected key creation response.")
    return result


def extract_api_key(key: dict[str, Any]) -> str:
    for field in ("key", "api_key", "token", "value"):
        value = key.get(field)
        if isinstance(value, str) and value:
            return value
    raise ApiError(f"Key creation response did not include an API key: {key}")


def redact_result(result: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(result)
    for field in ("access_token", "refresh_token"):
        token = redacted.get(field)
        if isinstance(token, str) and len(token) > 16:
            redacted[field] = f"{token[:8]}...{token[-8:]}"
    return redacted


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="注册 1 个 xhhai 账号，并在指定分组里创建 codex API key。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python xhhai_create_codex_key.py --email-domain qq.com\n"
            "  python xhhai_create_codex_key.py --group-name \"GPT Pro分组-低延迟稳定-线路B\" --output xhhai_codex_account.json\n"
            "  python xhhai_create_codex_key.py --use-system-proxy\n\n"
            "输出 JSON 会包含 email、password、api_key、group、key、registration 等字段。\n"
            "默认分组: GPT Pro分组-低延迟稳定-线路B。"
        ),
    )
    parser.add_argument("--email", help="指定邮箱。必须是 8-12 位数字 + @qq.com 或 @gmail.com；不传则自动生成。")
    parser.add_argument(
        "--email-domain",
        default=DEFAULT_EMAIL_DOMAIN,
        help="自动生成邮箱时使用的域名。默认: qq.com。",
    )
    parser.add_argument("--password", help="指定密码，长度必须是 6-10 位；不传则自动生成。")
    parser.add_argument("--key-name", default=DEFAULT_KEY_NAME, help="新建 API key 的名称。默认: %(default)s。")
    parser.add_argument("--group-name", default=DEFAULT_GROUP_NAME, help="目标分组名称，支持完整名或部分匹配。默认: %(default)s。")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="接口基础地址。默认: %(default)s。")
    parser.add_argument("--use-system-proxy", action="store_true", help="使用 Windows/环境变量里的系统代理或 VPN HTTP 代理路线。")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("xhhai_codex_account.json"),
        help="账号和 key 信息保存位置。默认: %(default)s。",
    )
    parser.add_argument(
        "--metrics-log",
        type=Path,
        default=DEFAULT_METRICS_LOG,
        help="把每一步和每个接口请求的耗时/错误追加到 JSONL 日志。默认: %(default)s。",
    )
    parser.add_argument(
        "--no-metrics",
        action="store_true",
        help="不写入 metrics 日志。",
    )
    parser.add_argument(
        "--concurrency-band",
        default=DEFAULT_CONCURRENCY_BAND,
        help="记录到输出 JSON 的并发策略区间。默认: %(default)s。",
    )
    parser.add_argument("--no-sync-codexpp", dest="sync_codexpp", action="store_false", default=True, help="不要同步 Codex++ 的批量注册/自定义供应商配置。")
    parser.add_argument("--codexpp-settings-path", type=Path, default=DEFAULT_CODEXPP_SETTINGS_PATH, help="Codex++ settings.json 路径；存在才会修改。默认: %(default)s。")
    parser.add_argument("--codexpp-webui-path", type=Path, default=DEFAULT_WEBUI_CUSTOM_PROVIDERS_PATH, help="Codex++ webui-custom-providers.json 路径；存在才会修改。默认: %(default)s。")
    parser.add_argument("--codexpp-profile-name", default=DEFAULT_CODEXPP_PROFILE_NAME, help="要同步的 Codex++ relay profile 名称。默认: %(default)s。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_base = args.api_base.rstrip("/")
    try:
        email = validate_email_rule(args.email) if args.email else make_email(args.email_domain)
        password = validate_password_rule(args.password) if args.password else make_password()
        concurrency_min, concurrency_max = parse_concurrency_band(args.concurrency_band)
    except ValueError as exc:
        print(f"Invalid concurrency band: {exc}", file=sys.stderr)
        return 2
    except ApiError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    run_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
    metric_sink = None if args.no_metrics else JsonlMetricLogger(args.metrics_log)
    metric_context = {
        "run_id": run_id,
        "email": email,
        "concurrency_band": args.concurrency_band,
        "concurrency_min": concurrency_min,
        "concurrency_max": concurrency_max,
    }
    workflow_started = time.perf_counter()
    workflow_ok = False
    workflow_error_type: str | None = None
    workflow_error_message: str | None = None

    try:
        with workflow_step("settings_check", metric_sink, metric_context):
            settings = get_public_settings(
                api_base,
                metric_sink=metric_sink,
                metric_context=metric_context,
                use_system_proxy=args.use_system_proxy,
            )
            validate_settings(settings, invitation_code=None)

        with workflow_step("register_account", metric_sink, metric_context):
            registration = register_account(
                api_base,
                email,
                password,
                metric_sink=metric_sink,
                metric_context=metric_context,
                use_system_proxy=args.use_system_proxy,
            )
        token = registration["access_token"]

        with workflow_step("get_available_groups", metric_sink, metric_context):
            groups = get_available_groups(
                api_base,
                token,
                metric_sink=metric_sink,
                metric_context=metric_context,
                use_system_proxy=args.use_system_proxy,
            )

        with workflow_step("pick_group", metric_sink, metric_context):
            group = pick_group(groups, args.group_name)
            group_id = group.get("id")
            if group_id is None:
                raise ApiError(f"Matched group has no id: {group}")

        with workflow_step("create_key", metric_sink, metric_context):
            key = create_key(
                api_base,
                token,
                args.key_name,
                group_id,
                metric_sink=metric_sink,
                metric_context=metric_context,
                use_system_proxy=args.use_system_proxy,
            )
            api_key = extract_api_key(key)

        with workflow_step("save_output", metric_sink, metric_context):
            output = {
                "site": "https://api.xhhai.top",
                "email": email,
                "password": password,
                "api_key": api_key,
                "balance_initial": 5.0,
                "concurrency_policy": {
                    "band": args.concurrency_band,
                    "minimum": concurrency_min,
                    "maximum": concurrency_max,
                },
                "group": group,
                "key": key,
                "registration": registration,
            }
            write_json_atomic(args.output, output)

        sync_result = None
        if args.sync_codexpp:
            with workflow_step("sync_codexpp", metric_sink, metric_context):
                sync_result = sync_codexpp_if_present(
                    api_key=api_key,
                    base_url=provider_base_url_from_api_base(api_base),
                    settings_path=args.codexpp_settings_path.expanduser(),
                    webui_path=args.codexpp_webui_path.expanduser(),
                    profile_name=args.codexpp_profile_name,
                )

        print("Done.")
        print(f"Email: {email}")
        print(f"Password: {password}")
        print(f"Concurrency band: {args.concurrency_band}")
        print(f"Group: {group.get('name')} (id={group_id})")
        print(f"Key name: {key.get('name', args.key_name)}")
        print(f"API key: ...{api_key[-8:]}")
        print(f"Output: {args.output}")
        if sync_result is not None:
            print(f"Codex++ sync: {sync_result.summary()}")
        print("Registration:", json.dumps(redact_result(registration), ensure_ascii=False))
        workflow_ok = True
        return 0
    except ApiError as exc:
        workflow_error_type = classify_workflow_error(exc)
        workflow_error_message = str(exc)
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    finally:
        elapsed_ms = round((time.perf_counter() - workflow_started) * 1000, 2)
        event: dict[str, Any] = {
            "event": "workflow_run",
            "ts": metric_timestamp(),
            "ok": workflow_ok,
            "elapsed_ms": elapsed_ms,
            "error_type": workflow_error_type,
            "error_message": workflow_error_message,
            "pressure_hint": pressure_hint(None, elapsed_ms, workflow_error_type),
        }
        event.update(metric_context)
        emit_metric(metric_sink, event)


if __name__ == "__main__":
    raise SystemExit(main())
