#!/usr/bin/env python3
"""Local OpenAI-compatible relay for xhhai API key rotation."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from omini_api.xhhai_key_manager import (
    DEFAULT_ACCOUNT_GLOB,
    DEFAULT_API_BASE,
    DEFAULT_LOG_PATH,
    DEFAULT_STATE_PATH,
    Account,
    account_for_key,
    account_state_id,
    depleted_accounts,
    format_account,
    is_account_depleted,
    key_suffix,
    load_accounts,
    load_rotation_state,
    log,
    mark_account_depleted,
    query_account_balance,
    read_account_cached_balance,
    rotation_state_path,
    save_rotation_state,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_CLAUDE_PORT = 8788
DEFAULT_UPSTREAM_BASE_URL = "https://api.xhhai.top"
DEFAULT_PROXY_AUTH_KEY = "xhhai-local-proxy"
DEFAULT_NON200_LOG_DIR = Path("xhhai_upstream_non200_logs")
MAX_NON200_BODY_CHARS = 12000
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
SENSITIVE_HEADER_NAMES = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
}


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


_configure_utf8_stdio()


class ProxyState:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.lock = threading.Lock()

    @property
    def log_path(self) -> Path | None:
        return self.args.log_path if self.args.log_path else None

    @property
    def non200_log_dir(self) -> Path:
        return self.args.non200_log_dir if self.args.non200_log_dir else DEFAULT_NON200_LOG_DIR

    def load_accounts(self) -> list[Account]:
        return load_accounts(self.args.accounts)

    def is_depleted(self, state: dict[str, Any], account: Account) -> bool:
        if not is_account_depleted(state, account):
            return False
        balance, source = read_account_cached_balance(account.path)
        if balance is not None and balance > 0:
            log(
                f"[local-proxy] ignoring stale depleted state because local balance is positive: "
                f"{account.email} balance={balance:.4f} source={source} key=...{key_suffix(account.api_key)}",
                self.log_path,
            )
            return False
        return True

    def current_account(self, accounts: list[Account]) -> Account | None:
        state = load_rotation_state(rotation_state_path(self.args))
        suffix = state.get("last_api_key_suffix")
        email = state.get("last_email")
        for account in accounts:
            if account.api_key[-8:] == suffix and account.email == email and not self.is_depleted(state, account):
                return account
        for account in accounts:
            if not self.is_depleted(state, account):
                return account
        return None

    def choose_next_account(self, accounts: list[Account], current: Account | None) -> Account | None:
        state = load_rotation_state(rotation_state_path(self.args))
        if not accounts:
            return None
        start = accounts.index(current) if current in accounts else -1
        for offset in range(1, len(accounts) + 1):
            candidate = accounts[(start + offset) % len(accounts)]
            if current is not None and candidate.api_key == current.api_key:
                continue
            if self.is_depleted(state, candidate):
                continue
            return candidate
        return None

    def mark_depleted(self, account: Account) -> None:
        with self.lock:
            state = load_rotation_state(rotation_state_path(self.args))
            mark_account_depleted(self.args, state, account, self.log_path)

    def remember_current(self, account: Account) -> None:
        with self.lock:
            state = load_rotation_state(rotation_state_path(self.args))
            if state.get("last_email") or state.get("last_api_key_suffix"):
                return
            save_rotation_state(self.args, account, self.log_path)


def upstream_base_from_api_base(api_base: str) -> str:
    value = api_base.strip().rstrip("/")
    for suffix in ("/api/v1", "/api"):
        if value.lower().endswith(suffix):
            return value[: -len(suffix)]
    return value or DEFAULT_UPSTREAM_BASE_URL


def is_depleted_response(status: int, body: bytes) -> bool:
    if status not in (400, 402):
        return False
    text = body[:2000].decode("utf-8", errors="ignore").lower()
    positive_markers = (
        "insufficient_balance",
        "insufficient account balance",
        "account balance is insufficient",
        "balance_not_enough",
        "not enough balance",
        "余额不足",
    )
    transient_markers = (
        "service unavailable",
        "temporarily unavailable",
        "timeout",
        "timed out",
        "rate limit",
        "too many requests",
        "overloaded",
        "upstream",
        "503",
        "502",
        "504",
    )
    return any(marker in text for marker in positive_markers) and not any(marker in text for marker in transient_markers)


def copy_response_headers(handler: BaseHTTPRequestHandler, headers: Any) -> None:
    for key, value in headers.items():
        if key.lower() in HOP_BY_HOP_HEADERS:
            continue
        handler.send_header(key, value)


def build_upstream_headers(handler: BaseHTTPRequestHandler, account: Account, body: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    protocol = getattr(handler.server.proxy_state.args, "protocol", "openai")  # type: ignore[attr-defined]
    for key, value in handler.headers.items():
        lower = key.lower()
        if lower in HOP_BY_HOP_HEADERS or lower in {"authorization", "x-api-key"}:
            continue
        headers[key] = value
    if protocol == "anthropic":
        headers["x-api-key"] = account.api_key
        headers.setdefault("anthropic-version", "2023-06-01")
    else:
        headers["Authorization"] = f"Bearer {account.api_key}"
    headers["Accept-Encoding"] = "identity"
    if body:
        headers["Content-Length"] = str(len(body))
    return headers


def translate_local_path_to_upstream(path: str) -> str:
    # Codex Desktop's provider test invokes ``/responses`` directly even when
    # its profile's base URL includes ``/api/v1``.  Accept both wire forms so
    # the local relay works for the test button as well as normal OpenAI SDKs.
    if path in {"", "/"}:
        return "/v1"
    if path in {"/responses", "/models"}:
        return "/v1" + path
    if path == "/api/v1":
        return "/v1"
    if path.startswith("/api/v1/"):
        return "/v1/" + path[len("/api/v1/"):]
    return path


def is_supported_openai_path(path: str) -> bool:
    return path in {
        "/", "/responses", "/models",
        "/api/v1/responses", "/api/v1/models", "/api/v1", "/api/v1/",
        "/v1/responses", "/v1/models", "/v1", "/v1/",
    }


def is_supported_anthropic_path(path: str) -> bool:
    return (
        path in {"/v1", "/v1/", "/v1/messages", "/v1/messages/count_tokens", "/v1/models", "/v1/complete"}
        or path.startswith("/v1/messages/")
        or path.startswith("/v1/models/")
    )


def translate_local_path(path: str, protocol: str) -> str:
    if protocol == "anthropic":
        return path
    return translate_local_path_to_upstream(path)


def utc_timestamp() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def safe_headers(headers: Any) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    try:
        items = headers.items()
    except AttributeError:
        items = []
    for key, value in items:
        if key.lower() in SENSITIVE_HEADER_NAMES:
            sanitized[key] = "[redacted]"
        else:
            sanitized[key] = str(value)
    return sanitized


def body_preview(body: bytes, limit: int = MAX_NON200_BODY_CHARS) -> str:
    text = body.decode("utf-8", errors="replace")
    if len(text) > limit:
        return text[:limit] + f"...[truncated {len(text) - limit} chars]"
    return text


def classify_non200_status(status: int) -> str:
    if status == 400:
        return "bad_request"
    if status in (401, 403):
        return "auth_or_forbidden"
    if status == 402:
        return "payment_or_balance"
    if status == 404:
        return "not_found"
    if status == 408:
        return "timeout"
    if status == 409:
        return "conflict"
    if status == 413:
        return "payload_too_large"
    if status == 422:
        return "validation"
    if status == 429:
        return "rate_limited"
    if status in (500, 502, 503, 504):
        return "upstream_server_error"
    if status == 554:
        return "upstream_unknown_554"
    if 400 <= status < 500:
        return "client_error"
    if 500 <= status < 600:
        return "server_error"
    return "non_success"


def proxy_prefix(args: argparse.Namespace) -> str:
    return "[claude-proxy]" if getattr(args, "protocol", "openai") == "anthropic" else "[local-proxy]"


def log_non200_upstream_response(
    state: ProxyState,
    *,
    status: int,
    account: Account,
    upstream_url: str,
    method: str,
    request_headers: dict[str, str],
    request_body: bytes,
    response_headers: Any,
    response_body: bytes,
) -> None:
    if not getattr(state.args, "log_non200_details", False):
        return

    entry = {
        "ts": utc_timestamp(),
        "category": classify_non200_status(status),
        "status": status,
        "method": method,
        "url": upstream_url,
        "account_email": account.email,
        "api_key_suffix": key_suffix(account.api_key),
        "request_headers": safe_headers(request_headers),
        "request_body": body_preview(request_body),
        "response_headers": safe_headers(response_headers),
        "response_body": body_preview(response_body),
    }
    try:
        log_dir = state.non200_log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"xhhai_upstream_non200_{status}.jsonl"
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        log(f"{proxy_prefix(state.args)} non-200 upstream details recorded: status={status} category={entry['category']} file={log_file}", state.log_path)
    except Exception as exc:
        log(f"{proxy_prefix(state.args)} non-200 detail logging failed: {exc}", state.log_path)


class LocalProxyHandler(BaseHTTPRequestHandler):
    server_version = "XHHAILocalProxy/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        state: ProxyState = self.server.proxy_state  # type: ignore[attr-defined]
        log(f"{proxy_prefix(state.args)} " + (fmt % args), state.log_path)

    def do_GET(self) -> None:
        self.handle_proxy()

    def do_POST(self) -> None:
        self.handle_proxy()

    def do_PUT(self) -> None:
        self.handle_proxy()

    def do_PATCH(self) -> None:
        self.handle_proxy()

    def do_DELETE(self) -> None:
        self.handle_proxy()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "authorization,content-type,openai-beta,x-api-key,anthropic-version,anthropic-beta")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS")
        self.end_headers()

    def handle_proxy(self) -> None:
        state: ProxyState = self.server.proxy_state  # type: ignore[attr-defined]
        protocol = getattr(state.args, "protocol", "openai")
        request_path = urllib.parse.urlsplit(self.path).path or "/"
        supported = is_supported_anthropic_path(request_path) if protocol == "anthropic" else is_supported_openai_path(request_path)
        if not supported:
            self.send_json(404, {"error": f"unsupported path for xhhai {protocol} local proxy", "path": self.path})
            return

        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length else b""
        try:
            accounts = state.load_accounts()
        except Exception as exc:
            self.send_json(503, {"error": f"account load failed: {exc}"})
            return

        account = state.current_account(accounts)
        attempts = 0
        max_attempts = max(len(accounts), 1)
        while account is not None and attempts < max_attempts:
            attempts += 1
            status, response_headers, response_body = self.forward_once(state, account, body)
            if is_depleted_response(status, response_body):
                state.mark_depleted(account)
                log(
                    f"{proxy_prefix(state.args)} upstream reported depleted key; retrying next account. "
                    f"{account.email} key=...{key_suffix(account.api_key)}",
                    state.log_path,
                )
                account = state.choose_next_account(accounts, account)
                continue

            if 200 <= status < 300:
                state.remember_current(account)
            self.send_response(status)
            copy_response_headers(self, response_headers)
            self.send_header("X-XHHAI-Account", account.email)
            self.send_header("X-XHHAI-Key-Suffix", key_suffix(account.api_key))
            self.end_headers()
            if response_body:
                self.write_response_body(response_body, state)
            return

        self.send_json(503, {"error": "no usable xhhai account key is available"})

    def forward_once(self, state: ProxyState, account: Account, body: bytes) -> tuple[int, Any, bytes]:
        parsed = urllib.parse.urlsplit(self.path)
        path = translate_local_path(parsed.path or "/", getattr(state.args, "protocol", "openai"))
        upstream = state.args.upstream_base_url.rstrip("/") + urllib.parse.urlunsplit(("", "", path, parsed.query, ""))
        headers = build_upstream_headers(self, account, body)
        req = urllib.request.Request(upstream, data=body if body else None, method=self.command, headers=headers)
        proxy_url = str(getattr(state.args, "proxy_url", "") or "").strip()
        if state.args.use_system_proxy and proxy_url:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            ).open
        elif state.args.use_system_proxy:
            opener = urllib.request.urlopen
        else:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        try:
            with opener(req, timeout=state.args.timeout) as response:
                response_body = response.read()
                if response.status != 200:
                    log_non200_upstream_response(
                        state,
                        status=response.status,
                        account=account,
                        upstream_url=upstream,
                        method=self.command,
                        request_headers=headers,
                        request_body=body,
                        response_headers=response.headers,
                        response_body=response_body,
                    )
                return response.status, response.headers, response_body
        except urllib.error.HTTPError as exc:
            response_body = exc.read()
            log_non200_upstream_response(
                state,
                status=exc.code,
                account=account,
                upstream_url=upstream,
                method=self.command,
                request_headers=headers,
                request_body=body,
                response_headers=exc.headers,
                response_body=response_body,
            )
            return exc.code, exc.headers, response_body
        except Exception as exc:
            log(f"{proxy_prefix(state.args)} upstream request failed for {account.email}: {exc}", state.log_path)
            response_body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
            log_non200_upstream_response(
                state,
                status=502,
                account=account,
                upstream_url=upstream,
                method=self.command,
                request_headers=headers,
                request_body=body,
                response_headers={},
                response_body=response_body,
            )
            return 502, {}, response_body

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        state: ProxyState = self.server.proxy_state  # type: ignore[attr-defined]
        self.write_response_body(body, state)

    def write_response_body(self, body: bytes, state: ProxyState) -> None:
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError) as exc:
            log(f"{proxy_prefix(state.args)} client disconnected before response completed: {exc}", state.log_path)


def precheck_first_account(args: argparse.Namespace, state: ProxyState) -> None:
    try:
        accounts = state.load_accounts()
        account = state.current_account(accounts)
        if account is None:
            log(f"{proxy_prefix(args)} no usable account found during startup.", state.log_path)
            return
        account.balance = query_account_balance(args.api_base.rstrip("/"), account, use_system_proxy=args.use_system_proxy)
        log(f"{proxy_prefix(args)} startup current account: {format_account(account)} key=...{key_suffix(account.api_key)}", state.log_path)
        save_rotation_state(args, account, state.log_path)
    except Exception as exc:
        log(f"{proxy_prefix(args)} startup balance precheck skipped: {exc}", state.log_path)


def run_proxy(args: argparse.Namespace) -> int:
    args.accounts = args.accounts or [DEFAULT_ACCOUNT_GLOB]
    args.upstream_base_url = args.upstream_base_url or upstream_base_from_api_base(args.api_base)
    state = ProxyState(args)
    if args.precheck:
        precheck_first_account(args, state)

    server = ThreadingHTTPServer((args.host, args.port), LocalProxyHandler)
    server.proxy_state = state  # type: ignore[attr-defined]
    mapping = "passes /v1 through" if getattr(args, "protocol", "openai") == "anthropic" else "maps /api/v1 -> /v1"
    log(
        f"{proxy_prefix(args)} listening on http://{args.host}:{args.port} -> {args.upstream_base_url} "
        f"({mapping}); "
        f"accounts={','.join(args.accounts)}",
        state.log_path,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log(f"{proxy_prefix(args)} stopped by keyboard interrupt.", state.log_path)
    finally:
        server.server_close()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local xhhai API key rotation proxy.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--accounts", action="append", default=[])
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--upstream-base-url", default=DEFAULT_UPSTREAM_BASE_URL)
    parser.add_argument("--protocol", choices=("openai", "anthropic"), default="openai")
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--use-system-proxy", action="store_true")
    parser.add_argument("--proxy-url", default="")
    parser.add_argument("--precheck", action="store_true")
    parser.add_argument("--log-non200-details", action="store_true", help="记录本地代理上游非 200 响应的详细信息，默认关闭。")
    parser.add_argument("--non200-log-dir", type=Path, default=DEFAULT_NON200_LOG_DIR, help="非 200 详细日志目录。默认: %(default)s。")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    return run_proxy(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
