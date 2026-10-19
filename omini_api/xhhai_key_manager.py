#!/usr/bin/env python3
"""
Monitor xhhai account balances and switch the Codex API key automatically.

Balance belongs to the xhhai account, not to the API key string itself, so this
manager keeps each account's login token alongside its API key and checks
GET /api/v1/auth/me for the account balance.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import msvcrt
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from omini_api.xhhai_codexpp_sync import (
    DEFAULT_CODEXPP_SETTINGS_PATH,
    DEFAULT_PROFILE_NAME as DEFAULT_CODEXPP_PROFILE_NAME,
    DEFAULT_WEBUI_CUSTOM_PROVIDERS_PATH,
    CodexPlusSyncResult,
    read_codexpp_profile_state,
    sync_codexpp_if_present,
    urls_match,
)


DEFAULT_API_BASE = "https://api.xhhai.top/api/v1"
DEFAULT_MANAGED_BASE_URL = "https://api.xhhai.top"
DEFAULT_AUTH_PATH = Path.home() / ".codex" / "auth.json"
DEFAULT_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
DEFAULT_ACCOUNT_GLOB = "xhhai_codex_account*.json"
DEFAULT_THRESHOLD = 0.5
DEFAULT_INTERVAL_SECONDS = 1
DEFAULT_ROTATE_SECONDS = 3
DEFAULT_STATE_PATH = Path("xhhai_key_manager_state.json")
DEFAULT_LOG_PATH = Path("xhhai_key_manager.log")
DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
DEPLETED_BALANCE_THRESHOLD = 0.0
DEFAULT_INITIAL_BALANCE = 5.0


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


_configure_utf8_stdio()


class ManagerError(RuntimeError):
    pass


@dataclass
class Account:
    path: Path
    email: str
    api_key: str
    access_token: str
    refresh_token: str
    password: str = ""
    balance: float | None = None
    error: str | None = None


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str, log_path: Path | None = None) -> None:
    line = f"{now()} {message}"
    print(line, flush=True)
    if log_path:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def parse_error_message(body: str) -> str | None:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return body[:300] if body else None
    if isinstance(parsed, dict):
        return str(parsed.get("message") or parsed.get("detail") or parsed.get("error") or parsed)
    return str(parsed)


def request_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
    use_system_proxy: bool = False,
) -> Any:
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Language": "zh",
        "User-Agent": "Mozilla/5.0 xhhai-key-manager/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        opener = urllib.request.urlopen if use_system_proxy else DIRECT_OPENER.open
        with opener(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ManagerError(parse_error_message(body) or f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ManagerError(f"Network error: {exc.reason}") from exc

    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ManagerError(f"Non-JSON response: {body[:300]}") from exc

    if isinstance(result, dict) and "code" in result:
        if result.get("code") == 0:
            return result.get("data")
        raise ManagerError(str(result.get("message") or result.get("detail") or result))
    return result


def load_account(path: Path) -> Account:
    data = json.loads(path.read_text(encoding="utf-8"))
    registration = data.get("registration") or {}
    user = registration.get("user") or {}
    email = str(data.get("email") or user.get("email") or path.stem)
    password = str(data.get("password") or "")
    api_key = str(data.get("api_key") or (data.get("key") or {}).get("key") or "")
    access_token = str(registration.get("access_token") or "")
    refresh_token = str(registration.get("refresh_token") or "")

    if not api_key:
        raise ManagerError(f"{path} has no api_key")
    if not access_token:
        raise ManagerError(f"{path} has no registration.access_token")
    if not refresh_token:
        raise ManagerError(f"{path} has no registration.refresh_token")

    return Account(path, email, api_key, access_token, refresh_token, password=password)


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_account_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def read_account_initial_balance(path: Path) -> float | None:
    data = read_account_json(path)
    if not data:
        return None
    value = _float_or_none(data.get("balance_initial"))
    if value is not None:
        return value
    cache = data.get("balance_cache")
    if isinstance(cache, dict):
        return _float_or_none(cache.get("initial"))
    return None


def read_account_cached_balance(path: Path) -> tuple[float | None, str]:
    data = read_account_json(path)
    cache = data.get("balance_cache") if isinstance(data, dict) else None
    if isinstance(cache, dict):
        for key in ("balance", "current_balance", "remaining"):
            value = _float_or_none(cache.get(key))
            if value is not None:
                return value, f"balance_cache.{key}"

    initial = read_account_initial_balance(path)
    if initial is not None:
        return initial, "balance_initial"
    return None, "missing"


def save_account_balance_cache(account: Account, balance: float, initial: float | None = None, consumed: float | None = None) -> None:
    data = read_account_json(account.path)
    if not data:
        return
    if initial is None:
        initial = read_account_initial_balance(account.path)
    if initial is None:
        initial = balance
    data["balance_initial"] = initial
    cache: dict[str, Any] = {
        "balance": balance,
        "updated_at": now(),
    }
    if initial is not None:
        cache["initial"] = initial
    if consumed is not None:
        cache["consumed"] = consumed
    data["balance_cache"] = cache
    account.path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _account_depleted_record(account: Account) -> dict[str, Any]:
    return {
        "email": account.email,
        "account_path": str(account.path.resolve()),
        "api_key_suffix": account.api_key[-8:],
        "balance": account.balance,
        "marked_at": now(),
    }


def load_accounts(patterns: list[str]) -> list[Account]:
    paths: list[Path] = []
    for pattern in patterns:
        matches = sorted(Path(p) for p in glob.glob(pattern))
        if matches:
            paths.extend(matches)
        else:
            literal = Path(pattern)
            if literal.exists():
                paths.append(literal)

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)

    accounts: list[Account] = []
    skipped: list[str] = []
    for path in unique:
        try:
            accounts.append(load_account(path))
        except Exception as exc:
            skipped.append(f"{path}: {exc}")
            print(f"{now()} [WARN] skipped invalid account file: {path}: {exc}", file=sys.stderr, flush=True)
    if not accounts:
        detail = "; ".join(skipped[:3])
        suffix = f" skipped={len(skipped)} sample={detail}" if skipped else ""
        raise ManagerError(f"No valid account files found.{suffix}")
    return accounts


def save_account_tokens(account: Account, token_data: dict[str, Any]) -> None:
    data = json.loads(account.path.read_text(encoding="utf-8"))
    registration = data.setdefault("registration", {})
    registration["access_token"] = token_data.get("access_token", account.access_token)
    registration["refresh_token"] = token_data.get("refresh_token", account.refresh_token)
    registration["expires_in"] = token_data.get("expires_in", registration.get("expires_in"))
    registration["token_type"] = token_data.get("token_type", registration.get("token_type", "Bearer"))
    user = registration.get("user")
    if not isinstance(user, dict):
        user = {}
    if isinstance(token_data.get("user"), dict):
        for key, value in token_data["user"].items():
            if key == "balance":
                continue
            if key not in user:
                user[key] = value
    registration["user"] = user
    account.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    account.access_token = registration["access_token"]
    account.refresh_token = registration["refresh_token"]


def should_refresh_token_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "401",
        "unauthorized",
        "invalid",
        "expired",
        "token has expired",
        "jwt expired",
        "token expired",
        "not authenticated",
        "authentication",
        "登录已过期",
        "token过期",
        "令牌过期",
    )
    return any(marker in text for marker in markers)


def refresh_account_token(api_base: str, account: Account, use_system_proxy: bool = False) -> None:
    token_data = request_json(
        "POST",
        f"{api_base}/auth/refresh",
        payload={"refresh_token": account.refresh_token},
        use_system_proxy=use_system_proxy,
    )
    if not isinstance(token_data, dict) or not token_data.get("access_token"):
        raise ManagerError("Refresh response did not include access_token")
    save_account_tokens(account, token_data)


def login_account_token(api_base: str, account: Account, use_system_proxy: bool = False) -> None:
    if not account.email or not account.password:
        raise ManagerError(f"{account.path} cannot login because email/password is missing")
    token_data = request_json(
        "POST",
        f"{api_base}/auth/login",
        payload={"email": account.email, "password": account.password},
        use_system_proxy=use_system_proxy,
    )
    if not isinstance(token_data, dict) or not token_data.get("access_token"):
        raise ManagerError("Login response did not include access_token")
    save_account_tokens(account, token_data)


def refresh_or_login_account_token(api_base: str, account: Account, use_system_proxy: bool = False) -> str:
    try:
        refresh_account_token(api_base, account, use_system_proxy=use_system_proxy)
        return "refresh_token"
    except Exception as refresh_exc:
        if not account.password:
            raise refresh_exc
        login_account_token(api_base, account, use_system_proxy=use_system_proxy)
        return "login"


def query_account_balance(api_base: str, account: Account, use_system_proxy: bool = False) -> float:
    try:
        user = request_json("GET", f"{api_base}/auth/me", token=account.access_token, use_system_proxy=use_system_proxy)
    except ManagerError as exc:
        if not should_refresh_token_error(exc):
            raise
        refresh_or_login_account_token(api_base, account, use_system_proxy=use_system_proxy)
        user = request_json("GET", f"{api_base}/auth/me", token=account.access_token, use_system_proxy=use_system_proxy)

    if isinstance(user, dict) and "data" in user and isinstance(user["data"], dict):
        user = user["data"]
    if not isinstance(user, dict) or "balance" not in user:
        raise ManagerError(f"Balance not found in /auth/me response: {user}")
    return float(user["balance"])


def sync_account_depleted_state(
    state_path: Path,
    account: Account,
    balance: float,
    *,
    dry_run: bool = False,
    log_path: Path | None = None,
) -> None:
    args = argparse.Namespace(state_path=state_path, dry_run=dry_run)
    with rotation_state_lock(state_path):
        state = load_rotation_state(state_path)
        if balance <= DEPLETED_BALANCE_THRESHOLD:
            records = dict(depleted_accounts(state))
            records[account_state_id(account)] = _account_depleted_record(account)
            state["depleted_accounts"] = records
            write_rotation_state(args, state, log_path, locked=True)
            log(f"Timed rotation marked depleted account: {format_account(account)} key=...{key_suffix(account.api_key)}", log_path)
            return

        records = dict(depleted_accounts(state))
        state_id = account_state_id(account)
        if state_id not in records:
            return
        records.pop(state_id, None)
        if records:
            state["depleted_accounts"] = records
        else:
            state.pop("depleted_accounts", None)
        write_rotation_state(args, state, log_path, locked=True)
        log(f"Timed rotation restored account: {format_account(account)} key=...{key_suffix(account.api_key)}", log_path)


def query_balances(
    api_base: str,
    accounts: list[Account],
    use_system_proxy: bool = False,
    *,
    state_path: Path | None = None,
    log_path: Path | None = None,
    dry_run: bool = False,
) -> None:
    for account in accounts:
        try:
            account.balance = query_account_balance(api_base, account, use_system_proxy=use_system_proxy)
            initial = read_account_initial_balance(account.path)
            if initial is None:
                initial = account.balance
            save_account_balance_cache(account, account.balance, initial, max(initial - account.balance, 0.0))
            if state_path is not None and not dry_run:
                sync_account_depleted_state(state_path, account, account.balance, dry_run=False, log_path=log_path)
            account.error = None
        except Exception as exc:
            account.balance = None
            account.error = str(exc)


def load_local_balances(accounts: list[Account]) -> None:
    for account in accounts:
        balance, source = read_account_cached_balance(account.path)
        account.balance = balance
        account.error = None if balance is not None else "no local balance cache"


def read_codex_api_key(auth_path: Path, key_field: str) -> str | None:
    if not auth_path.exists():
        return None
    data = json.loads(auth_path.read_text(encoding="utf-8"))
    value = data.get(key_field)
    return value if isinstance(value, str) and value else None


def backup_file(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    return backup


def write_codex_api_key(auth_path: Path, key_field: str, api_key: str, dry_run: bool) -> tuple[bool, Path | None]:
    data: dict[str, Any] = {}
    if auth_path.exists():
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    if data.get(key_field) == api_key:
        return False, None
    data[key_field] = api_key

    if dry_run:
        return True, None

    auth_path.parent.mkdir(parents=True, exist_ok=True)
    backup = backup_file(auth_path) if auth_path.exists() else None
    temp_path = auth_path.with_name(f"{auth_path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, auth_path)
    return True, backup

def normalize_base_url(url: str | None) -> str | None:
    if not url:
        return None
    return url.strip().rstrip("/").lower()


def read_config_base_url(config_path: Path) -> str | None:
    if not config_path.exists():
        return None
    text = config_path.read_text(encoding="utf-8")
    in_custom = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_custom = stripped == "[model_providers.custom]"
            continue
        if in_custom and stripped.startswith("base_url") and "=" in stripped:
            value = stripped.split("=", 1)[1].strip()
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            return value
    return None

def sync_codexpp_or_log(
    args: argparse.Namespace,
    *,
    api_key: str,
    base_url: str | None,
    log_path: Path | None,
) -> CodexPlusSyncResult | None:
    if not getattr(args, "sync_codexpp", True):
        return None
    if not base_url:
        log("Codex++ sync skipped because target base_url is empty.", log_path)
        return None

    try:
        result = sync_codexpp_if_present(
            api_key=api_key,
            base_url=base_url,
            settings_path=Path(getattr(args, "codexpp_settings_path", DEFAULT_CODEXPP_SETTINGS_PATH)).expanduser(),
            webui_path=Path(getattr(args, "codexpp_webui_path", DEFAULT_WEBUI_CUSTOM_PROVIDERS_PATH)).expanduser(),
            profile_name=str(getattr(args, "codexpp_profile_name", DEFAULT_CODEXPP_PROFILE_NAME)),
            auth_key_field=str(getattr(args, "auth_key_field", "OPENAI_API_KEY")),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
    except Exception as exc:
        log(f"[WARN] Codex++ sync failed: {exc}", log_path)
        return None

    if result.updated:
        prefix = "[DRY-RUN] Would sync" if getattr(args, "dry_run", False) else "Synced"
        log(f"{prefix} Codex++ API key only. {result.summary()}", log_path)
    elif result.skipped:
        log(f"Codex++ sync skipped. {result.summary()}", log_path)
    else:
        log("Codex++ target profile(s) already matched the selected API key.", log_path)
    return result


def account_for_key(accounts: list[Account], api_key: str | None) -> Account | None:
    if not api_key:
        return None
    for account in accounts:
        if account.api_key == api_key:
            return account
    return None


def read_codexpp_managed_key(args: argparse.Namespace, expected_base_url: str, log_path: Path | None) -> tuple[str | None, str | None, str | None]:
    if not getattr(args, "sync_codexpp", True):
        return None, None, None
    state = read_codexpp_profile_state(
        settings_path=Path(getattr(args, "codexpp_settings_path", DEFAULT_CODEXPP_SETTINGS_PATH)).expanduser(),
        profile_name=str(getattr(args, "codexpp_profile_name", DEFAULT_CODEXPP_PROFILE_NAME)),
        expected_base_url=expected_base_url,
        auth_key_field=str(getattr(args, "auth_key_field", "OPENAI_API_KEY")),
    )
    if not state.found:
        if state.skipped:
            log(f"Codex++ profile state skipped: {state.skipped}", log_path)
        return None, None, None
    if state.base_url:
        log(f"Codex++ profile '{state.label}' base_url is {state.base_url}", log_path)
    else:
        log(f"Codex++ profile '{state.label}' base_url was not found.", log_path)
    if not urls_match(state.base_url, expected_base_url):
        log(
            f"Codex++ profile URL is not target; skip profile key switching. "
            f"profile={state.label} current={state.base_url or 'empty'} target={expected_base_url}",
            log_path,
        )
        return None, state.base_url, state.label
    return state.api_key, state.base_url, state.label


def choose_replacement(accounts: list[Account], current: Account | None, threshold: float) -> Account | None:
    viable = [account for account in accounts if account.balance is not None and account.balance > threshold]
    if not viable:
        return None
    if current is None:
        return viable[0]

    start = accounts.index(current) if current in accounts else -1
    for offset in range(1, len(accounts) + 1):
        candidate = accounts[(start + offset) % len(accounts)]
        if candidate in viable:
            return candidate
    return viable[0]


def choose_next_account(accounts: list[Account], current: Account | None) -> Account | None:
    if not accounts:
        return None
    if current is None or current not in accounts:
        return accounts[0]
    if len(accounts) == 1:
        return current

    start = accounts.index(current)
    return accounts[(start + 1) % len(accounts)]


def iter_next_accounts(accounts: list[Account], current: Account | None):
    if not accounts:
        return
    start = accounts.index(current) if current in accounts else -1
    for offset in range(1, len(accounts) + 1):
        candidate = accounts[(start + offset) % len(accounts)]
        if current is not None and candidate.api_key == current.api_key:
            continue
        yield candidate


def format_account(account: Account) -> str:
    balance = "?" if account.balance is None else f"{account.balance:.4f}"
    return f"{account.email} balance={balance}"


def key_suffix(api_key: str | None) -> str:
    if not api_key:
        return "none"
    return api_key[-8:]


def rotation_state_path(args: argparse.Namespace) -> Path:
    value = getattr(args, "state_path", DEFAULT_STATE_PATH)
    return value if isinstance(value, Path) else Path(value)


def load_rotation_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_rotation_state(args: argparse.Namespace, account: Account, log_path: Path | None = None) -> None:
    path = rotation_state_path(args)
    with rotation_state_lock(path):
        state = load_rotation_state(path)
        state.update(
            {
                "last_rotated_at": time.time(),
                "last_rotated_at_text": now(),
                "last_email": account.email,
                "last_account_path": str(account.path.resolve()),
                "last_api_key_suffix": account.api_key[-8:],
            }
        )
        write_rotation_state(args, state, log_path, locked=True)


@contextlib.contextmanager
def rotation_state_lock(path: Path):
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def write_rotation_state(
    args: argparse.Namespace,
    state: dict[str, Any],
    log_path: Path | None = None,
    *,
    locked: bool = False,
) -> None:
    path = rotation_state_path(args)
    if getattr(args, "dry_run", False):
        log(f"[DRY-RUN] Would update timed rotation state: {path}", log_path)
        return
    if not locked:
        with rotation_state_lock(path):
            write_rotation_state(args, state, log_path, locked=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, path)


def account_state_id(account: Account) -> str:
    return f"{account.email}|{account.api_key[-8:]}"


def depleted_accounts(state: dict[str, Any]) -> dict[str, Any]:
    value = state.get("depleted_accounts")
    return value if isinstance(value, dict) else {}


def is_account_depleted(state: dict[str, Any], account: Account) -> bool:
    return account_state_id(account) in depleted_accounts(state)


def is_account_effectively_depleted(state: dict[str, Any], account: Account) -> bool:
    if not is_account_depleted(state, account):
        return False
    balance, _source = read_account_cached_balance(account.path)
    if balance is not None and balance > DEPLETED_BALANCE_THRESHOLD:
        return False
    return True


def mark_account_depleted(args: argparse.Namespace, state: dict[str, Any], account: Account, log_path: Path | None = None) -> None:
    path = rotation_state_path(args)
    with rotation_state_lock(path):
        latest_state = load_rotation_state(path)
        records = dict(depleted_accounts(latest_state))
        records[account_state_id(account)] = {
            "email": account.email,
            "account_path": str(account.path.resolve()),
            "api_key_suffix": account.api_key[-8:],
            "balance": account.balance,
            "marked_at": now(),
        }
        latest_state["depleted_accounts"] = records
        write_rotation_state(args, latest_state, log_path, locked=True)
    log(f"Timed rotation marked depleted account: {format_account(account)} key=...{key_suffix(account.api_key)}", log_path)


def state_matches_account(state: dict[str, Any], account: Account | None) -> bool:
    if not state or account is None:
        return False
    stored_path = str(state.get("last_account_path") or "")
    if stored_path:
        try:
            if Path(stored_path).resolve() == account.path.resolve():
                return True
        except OSError:
            pass
    return state.get("last_email") == account.email and state.get("last_api_key_suffix") == account.api_key[-8:]


def account_from_rotation_state(accounts: list[Account], state: dict[str, Any]) -> Account | None:
    for account in accounts:
        if state_matches_account(state, account):
            return account
    return None


def switch_to_account(
    args: argparse.Namespace,
    replacement: Account,
    target_base_url: str | None,
    log_path: Path | None,
    *,
    reason: str,
    previous: Account | None = None,
) -> int:
    wrote_targets: list[str] = []
    skipped_targets: list[str] = []

    local_proxy_mode = bool(getattr(args, "local_proxy_mode", False))
    if local_proxy_mode:
        skipped_targets.append("Codex++(local-proxy-mode)")
    else:
        sync_result = sync_codexpp_or_log(args, api_key=replacement.api_key, base_url=target_base_url, log_path=log_path)
        if getattr(args, "sync_codexpp", True):
            if sync_result:
                wrote_targets.extend(f"Codex++:{name}" for name in sync_result.updated_profiles)
                if sync_result.updated_webui:
                    wrote_targets.append("Codex++:webui-custom-providers")
                wrote_targets.extend(f"Codex++:{name}:already-current" for name in sync_result.matched_profiles)
                if sync_result.matched_webui:
                    wrote_targets.append("Codex++:webui-custom-providers:already-current")
                if not (sync_result.updated_profiles or sync_result.updated_webui or sync_result.matched_profiles or sync_result.matched_webui):
                    skipped_targets.append("Codex++")
            else:
                skipped_targets.append("Codex++")

    backup: Path | None = None
    changed_auth = False
    if local_proxy_mode:
        skipped_targets.append("Codex auth.json(local-proxy-mode)")
    elif getattr(args, "take_over", False):
        current_base_url = read_config_base_url(args.config_path)
        if urls_match(current_base_url, target_base_url):
            changed_auth, backup = write_codex_api_key(args.auth_path, args.auth_key_field, replacement.api_key, args.dry_run)
            wrote_targets.append("Codex auth.json" if changed_auth else "Codex auth.json:already-current")
        else:
            skipped_targets.append(f"Codex auth.json(url={current_base_url or 'empty'})")
            log(
                f"Current Codex URL is not target; skip writing auth.json. "
                f"current={current_base_url or 'empty'} target={target_base_url}",
                log_path,
            )
    else:
        skipped_targets.append("Codex auth.json(disabled)")

    if local_proxy_mode:
        save_rotation_state(args, replacement, log_path)
        wrote_targets.append("local-proxy-state")

    previous_text = f"{previous.email} key=...{key_suffix(previous.api_key)}" if previous else "unmanaged/empty"
    replacement_text = f"{replacement.email} key=...{key_suffix(replacement.api_key)}"
    target_text = "; targets=" + ",".join(wrote_targets or ["none"])
    if skipped_targets:
        target_text += "; skipped=" + ",".join(skipped_targets)
    changed_any = any(":already-current" not in target for target in wrote_targets)
    if args.dry_run:
        action = "【切换API Key】" if changed_any else "【切换API Key】未写入"
        log(f"[DRY-RUN] {action}{previous_text} -> {replacement_text}; {format_account(replacement)}; reason={reason}{target_text}", log_path)
    else:
        backup_text = f"; backup={backup}" if backup else ""
        if changed_any:
            log(f"【切换API Key】已切换: {previous_text} -> {replacement_text}; {format_account(replacement)}; reason={reason}{target_text}{backup_text}", log_path)
        else:
            log(f"【切换API Key】未写入: {previous_text} -> {replacement_text}; {format_account(replacement)}; reason={reason}{target_text}{backup_text}", log_path)
    return 0


def run_time_rotation_once(
    args: argparse.Namespace,
    accounts: list[Account],
    current: Account | None,
    target_base_url: str | None,
    log_path: Path | None,
) -> int:
    api_base = args.api_base.rstrip("/")
    rotate_seconds = max(int(getattr(args, "rotate_seconds", DEFAULT_ROTATE_SECONDS)), 1)
    state_path = rotation_state_path(args)
    state = load_rotation_state(state_path)
    now_ts = time.time()

    if current is not None and not state_matches_account(state, current):
        if not getattr(args, "local_proxy_mode", False):
            sync_codexpp_or_log(args, api_key=current.api_key, base_url=target_base_url, log_path=log_path)
        save_rotation_state(args, current, log_path)
        log(f"Timed rotation initialized from current key. Next switch after {rotate_seconds}s.", log_path)
        return 0

    last_rotated_at = float(state.get("last_rotated_at") or 0)
    elapsed = now_ts - last_rotated_at if last_rotated_at else rotate_seconds
    if current is not None and elapsed < rotate_seconds:
        if not getattr(args, "local_proxy_mode", False):
            sync_codexpp_or_log(args, api_key=current.api_key, base_url=target_base_url, log_path=log_path)
        remaining = max(int(rotate_seconds - elapsed), 0)
        log(f"No switch needed. Timed rotation mode: {remaining}s until next switch.", log_path)
        return 0

    replacement = None
    skipped_depleted = 0
    for candidate in iter_next_accounts(accounts, current):
        if is_account_effectively_depleted(state, candidate):
            skipped_depleted += 1
            continue
        if skipped_depleted:
            log(f"Timed rotation skipped {skipped_depleted} depleted account(s) before checking next candidate.", log_path)
            skipped_depleted = 0
        try:
            candidate.balance = query_account_balance(api_base, candidate, use_system_proxy=args.use_system_proxy)
            candidate.error = None
            log(f"Timed rotation candidate checked: {format_account(candidate)} key=...{key_suffix(candidate.api_key)}", log_path)

        except Exception as exc:
            candidate.balance = None
            candidate.error = str(exc)
            log(f"[WARN] Timed rotation candidate balance query failed: {candidate.email}: {exc}", log_path)
            continue
        if candidate.balance <= DEPLETED_BALANCE_THRESHOLD:
            mark_account_depleted(args, state, candidate, log_path)
            state = load_rotation_state(state_path)
            continue
        replacement = candidate
        break

    if replacement is None:
        if skipped_depleted:
            log(f"Timed rotation skipped {skipped_depleted} depleted account(s); no unchecked account left.", log_path)
        log("[ERROR] No non-depleted managed account is available for timed rotation.", log_path)
        return 2
    if current and current.api_key == replacement.api_key:
        log(f"[ERROR] Timed rotation needs at least 2 managed accounts; only current is available: {format_account(current)}", log_path)
        return 2

    code = switch_to_account(
        args,
        replacement,
        target_base_url,
        log_path,
        reason=f"timed rotation interval reached ({rotate_seconds}s)",
        previous=current,
    )
    if code == 0:
        save_rotation_state(args, replacement, log_path)
    return code


def run_once(args: argparse.Namespace, log_path: Path | None = None) -> int:
    accounts = load_accounts(args.accounts)
    api_base = args.api_base.rstrip("/")
    target_base_url = args.managed_base_url.rstrip("/")
    switch_mode = getattr(args, "switch_mode", "balance")
    if switch_mode == "balance":
        query_balances(
            api_base,
            accounts,
            use_system_proxy=args.use_system_proxy,
            state_path=rotation_state_path(args),
            log_path=log_path,
            dry_run=getattr(args, "dry_run", False),
        )

        for account in accounts:
            if account.error:
                log(f"[WARN] {account.email}: balance query failed: {account.error}", log_path)
            else:
                log(f"[OK] {format_account(account)}", log_path)
    elif switch_mode == "local-balance":
        load_local_balances(accounts)
        for account in accounts:
            if account.balance is None:
                log(f"[WARN] {account.email}: local balance unavailable; skipped for local-balance mode.", log_path)
            else:
                log(f"[OK] local {format_account(account)}", log_path)

    current_base_url = read_config_base_url(args.config_path)
    if current_base_url:
        log(f"Current Codex base_url is {current_base_url}", log_path)
    else:
        log("Current Codex base_url was not found.", log_path)

    current = None
    current_source = "rotation state"
    if getattr(args, "local_proxy_mode", False):
        current = account_from_rotation_state(accounts, load_rotation_state(rotation_state_path(args)))
        if current:
            log(f"Current local-proxy state belongs to {current.email} key=...{key_suffix(current.api_key)}", log_path)
        else:
            log("Current local-proxy state is empty or not in the account pool.", log_path)
    else:
        current_key, profile_base_url, profile_label = read_codexpp_managed_key(args, target_base_url, log_path)
        current_source = f"Codex++:{profile_label or getattr(args, 'codexpp_profile_name', DEFAULT_CODEXPP_PROFILE_NAME)}"
        if not current_key:
            current_key = read_codex_api_key(args.auth_path, args.auth_key_field)
            current_source = "Codex auth.json"
            if not urls_match(current_base_url, target_base_url):
                log(
                    f"Current Codex URL is not target; auth.json key will not be used for switching decisions. "
                    f"current={current_base_url or 'empty'} target={target_base_url}",
                    log_path,
                )
                current_key = None
        current = account_for_key(accounts, current_key)

    if current:
        if switch_mode == "time":
            log(f"Current managed key from {current_source} belongs to {current.email} key=...{key_suffix(current.api_key)}", log_path)
        else:
            log(f"Current managed key from {current_source} belongs to {format_account(current)}", log_path)
    else:
        log("Current managed key is not in the account pool or no target URL is active.", log_path)

    if switch_mode == "time":
        return run_time_rotation_once(args, accounts, current, target_base_url, log_path)

    should_switch = current is None or current.balance is None or current.balance <= args.threshold
    if not should_switch:
        if not getattr(args, "local_proxy_mode", False):
            sync_codexpp_or_log(args, api_key=current.api_key, base_url=target_base_url, log_path=log_path)
        else:
            save_rotation_state(args, current, log_path)
        log(f"No switch needed. Threshold={args.threshold:.4f}", log_path)
        return 0

    replacement = choose_replacement(accounts, current, args.threshold)
    if replacement is None:
        log(f"[ERROR] No managed account has balance above {args.threshold:.4f}; keeping current key.", log_path)
        return 2
    if current and current.api_key == replacement.api_key:
        log(f"[ERROR] Only current account is available but balance is not above threshold: {format_account(current)}", log_path)
        return 2

    return switch_to_account(
        args,
        replacement,
        target_base_url,
        log_path,
        reason=f"balance <= threshold ({args.threshold:.4f})",
        previous=current,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="监控 xhhai 账号余额，并在余额低时自动切换 Codex 使用的 API key。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python xhhai_key_manager.py --dry-run\n"
            "  python xhhai_key_manager.py --accounts \"accounts\\xhhai_codex_account*.json\" --threshold 0.5\n"
            "  python xhhai_key_manager.py --watch --interval 1\n"
            "  python xhhai_key_manager.py --watch --switch-mode time --rotate-seconds 3\n\n"
            "说明:\n"
            "  默认读取当前目录的 xhhai_codex_account*.json。\n"
            "  --switch-mode balance 表示余额低于阈值时切换；--switch-mode time 表示按固定时间轮换 key。\n"
            "  默认只同步 Codex++ 的批量注册 profile，且只修改 API key，不修改 URL。\n"
            "  --local-proxy-mode 表示只更新本地轮换状态文件，不修改 Codex++ 或 Codex 的 API key。\n"
            "  --take-over 只是在当前 Codex base_url 已经是 xhhai 目标 URL 时，额外写 auth.json。"
        ),
    )
    parser.add_argument("--accounts", action="append", default=[], help="账号 JSON 文件或通配符；可重复传。默认: xhhai_codex_account*.json。")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="xhhai 接口基础地址。默认: %(default)s。")
    parser.add_argument("--managed-base-url", default=DEFAULT_MANAGED_BASE_URL, help="本管理器认为可接管的 Codex base_url。默认: %(default)s。")
    parser.add_argument("--auth-path", type=Path, default=DEFAULT_AUTH_PATH, help="Codex auth.json 路径。默认: %(default)s。")
    parser.add_argument("--config-path", type=Path, default=DEFAULT_CONFIG_PATH, help="Codex config.toml 路径。默认: %(default)s。")
    parser.add_argument("--auth-key-field", default="OPENAI_API_KEY", help="auth.json 里保存 key 的字段名。默认: %(default)s。")
    parser.add_argument("--switch-mode", choices=("balance", "time", "local-balance"), default="balance", help="切换模式：balance=请求接口查余额；time=按固定时间轮换 key；local-balance=只用本地余额缓存判断，不请求余额接口。默认: %(default)s。")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="余额低于或等于该值时切换。默认: %(default)s。")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS, help="--watch 模式下的检查间隔，单位秒。默认: %(default)s。")
    parser.add_argument("--rotate-seconds", type=int, default=DEFAULT_ROTATE_SECONDS, help="--switch-mode time 时，每隔多少秒切换到下一个 key。默认: %(default)s。")
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH, help="定时轮换状态文件路径，用于记住上次切换时间。默认: %(default)s。")
    parser.add_argument("--watch", action="store_true", help="持续运行并按 --interval 循环检查。")
    parser.add_argument("--dry-run", action="store_true", help="只检查余额和计划，不写入 Codex 文件。")
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH, help="运行日志路径。默认: %(default)s。")
    parser.add_argument("--use-system-proxy", action="store_true", help="使用 Windows/环境变量里的系统代理或 VPN HTTP 代理路线。")
    parser.add_argument("--take-over", action="store_true", help="当前 Codex base_url 已经是托管地址时，额外写入 auth.json 的 API key；不会修改 URL。")
    parser.add_argument("--local-proxy-mode", action="store_true", help="本地中转轮换模式：只写轮换状态文件，不修改 Codex++ 或 Codex 的 API key。")
    parser.add_argument("--no-sync-codexpp", dest="sync_codexpp", action="store_false", default=True, help="不要同步 Codex++ 的批量注册 API key。同步时只改 key，不改 URL。")
    parser.add_argument("--codexpp-settings-path", type=Path, default=DEFAULT_CODEXPP_SETTINGS_PATH, help="Codex++ settings.json 路径；存在才会修改。默认: %(default)s。")
    parser.add_argument("--codexpp-webui-path", type=Path, default=DEFAULT_WEBUI_CUSTOM_PROVIDERS_PATH, help="Codex++ webui-custom-providers.json 路径；存在才会修改。默认: %(default)s。")
    parser.add_argument("--codexpp-profile-name", default=DEFAULT_CODEXPP_PROFILE_NAME, help="要同步的 Codex++ relay profile 名称。默认: %(default)s。")
    args = parser.parse_args()
    if not args.accounts:
        args.accounts = [DEFAULT_ACCOUNT_GLOB]
    return args


def main() -> int:
    args = parse_args()
    log_path = args.log_path if args.log_path else None
    while True:
        try:
            code = run_once(args, log_path)
        except Exception as exc:
            code = 1
            log(f"[ERROR] {exc}", log_path)
        if not args.watch:
            return code
        time.sleep(max(args.interval, 1))


if __name__ == "__main__":
    raise SystemExit(main())
