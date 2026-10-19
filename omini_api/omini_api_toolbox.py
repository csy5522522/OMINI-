#!/usr/bin/env python3
"""Omini API Toolbox GUI for account, key, proxy, and rotation management."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from omini_api import xhhai_batch_create_codex_keys as batch_create
from omini_api import xhhai_ccswitch_sync as ccswitch_sync
from omini_api import xhhai_local_proxy as local_proxy
from omini_api import omini_account_service as account_service
from omini_api import omini_email_receiver as email_receiver
from omini_api import omini_site_discovery as site_discovery
from omini_api import omini_sites as site_profiles
from omini_api.xhhai_codexpp_sync import (
    DEFAULT_CODEXPP_SETTINGS_PATH,
    DEFAULT_LOCAL_PROXY_API_KEY,
    DEFAULT_LOCAL_PROXY_BASE_URL,
    DEFAULT_LOCAL_PROXY_PROFILE_NAME,
    DEFAULT_PROFILE_NAME as DEFAULT_CODEXPP_PROFILE_NAME,
    DEFAULT_WEBUI_CUSTOM_PROVIDERS_PATH,
    install_local_proxy_profile,
)
from omini_api import xhhai_key_manager as key_manager
from omini_api import xhhai_switch_account_group as group_switch
from omini_api.xhhai_create_codex_key import DEFAULT_GROUP_NAME, DEFAULT_KEY_NAME
from omini_api.xhhai_register import DEFAULT_API_BASE
from omini_api.xhhai_file_utils import write_json_atomic


APP_TITLE = "Omini API 工具箱"
DEFAULT_PROXY_HOST = "127.0.0.1"
DEFAULT_PROXY_PORT = "7897"
SETTINGS_FILE_NAME = "omini_api_toolbox_settings.json"
SETTINGS_VERSION = 2


def default_workspace_dir() -> Path:
    override = os.environ.get("OMINI_API_TOOLBOX_HOME") or os.environ.get("XHHAI_WORKSPACE")
    if override:
        return Path(override).expanduser().resolve()
    from omini_api.runtime import default_workspace
    return default_workspace()


WORKSPACE_DIR = default_workspace_dir()
ACCOUNT_FILE_PATTERN = "xhhai_codex_account*.json"
CLAUDE_ACCOUNT_FILE_PATTERN = "xhhai_*account*.json"
POOL_ALL = "全部 Codex 账号"
LEGACY_POOL_NAMES = {"全部账号（含旧目录）", "旧目录"}
ROUTE_KEYS = ("A", "B", "C", "D")
ROUTE_LABELS = {
    "A": "线路A",
    "B": "线路B",
    "C": "线路C",
    "D": "线路D",
}
DEFAULT_ROUTE_GROUPS = {
    "A": "GPTPro分组-线路A",
    "B": "GPT Pro分组-低延迟稳定-线路B",
    "C": "GPT Pro分组-备用-线速C",
    "D": "GPT Pro分组-备用-线路D",
}
CLAUDE_ROUTE_KEYS = ("A", "B")
CLAUDE_ROUTE_LABELS = {
    "A": "Claude线路A",
    "B": "Claude线路B",
}
DEFAULT_CLAUDE_ROUTE_GROUPS = {
    "A": "Claude Code-Max-线路A(支持最新模型)",
    "B": "Claude Code-线路B(限时优惠)",
}
CLAUDE_POOL_ALL = "Claude全部账号"
DEFAULT_CLAUDE_PROXY_PORT = 8788
DEFAULT_CLAUDE_AUTH_TOKEN = "xhhai-local-claude"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-5"
EMAIL_MODE_DIRECT_LABEL = "方案一（自定义后缀）"
EMAIL_MODE_RECEIVER_LABEL = "方案二（临时邮箱接码）"


def configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


configure_utf8_stdio()


def app_dir() -> Path:
    return WORKSPACE_DIR


def settings_path() -> Path:
    return app_dir() / SETTINGS_FILE_NAME


def email_receiver_config_path() -> Path:
    return app_dir() / "omini_email_receiver.json"


def worker_base_command() -> list[str]:
    from omini_api.runtime import base_command
    return base_command()


def accounts_root() -> Path:
    return app_dir() / "accounts"


def route_dir(route_key: str) -> Path:
    return accounts_root() / f"route_{route_key.upper()}"


def claude_route_dir(route_key: str) -> Path:
    return accounts_root() / f"claude_{route_key.upper()}"


def route_account_pattern(route_key: str) -> str:
    return str(route_dir(route_key) / ACCOUNT_FILE_PATTERN)


def claude_route_account_pattern(route_key: str) -> str:
    return str(claude_route_dir(route_key) / CLAUDE_ACCOUNT_FILE_PATTERN)


def all_account_patterns() -> list[str]:
    return [
        str(accounts_root() / "codex" / ACCOUNT_FILE_PATTERN),
        *[route_account_pattern(key) for key in ROUTE_KEYS],
        str(accounts_root() / ACCOUNT_FILE_PATTERN),
        str(app_dir() / ACCOUNT_FILE_PATTERN),
    ]


def all_claude_account_patterns() -> list[str]:
    return [claude_route_account_pattern(key) for key in CLAUDE_ROUTE_KEYS]


def all_known_account_patterns() -> list[str]:
    return [*all_account_patterns(), *all_claude_account_patterns()]


def route_pool_labels() -> list[str]:
    return [ROUTE_LABELS[key] for key in ROUTE_KEYS]


def claude_pool_labels() -> list[str]:
    return [CLAUDE_ROUTE_LABELS[key] for key in CLAUDE_ROUTE_KEYS]


def coerce_account_patterns(value: Any, default: list[str]) -> list[str]:
    raw_items = value if isinstance(value, (list, tuple)) else [value]
    patterns: list[str] = []
    for item in raw_items:
        if not item:
            continue
        text = str(item)
        for part in text.split(";"):
            part = part.strip()
            if part:
                patterns.append(part)
    return patterns or list(default)


def bool_flag(args: list[str], enabled: bool, flag: str) -> None:
    if enabled:
        args.append(flag)


def kv_value(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def account_glob_has_files(pattern: str) -> bool:
    return bool(pattern and glob.glob(pattern))


def is_token_error_text(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "token",
        "jwt",
        "expired",
        "unauthorized",
        "not authenticated",
        "authentication",
        "登录已过期",
        "令牌过期",
        "token过期",
    )
    return any(marker in lowered for marker in markers)


def run_batch_worker(ns: argparse.Namespace) -> int:
    profile: dict[str, Any] = {"name": getattr(ns, "site_id", "") or "未选择站点", "api_base": ""}
    proxy_status = f"{getattr(ns, 'proxy_mode', '未知')}/预检中"
    try:
        store = site_profiles.load_site_store(app_dir() / "omini_sites.json")
        site_id = getattr(ns, "site_id", "") or site_profiles.default_site(store)["id"]
        profile = site_profiles.site_by_id(store, site_id)
        proxy_mode = getattr(ns, "proxy_mode", "强制代理" if getattr(ns, "use_system_proxy", False) else "直连")
        proxy_status = f"{proxy_mode}/预检失败"
        decision = site_profiles.resolve_proxy(proxy_mode, ns.proxy_host, ns.proxy_port)
        proxy_status = f"{decision.mode}/{'代理' if decision.use_proxy else '直连'}"
        if decision.note:
            print(f"[NETWORK] {decision.note}", flush=True)
        if ns.worker == "batch" or ns.group_name:
            print("[DEPRECATED] batch/分组参数仅为兼容保留；当前入口执行完整自动开户流程。", flush=True)
        default_min, _default_max = batch_create.parse_concurrency_band(ns.default_band)
        concurrency = ns.initial_concurrency or (ns.paced_batch_size if ns.batch_mode == "paced" else default_min)
        complete, partial, failed = account_service.register_batch(
            profile,
            count=ns.count,
            email_domain=ns.email_domain,
            output_dir=Path(ns.output_dir),
            account_kind=getattr(ns, "account_kind", "codex"),
            route=getattr(ns, "route", ""),
            use_proxy=decision.use_proxy,
            proxy_url=decision.proxy_url,
            overwrite=ns.overwrite,
            concurrency=concurrency,
            emails_file=Path(ns.emails_file) if ns.emails_file else None,
            password=account_service.FIXED_ACCOUNT_PASSWORD,
            dry_run=ns.dry_run,
            email_mode=ns.email_mode,
            key_name=ns.key_name or ("claude" if getattr(ns, "account_kind", "codex") == "claude" else "codex"),
            receiver_config=email_receiver.load_receiver_config(Path(ns.receiver_config)),
        )
        return 0 if partial == 0 and failed == 0 else 1
    except Exception as exc:
        print(
            f"[ERROR] site={profile.get('name')} api={profile.get('api_base')} "
            f"proxy={proxy_status} message={account_service.redact_log_text(exc)[:500]}",
            flush=True,
        )
        return 1


def _worker_proxy_decision(ns: argparse.Namespace) -> site_profiles.ProxyDecision:
    decision = site_profiles.resolve_proxy(ns.proxy_mode, ns.proxy_host, ns.proxy_port)
    if decision.note:
        print(f"[NETWORK] {decision.note}", flush=True)
    return decision


def run_test_site_worker(ns: argparse.Namespace) -> int:
    try:
        store = site_profiles.load_site_store(app_dir() / "omini_sites.json")
        profile = site_profiles.site_by_id(store, ns.site_id)
        decision = _worker_proxy_decision(ns)
        print(site_profiles.test_site(profile, use_proxy=decision.use_proxy, proxy_url=decision.proxy_url), flush=True)
        return 0
    except Exception as exc:
        print(f"[ERROR] {account_service.redact_log_text(exc)[:500]}", flush=True)
        return 1


def run_test_email_receiver_worker(ns: argparse.Namespace) -> int:
    try:
        decision = _worker_proxy_decision(ns)
        config = email_receiver.load_receiver_config(Path(ns.receiver_config))
        print(email_receiver.test_connection(config, use_proxy=decision.use_proxy, proxy_url=decision.proxy_url), flush=True)
        return 0
    except Exception as exc:
        print(f"[ERROR] {account_service.redact_log_text(exc)[:500]}", flush=True)
        return 1


def run_discover_sites_worker(ns: argparse.Namespace) -> int:
    try:
        stop_path = Path(ns.stop_file)
        if stop_path.exists():
            stop_path.unlink()
        decision = _worker_proxy_decision(ns)
        summary = site_discovery.discover_sites(
            count=ns.count,
            probe_concurrency=ns.probe_concurrency,
            provision_concurrency=ns.provision_concurrency,
            email_domain=ns.email_domain,
            email_mode=ns.email_mode,
            key_name=ns.key_name,
            receiver_config=email_receiver.load_receiver_config(Path(ns.receiver_config)),
            accounts_root=accounts_root(),
            site_store_path=app_dir() / "omini_sites.json",
            log_path=app_dir() / "omini_site_discovery.jsonl",
            use_proxy=decision.use_proxy,
            proxy_url=decision.proxy_url,
            seed=ns.seed,
            stop_requested=stop_path.exists,
        )
        return 0
    except Exception as exc:
        print(f"[ERROR] {account_service.redact_log_text(exc)[:500]}", flush=True)
        return 1


def run_site_groups_worker(ns: argparse.Namespace) -> int:
    try:
        store = site_profiles.load_site_store(app_dir() / "omini_sites.json")
        profile = site_profiles.site_by_id(store, ns.site_id)
        decision = _worker_proxy_decision(ns)
        account_paths = [Path(item) for group in ns.account for item in group]
        groups = account_service.common_groups(profile, account_paths, use_proxy=decision.use_proxy, proxy_url=decision.proxy_url)
        for group in groups:
            print(f"[GROUP] id={group['id']} name={group['name']}", flush=True)
        print(f"[SUMMARY] operation=groups total={len(groups)}", flush=True)
        return 0
    except Exception as exc:
        print(f"[ERROR] {account_service.redact_log_text(exc)[:500]}", flush=True)
        return 1


def run_add_keys_worker(ns: argparse.Namespace) -> int:
    try:
        store = site_profiles.load_site_store(app_dir() / "omini_sites.json")
        profile = site_profiles.site_by_id(store, ns.site_id)
        decision = _worker_proxy_decision(ns)
        account_paths = [Path(item) for group in ns.account for item in group]
        group_name = ns.group_name
        if not group_name:
            groups = account_service.common_groups(
                profile, account_paths, use_proxy=decision.use_proxy, proxy_url=decision.proxy_url
            )
            matched = next((item for item in groups if str(item.get("id")) == str(ns.group_id)), None)
            if matched is None:
                raise site_profiles.SiteError(f"所选账号的共同分组中不存在 ID={ns.group_id}。")
            group_name = str(matched["name"])
        _ok, _skipped, failed = account_service.add_keys(
            profile,
            account_paths,
            group_id=ns.group_id,
            group_name=group_name,
            key_name=ns.key_name,
            include_existing=ns.include_existing,
            use_proxy=decision.use_proxy,
            proxy_url=decision.proxy_url,
            delay_seconds=ns.delay_seconds,
        )
        return 0 if failed == 0 else 1
    except Exception as exc:
        print(f"[ERROR] {account_service.redact_log_text(exc)[:500]}", flush=True)
        return 1


def run_switch_worker(ns: argparse.Namespace) -> int:
    patterns = coerce_account_patterns(ns.accounts, group_switch.DEFAULT_ACCOUNT_PATTERNS)
    paths = group_switch.resolve_account_paths(patterns)
    if not paths:
        print("没有找到账号 JSON。")
        return 1

    ok_count, fail_count = group_switch.switch_many(
        paths,
        api_base=ns.api_base.rstrip("/"),
        group_name=ns.group_name,
        key_name=ns.key_name,
        use_system_proxy=ns.use_system_proxy,
        refresh_first=not ns.no_refresh,
        dry_run=ns.dry_run,
        no_backup=ns.no_backup,
        batch_size=ns.batch_size,
        delay_seconds=ns.delay_seconds,
        source_group_name=ns.source_group_name,
        limit=ns.limit,
        target_dir=Path(ns.target_dir) if ns.target_dir else None,
        skip_depleted=not ns.include_depleted,
    )
    print(f"完成。成功={ok_count}, 失败={fail_count}")
    return 0 if fail_count == 0 else 1


def run_list_groups_worker(ns: argparse.Namespace) -> int:
    patterns = coerce_account_patterns(ns.accounts, all_known_account_patterns())
    paths = group_switch.resolve_account_paths(patterns)
    if not paths:
        print("没有找到账号 JSON。")
        return 1

    last_error: Exception | None = None
    loaded: tuple[Path, dict[str, Any], dict[str, Any], list[dict[str, Any]], Path | None, str] | None = None
    for path in paths:
        try:
            data, registration, groups, token_backup, token_source = group_switch.load_groups_for_account(
                path,
                api_base=ns.api_base.rstrip("/"),
                use_system_proxy=ns.use_system_proxy,
                refresh_first=not ns.no_refresh,
                no_backup=False,
                persist_tokens=True,
            )
            loaded = (path, data, registration, groups, token_backup, token_source)
            break
        except Exception as exc:
            last_error = exc
            print(f"[SKIP] 账号样本不可用: {path}: {exc}")
    if loaded is None:
        print(f"没有找到可用于刷新分组的账号。最后错误: {last_error}")
        return 1

    path, _data, _registration, groups, token_backup, token_source = loaded
    backup_text = f" token_backup={token_backup}" if token_backup else ""
    print(f"账号样本: {path} token={token_source}{backup_text}")
    for group in groups:
        name = str(group.get("name") or "").strip()
        if not name:
            continue
        group_id = group.get("id", "")
        model = group.get("model") or group.get("model_name") or group.get("type") or ""
        multiplier = group.get("multiplier") or group.get("rate") or group.get("倍率") or ""
        extra = []
        if model:
            extra.append(f"model={model}")
        if multiplier:
            extra.append(f"multiplier={multiplier}")
        suffix = " " + " ".join(extra) if extra else ""
        print(f"[GROUP] id={group_id} name={name}{suffix}")
    print(f"完成。可用分组={len(groups)}")
    return 0


def account_initial_balance(path: Path) -> float | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    value = data.get("balance_initial") if isinstance(data, dict) else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def run_account_balances_worker(ns: argparse.Namespace) -> int:
    patterns = coerce_account_patterns(ns.accounts, all_account_patterns())
    paths = group_switch.resolve_account_paths(patterns)
    if not paths:
        print("没有找到账号 JSON。")
        return 1

    store = site_profiles.load_site_store(app_dir() / "omini_sites.json")
    decision = site_profiles.resolve_proxy(
        getattr(ns, "proxy_mode", "强制代理" if ns.use_system_proxy else "直连"),
        getattr(ns, "proxy_host", DEFAULT_PROXY_HOST),
        int(getattr(ns, "proxy_port", DEFAULT_PROXY_PORT)),
    )
    if decision.note:
        print(f"[NETWORK] {decision.note}", flush=True)
    accounts: list[tuple[Path, dict[str, Any]]] = []
    load_fail = 0
    for path in paths:
        try:
            accounts.append((path, account_service.read_account(path)))
        except Exception as exc:
            load_fail += 1
            print(
                f"[BALANCE] ok=false email={kv_value(path.stem)} path={kv_value(path)} "
                f"error={kv_value(account_service.redact_log_text(exc))}"
            )

    total_initial = 0.0
    total_remaining = 0.0
    total_consumed = 0.0
    usable_count = 0
    query_fail = load_fail
    refreshed = 0

    for index, (path, data) in enumerate(accounts, start=1):
        email = str(data.get("email") or path.stem)
        try:
            if account_service.needs_registration_retry(data):
                raise site_profiles.SiteError("账号尚未完成注册，请先点击“重试自动开户”。")
            site_id = account_service.account_site_id(data)
            profile = site_profiles.site_by_id(store, site_id)
            balance, token_refreshed = account_service.query_balance(
                profile, path, use_proxy=decision.use_proxy, proxy_url=decision.proxy_url
            )
            stored_initial = key_manager.read_account_initial_balance(path)
            initial = stored_initial if stored_initial is not None else balance
            if token_refreshed:
                refreshed += 1
            consumed = max(initial - balance, 0.0)
            try:
                legacy_account = key_manager.load_account(path)
                legacy_account.balance = balance
                key_manager.sync_account_depleted_state(
                    Path(ns.state_path), legacy_account, balance, dry_run=False, log_path=Path(ns.log_path)
                )
            except Exception:
                pass
            total_initial += initial
            total_remaining += balance
            total_consumed += consumed
            if balance > ns.available_threshold:
                usable_count += 1
            print(
                f"[BALANCE] ok=true index={index} email={kv_value(email)} "
                f"balance={balance:.6f} initial={initial:.6f} consumed={consumed:.6f} "
                f"path={kv_value(path)}"
            )
        except Exception as exc:
            initial = key_manager.read_account_initial_balance(path)
            if initial is None:
                initial = 0.0
            query_fail += 1
            total_initial += initial
            total_consumed += max(initial, 0.0)
            print(
                f"[BALANCE] ok=false index={index} email={kv_value(email)} "
                f"initial={initial:.6f} path={kv_value(path)} "
                f"error={kv_value(account_service.redact_log_text(exc))}"
            )
        if ns.query_interval_seconds > 0 and index < len(accounts):
            print(f"[BALANCE_WAIT] seconds={ns.query_interval_seconds}", flush=True)
            time.sleep(ns.query_interval_seconds)

    print(
        f"[SUMMARY] total={len(paths)} loaded={len(accounts)} usable={usable_count} "
        f"failed={query_fail} remaining={total_remaining:.6f} consumed={total_consumed:.6f} "
        f"initial={total_initial:.6f} refreshed_tokens={refreshed}"
    )
    return 0 if query_fail == 0 else 1


def key_manager_namespace(ns: argparse.Namespace) -> argparse.Namespace:
    accounts = coerce_account_patterns(ns.accounts, all_account_patterns())
    return argparse.Namespace(
        accounts=accounts,
        api_base=ns.api_base,
        managed_base_url=ns.managed_base_url,
        auth_path=Path(ns.auth_path).expanduser(),
        config_path=Path(ns.config_path).expanduser(),
        auth_key_field=ns.auth_key_field,
        switch_mode=ns.switch_mode,
        threshold=ns.threshold,
        interval=ns.interval,
        rotate_seconds=ns.rotate_seconds,
        state_path=Path(ns.state_path),
        watch=True,
        dry_run=ns.dry_run,
        log_path=Path(ns.log_path),
        use_system_proxy=ns.use_system_proxy,
        take_over=ns.take_over,
        local_proxy_mode=ns.local_proxy_mode,
        sync_codexpp=not ns.no_sync_codexpp,
        codexpp_settings_path=Path(ns.codexpp_settings_path),
        codexpp_webui_path=Path(ns.codexpp_webui_path),
        codexpp_profile_name=ns.codexpp_profile_name,
    )


def run_monitor_worker(ns: argparse.Namespace) -> int:
    args = key_manager_namespace(ns)
    log_path = args.log_path if args.log_path else None
    log_pos = log_path.stat().st_size if log_path and log_path.exists() else 0
    if args.switch_mode == "time":
        print(f"余额监控已启动：定时轮换 key，每 {args.rotate_seconds}s 切换一次。按停止按钮可结束。")
    elif args.switch_mode == "local-balance":
        print(f"余额监控已启动：本地余额切换，只读账号 JSON 缓存，不发余额请求；检测间隔 {args.interval}s。按停止按钮可结束。")
    else:
        print(f"余额监控已启动：余额低于阈值时切换，检测间隔 {args.interval}s。按停止按钮可结束。")
    while True:
        try:
            code = key_manager.run_once(args, log_path)
        except Exception as exc:
            message = str(exc)
            if "No account files found" in message:
                print(f"等待账号 JSON 生成，{args.interval}s 后继续。", flush=True)
                time.sleep(max(args.interval, 1))
                continue
            code = 1
            key_manager.log(f"[ERROR] {exc}", log_path)
        if log_path and log_path.exists():
            try:
                with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(log_pos)
                    text = fh.read()
                    log_pos = fh.tell()
                lines = text.splitlines()
                important = [
                    line for line in lines
                    if "【切换API Key】" in line
                    or "No switch needed" in line
                    or "Current managed key" in line
                    or "Current Codex URL is not target" in line
                    or "Current Codex base_url" in line
                    or "Codex++ profile URL is not target" in line
                    or "Codex++ profile '" in line
                    or "Codex++ sync skipped" in line
                    or "Codex++ API key only" in line
                    or "Timed rotation initialized" in line
                    or "Timed rotation mode" in line
                    or "Timed rotation candidate" in line
                    or ("Timed rotation skipped " in line and "depleted account:" not in line)
                    or "Timed rotation marked depleted" in line
                    or "[local-proxy]" in line
                    or "[ERROR]" in line
                ]
                for line in important:
                    print(f"监控日志: {line}", flush=True)
            except Exception as exc:
                print(f"监控日志读取失败: {exc}", flush=True)
        print(f"本轮检测完成，退出码={code}，{args.interval}s 后继续。", flush=True)
        time.sleep(max(args.interval, 1))


def run_local_proxy_worker(ns: argparse.Namespace) -> int:
    default_patterns = all_claude_account_patterns() if ns.protocol == "anthropic" else all_account_patterns()
    accounts = coerce_account_patterns(ns.accounts, default_patterns)
    args = argparse.Namespace(
        host=ns.host,
        port=ns.port,
        accounts=accounts,
        api_base=ns.api_base,
        upstream_base_url=ns.upstream_base_url,
        state_path=Path(ns.state_path),
        log_path=Path(ns.log_path),
        timeout=ns.timeout,
        use_system_proxy=ns.use_system_proxy,
        proxy_url=ns.proxy_url,
        precheck=ns.precheck,
        log_non200_details=ns.log_non200_details,
        non200_log_dir=Path(ns.non200_log_dir),
        protocol=ns.protocol,
        dry_run=False,
    )
    return local_proxy.run_proxy(args)


def run_test_local_proxy_worker(ns: argparse.Namespace) -> int:
    """Send the same minimal Responses request used by Codex's provider test."""
    url = f"http://{ns.host}:{ns.port}/responses"
    payload = {"model": ns.model, "input": "hello", "max_output_tokens": 16}
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {DEFAULT_LOCAL_PROXY_API_KEY}",
            # ColdAPI's Cloudflare rule rejects Python's default urllib user
            # agent.  Use a neutral client signature so this diagnostic tests
            # the API route rather than the WAF signature rule.
            "User-Agent": "curl/8.0",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.monotonic()
    try:
        with opener.open(request, timeout=ns.timeout) as response:
            status = int(response.status)
            headers = response.headers
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        headers = exc.headers
        body = exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        print(
            f"[LOCAL_PROXY_TEST] ok=false stage=connect url={kv_value(url)} "
            f"elapsed={time.monotonic() - started:.1f}s error={kv_value(account_service.redact_log_text(exc))}",
            flush=True,
        )
        return 1

    account = str(headers.get("X-XHHAI-Account") or "")
    key_suffix = str(headers.get("X-XHHAI-Key-Suffix") or "")
    summary = account_service.redact_log_text(body).replace("\r", " ").replace("\n", " ")[:800]
    ok = 200 <= status < 300
    print(
        f"[LOCAL_PROXY_TEST] ok={'true' if ok else 'false'} stage=upstream "
        f"http={status} url={kv_value(url)} model={kv_value(ns.model)} "
        f"elapsed={time.monotonic() - started:.1f}s account={kv_value(account)} "
        f"key_suffix={kv_value(key_suffix)} response={kv_value(summary)}",
        flush=True,
    )
    return 0 if ok else 1


def run_install_proxy_profile_worker(ns: argparse.Namespace) -> int:
    result = install_local_proxy_profile(
        settings_path=Path(ns.codexpp_settings_path),
        profile_name=ns.profile_name,
        base_url=ns.base_url,
        api_key=ns.api_key,
        activate=ns.activate,
        dry_run=ns.dry_run,
    )
    print(result.summary(), flush=True)
    return 0 if not result.skipped else 1


def run_install_ccswitch_claude_worker(ns: argparse.Namespace) -> int:
    if ns.all_claude:
        results = ccswitch_sync.install_all_claude_providers(
            db_path=Path(ns.db_path),
            provider_name=ns.provider_name,
            source_provider_name=ns.source_provider_name,
            base_url=ns.base_url,
            auth_token=ns.auth_token,
            model=ns.model or None,
            set_current=not ns.no_set_current,
            create_backup=not ns.no_backup,
        )
        for result in results:
            print(result.summary(), flush=True)
    else:
        result = ccswitch_sync.install_provider(
            db_path=Path(ns.db_path),
            provider_id=ns.provider_id,
            provider_name=ns.provider_name,
            source_provider_name=ns.source_provider_name,
            app_type=ns.app_type,
            base_url=ns.base_url,
            auth_token=ns.auth_token,
            model=ns.model or None,
            set_current=not ns.no_set_current,
            create_backup=not ns.no_backup,
        )
        print(result.summary(), flush=True)
    return 0


def add_registration_worker_args(command: argparse.ArgumentParser) -> None:
    command.add_argument("--site-id", default="")
    command.add_argument("--account-kind", choices=("codex", "claude"), default="codex")
    command.add_argument("--route", default="")
    command.add_argument("--count", type=int, default=20)
    command.add_argument("--email-domain", default="qq.com")
    command.add_argument("--email-mode", choices=("auto", "direct", "receiver"), default="auto")
    command.add_argument("--receiver-config", default=str(email_receiver_config_path()))
    command.add_argument("--emails-file", default="")
    command.add_argument("--password", default="")
    command.add_argument("--output-dir", default=str(app_dir() / "accounts"))
    command.add_argument("--output-pattern", default=batch_create.DEFAULT_OUTPUT_PATTERN)
    command.add_argument("--overwrite", action="store_true")
    command.add_argument("--default-band", default="5-20")
    command.add_argument("--low-band", default="1-5")
    command.add_argument("--batch-mode", choices=("adaptive", "paced"), default="adaptive")
    command.add_argument("--paced-batch-size", type=int, default=2)
    command.add_argument("--paced-interval-seconds", type=int, default=2)
    command.add_argument("--paced-failure-step-seconds", type=int, default=1)
    command.add_argument("--paced-recovery-seconds", type=int, default=10)
    command.add_argument("--initial-concurrency", type=int)
    command.add_argument("--ramp-step", type=int, default=1)
    command.add_argument("--max-backoff-seconds", type=int, default=300)
    command.add_argument("--proxy-mode", choices=site_profiles.PROXY_MODES, default="自动")
    command.add_argument("--proxy-host", default=DEFAULT_PROXY_HOST)
    command.add_argument("--proxy-port", type=int, default=int(DEFAULT_PROXY_PORT))
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--key-name", default="")
    command.add_argument("--group-name", default="", help="兼容旧命令；注册阶段忽略。")
    command.add_argument("--api-base", default=DEFAULT_API_BASE, help="兼容旧命令；由站点配置决定。")
    command.add_argument("--use-system-proxy", action="store_true", help="兼容旧命令。")
    command.add_argument("--metrics-log", default=str(app_dir() / "xhhai_register_metrics.jsonl"))
    command.add_argument("--no-metrics", action="store_true")
    command.add_argument("--no-sync-codexpp", action="store_true")
    command.add_argument("--codexpp-settings-path", default=str(DEFAULT_CODEXPP_SETTINGS_PATH))
    command.add_argument("--codexpp-webui-path", default=str(DEFAULT_WEBUI_CUSTOM_PROVIDERS_PATH))
    command.add_argument("--codexpp-profile-name", default=DEFAULT_CODEXPP_PROFILE_NAME)


def build_worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Omini API 工具箱 worker")
    sub = parser.add_subparsers(dest="worker", required=True)

    batch = sub.add_parser("batch", help="兼容入口：完整自动开户。")
    add_registration_worker_args(batch)
    register_batch = sub.add_parser("register-batch", help="批量注册、建 Key 并自动选择首个分组。")
    add_registration_worker_args(register_batch)

    test_site = sub.add_parser("test-site")
    test_site.add_argument("--site-id", required=True)
    test_site.add_argument("--proxy-mode", choices=site_profiles.PROXY_MODES, default="自动")
    test_site.add_argument("--proxy-host", default=DEFAULT_PROXY_HOST)
    test_site.add_argument("--proxy-port", type=int, default=int(DEFAULT_PROXY_PORT))

    test_receiver = sub.add_parser("test-email-receiver")
    test_receiver.add_argument("--receiver-config", default=str(email_receiver_config_path()))
    test_receiver.add_argument("--proxy-mode", choices=site_profiles.PROXY_MODES, default="自动")
    test_receiver.add_argument("--proxy-host", default=DEFAULT_PROXY_HOST)
    test_receiver.add_argument("--proxy-port", type=int, default=int(DEFAULT_PROXY_PORT))

    discover = sub.add_parser("discover-sites", help="搜索并验证 XHHAI 兼容号池网站。")
    discover.add_argument("--count", type=int, default=1000)
    discover.add_argument("--probe-concurrency", type=int, default=20)
    discover.add_argument("--provision-concurrency", type=int, default=2)
    discover.add_argument("--email-domain", required=True)
    discover.add_argument("--email-mode", choices=("direct", "receiver"), default="direct")
    discover.add_argument("--key-name", default="codex")
    discover.add_argument("--receiver-config", default=str(email_receiver_config_path()))
    discover.add_argument("--proxy-mode", choices=site_profiles.PROXY_MODES, default="自动")
    discover.add_argument("--proxy-host", default=DEFAULT_PROXY_HOST)
    discover.add_argument("--proxy-port", type=int, default=int(DEFAULT_PROXY_PORT))
    discover.add_argument("--seed", type=int)
    discover.add_argument("--stop-file", default=str(app_dir() / "omini_site_discovery.stop"))

    site_groups = sub.add_parser("site-groups")
    site_groups.add_argument("--site-id", required=True)
    site_groups.add_argument("--account", action="append", nargs="+", required=True)
    site_groups.add_argument("--proxy-mode", choices=site_profiles.PROXY_MODES, default="自动")
    site_groups.add_argument("--proxy-host", default=DEFAULT_PROXY_HOST)
    site_groups.add_argument("--proxy-port", type=int, default=int(DEFAULT_PROXY_PORT))

    add_keys = sub.add_parser("add-keys")
    add_keys.add_argument("--site-id", required=True)
    add_keys.add_argument("--account", action="append", nargs="+", required=True)
    add_keys.add_argument("--group-id", required=True)
    add_keys.add_argument("--group-name", default="")
    add_keys.add_argument("--key-name", default=DEFAULT_KEY_NAME)
    add_keys.add_argument("--include-existing", action="store_true")
    add_keys.add_argument("--delay-seconds", type=float, default=0.0)
    add_keys.add_argument("--proxy-mode", choices=site_profiles.PROXY_MODES, default="自动")
    add_keys.add_argument("--proxy-host", default=DEFAULT_PROXY_HOST)
    add_keys.add_argument("--proxy-port", type=int, default=int(DEFAULT_PROXY_PORT))

    switch = sub.add_parser("switch-group")
    switch.add_argument("--accounts", action="append", default=[])
    switch.add_argument("--source-group-name", default="")
    switch.add_argument("--group-name", default=DEFAULT_GROUP_NAME)
    switch.add_argument("--key-name", default=DEFAULT_KEY_NAME)
    switch.add_argument("--api-base", default=DEFAULT_API_BASE)
    switch.add_argument("--use-system-proxy", action="store_true")
    switch.add_argument("--no-refresh", action="store_true")
    switch.add_argument("--no-backup", action="store_true")
    switch.add_argument("--dry-run", action="store_true")
    switch.add_argument("--batch-size", type=int, default=1)
    switch.add_argument("--delay-seconds", type=float, default=2.0)
    switch.add_argument("--limit", type=int, default=0)
    switch.add_argument("--target-dir", default="")
    switch.add_argument("--include-depleted", action="store_true")

    list_groups = sub.add_parser("list-groups")
    list_groups.add_argument("--accounts", action="append", default=[])
    list_groups.add_argument("--api-base", default=DEFAULT_API_BASE)
    list_groups.add_argument("--use-system-proxy", action="store_true")
    list_groups.add_argument("--no-refresh", action="store_true")

    balances = sub.add_parser("account-balances")
    balances.add_argument("--accounts", action="append", default=[])
    balances.add_argument("--api-base", default=DEFAULT_API_BASE)
    balances.add_argument("--use-system-proxy", action="store_true")
    balances.add_argument("--proxy-mode", choices=site_profiles.PROXY_MODES, default="自动")
    balances.add_argument("--proxy-host", default=DEFAULT_PROXY_HOST)
    balances.add_argument("--proxy-port", type=int, default=int(DEFAULT_PROXY_PORT))
    balances.add_argument("--available-threshold", type=float, default=0.0)
    balances.add_argument("--query-interval-seconds", type=float, default=2.0)
    balances.add_argument("--state-path", default=str(app_dir() / "xhhai_key_manager_state.json"))
    balances.add_argument("--log-path", default=str(app_dir() / "xhhai_key_manager.log"))

    monitor = sub.add_parser("monitor")
    monitor.add_argument("--accounts", action="append", default=[])
    monitor.add_argument("--api-base", default=DEFAULT_API_BASE)
    monitor.add_argument("--managed-base-url", default="https://api.xhhai.top")
    monitor.add_argument("--auth-path", default=str(Path.home() / ".codex" / "auth.json"))
    monitor.add_argument("--config-path", default=str(Path.home() / ".codex" / "config.toml"))
    monitor.add_argument("--auth-key-field", default="OPENAI_API_KEY")
    monitor.add_argument("--switch-mode", choices=("balance", "time", "local-balance"), default="balance")
    monitor.add_argument("--threshold", type=float, default=0.5)
    monitor.add_argument("--interval", type=int, default=1)
    monitor.add_argument("--rotate-seconds", type=int, default=3)
    monitor.add_argument("--state-path", default=str(app_dir() / "xhhai_key_manager_state.json"))
    monitor.add_argument("--dry-run", action="store_true")
    monitor.add_argument("--log-path", default=str(app_dir() / "xhhai_key_manager.log"))
    monitor.add_argument("--use-system-proxy", action="store_true")
    monitor.add_argument("--take-over", action="store_true")
    monitor.add_argument("--local-proxy-mode", action="store_true")
    monitor.add_argument("--no-sync-codexpp", action="store_true")
    monitor.add_argument("--codexpp-settings-path", default=str(DEFAULT_CODEXPP_SETTINGS_PATH))
    monitor.add_argument("--codexpp-webui-path", default=str(DEFAULT_WEBUI_CUSTOM_PROVIDERS_PATH))
    monitor.add_argument("--codexpp-profile-name", default=DEFAULT_CODEXPP_PROFILE_NAME)

    proxy = sub.add_parser("local-proxy")
    proxy.add_argument("--accounts", action="append", default=[])
    proxy.add_argument("--api-base", default=DEFAULT_API_BASE)
    proxy.add_argument("--upstream-base-url", default="https://api.xhhai.top")
    proxy.add_argument("--protocol", choices=("openai", "anthropic"), default="openai")
    proxy.add_argument("--host", default=local_proxy.DEFAULT_HOST)
    proxy.add_argument("--port", type=int, default=local_proxy.DEFAULT_PORT)
    proxy.add_argument("--state-path", default=str(app_dir() / "xhhai_key_manager_state.json"))
    proxy.add_argument("--log-path", default=str(app_dir() / "xhhai_key_manager.log"))
    proxy.add_argument("--timeout", type=int, default=600)
    proxy.add_argument("--use-system-proxy", action="store_true")
    proxy.add_argument("--proxy-url", default="")
    proxy.add_argument("--precheck", action="store_true")
    proxy.add_argument("--log-non200-details", action="store_true")
    proxy.add_argument("--non200-log-dir", default=str(app_dir() / "xhhai_upstream_non200_logs"))

    test_proxy = sub.add_parser("test-local-proxy")
    test_proxy.add_argument("--host", default=local_proxy.DEFAULT_HOST)
    test_proxy.add_argument("--port", type=int, default=local_proxy.DEFAULT_PORT)
    test_proxy.add_argument("--model", default="gpt-5.6-sol")
    test_proxy.add_argument("--timeout", type=int, default=45)

    install_proxy = sub.add_parser("install-proxy-profile")
    install_proxy.add_argument("--codexpp-settings-path", default=str(DEFAULT_CODEXPP_SETTINGS_PATH))
    install_proxy.add_argument("--profile-name", default=DEFAULT_LOCAL_PROXY_PROFILE_NAME)
    install_proxy.add_argument("--base-url", default=DEFAULT_LOCAL_PROXY_BASE_URL)
    install_proxy.add_argument("--api-key", default=DEFAULT_LOCAL_PROXY_API_KEY)
    install_proxy.add_argument("--activate", action="store_true")
    install_proxy.add_argument("--dry-run", action="store_true")

    install_ccswitch = sub.add_parser("install-ccswitch-claude")
    install_ccswitch.add_argument("--db-path", default=str(ccswitch_sync.DEFAULT_CCSWITCH_DB))
    install_ccswitch.add_argument("--provider-id", default=ccswitch_sync.DEFAULT_PROVIDER_ID)
    install_ccswitch.add_argument("--provider-name", default=ccswitch_sync.DEFAULT_PROVIDER_NAME)
    install_ccswitch.add_argument("--source-provider-name", default=ccswitch_sync.DEFAULT_SOURCE_PROVIDER_NAME)
    install_ccswitch.add_argument("--app-type", default="claude")
    install_ccswitch.add_argument("--base-url", default=ccswitch_sync.DEFAULT_LOCAL_BASE_URL)
    install_ccswitch.add_argument("--auth-token", default=ccswitch_sync.DEFAULT_LOCAL_AUTH_TOKEN)
    install_ccswitch.add_argument("--model", default="")
    install_ccswitch.add_argument("--no-set-current", action="store_true")
    install_ccswitch.add_argument("--no-backup", action="store_true")
    install_ccswitch.add_argument("--all-claude", action="store_true")
    return parser


def run_worker(argv: list[str]) -> int:
    parser = build_worker_parser()
    ns = parser.parse_args(argv)
    os.chdir(app_dir())
    if ns.worker in {"batch", "register-batch"}:
        return run_batch_worker(ns)
    if ns.worker == "test-site":
        return run_test_site_worker(ns)
    if ns.worker == "test-email-receiver":
        return run_test_email_receiver_worker(ns)
    if ns.worker == "discover-sites":
        return run_discover_sites_worker(ns)
    if ns.worker == "site-groups":
        return run_site_groups_worker(ns)
    if ns.worker == "add-keys":
        return run_add_keys_worker(ns)
    if ns.worker == "switch-group":
        return run_switch_worker(ns)
    if ns.worker == "list-groups":
        return run_list_groups_worker(ns)
    if ns.worker == "account-balances":
        return run_account_balances_worker(ns)
    if ns.worker == "monitor":
        return run_monitor_worker(ns)
    if ns.worker == "local-proxy":
        return run_local_proxy_worker(ns)
    if ns.worker == "test-local-proxy":
        return run_test_local_proxy_worker(ns)
    if ns.worker == "install-proxy-profile":
        return run_install_proxy_profile_worker(ns)
    if ns.worker == "install-ccswitch-claude":
        return run_install_ccswitch_claude_worker(ns)
    return 2


class ToolboxApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("980x720")
        self.minsize(900, 640)
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.running_processes: dict[str, subprocess.Popen[str]] = {}
        self.stop_all_requested = False
        self.site_store_path = app_dir() / "omini_sites.json"
        self.site_store = site_profiles.load_site_store(self.site_store_path)
        self._build_vars()
        self._load_settings()
        self._normalize_loaded_settings()
        self._sync_group_names_from_account_files()
        self._build_ui()
        self.after(150, self._drain_logs)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_vars(self) -> None:
        base = app_dir()
        self.receiver_config_path = email_receiver_config_path()
        try:
            receiver = email_receiver.load_receiver_config(self.receiver_config_path, create=True)
        except Exception:
            receiver = dict(email_receiver.DEFAULT_CONFIG)
        selected_site = site_profiles.default_site(self.site_store)
        self.current_site_id = tk.StringVar(value=str(selected_site["id"]))
        self.current_site_label = tk.StringVar(value="")
        self.proxy_mode = tk.StringVar(value="自动")
        self._loaded_proxy_mode_present = False
        self.count = tk.StringVar(value="20")
        self.email_domain = tk.StringVar(value="qq.com")
        self.email_mode = tk.StringVar(value="方案一（自定义后缀）")
        self.email_domain_history = ["qq.com", "gmail.com"]
        self.group_name = tk.StringVar(value=DEFAULT_GROUP_NAME)
        self.group_choices: list[str] = [DEFAULT_GROUP_NAME]
        self.main_group_box: ttk.Combobox | None = None
        self.advanced_group_box: ttk.Combobox | None = None
        self.route_group_boxes: list[ttk.Combobox] = []
        self.route_counts = {key: tk.StringVar(value="0") for key in ROUTE_KEYS}
        self.route_groups = {key: tk.StringVar(value=DEFAULT_ROUTE_GROUPS[key]) for key in ROUTE_KEYS}
        self.claude_group_boxes: list[ttk.Combobox] = []
        self.claude_route_counts = {key: tk.StringVar(value="0") for key in CLAUDE_ROUTE_KEYS}
        self.claude_route_groups = {key: tk.StringVar(value=DEFAULT_CLAUDE_ROUTE_GROUPS[key]) for key in CLAUDE_ROUTE_KEYS}
        self.claude_active_pool = tk.StringVar(value=CLAUDE_ROUTE_LABELS["A"])
        self.claude_balance_pool = tk.StringVar(value=CLAUDE_POOL_ALL)
        self.active_pool = tk.StringVar(value=POOL_ALL)
        self.balance_pool = tk.StringVar(value=POOL_ALL)
        self.switch_source_pool = tk.StringVar(value=POOL_ALL)
        self.switch_target_pool = tk.StringVar(value=ROUTE_LABELS["A"])
        self.group_switch_limit = tk.StringVar(value="0")
        self.include_depleted_switch = tk.BooleanVar(value=False)
        self.output_dir = tk.StringVar(value=str(base / "accounts"))
        self.account_pattern = tk.StringVar(value=route_account_pattern("A"))
        self.api_base = tk.StringVar(value=DEFAULT_API_BASE)
        self.key_name = tk.StringVar(value="codex")
        self.claude_key_name = tk.StringVar(value="claude")
        self.receiver_api_base = tk.StringVar(value=str(receiver.get("api_base") or ""))
        self.receiver_api_key = tk.StringVar(value=str(receiver.get("api_key") or ""))
        self.receiver_domain = tk.StringVar(value=str(receiver.get("domain") or ""))
        self.receiver_prefix = tk.StringVar(value=str(receiver.get("prefix") or "omini"))
        self.receiver_random_length = tk.StringVar(value=str(receiver.get("random_length") or 8))
        self.receiver_domain_level = tk.StringVar(value=str(receiver.get("domain_level") or 3))
        self.receiver_poll_interval = tk.StringVar(value=str(receiver.get("poll_interval") or 3))
        self.receiver_timeout = tk.StringVar(value=str(receiver.get("timeout") or 180))
        self.receiver_status = tk.StringVar(value="未测试")
        self.receiver_test_signature: str = ""
        self.receiver_test_running = False
        self.default_band = tk.StringVar(value="5-20")
        self.low_band = tk.StringVar(value="1-5")
        self.batch_mode = tk.StringVar(value="自动并发 5-20/1-5")
        self.paced_batch_size = tk.StringVar(value="2")
        self.paced_interval = tk.StringVar(value="2")
        self.paced_failure_step = tk.StringVar(value="1")
        self.paced_recovery = tk.StringVar(value="10")
        self.initial_concurrency = tk.StringVar(value="")
        self.ramp_step = tk.StringVar(value="1")
        self.max_backoff = tk.StringVar(value="300")
        self.overwrite = tk.BooleanVar(value=False)
        self.batch_dry_run = tk.BooleanVar(value=False)
        self.group_dry_run = tk.BooleanVar(value=False)
        self.group_batch_size = tk.StringVar(value="1")
        self.group_delay_seconds = tk.StringVar(value="2")
        self.no_refresh = tk.BooleanVar(value=False)
        self.no_backup = tk.BooleanVar(value=False)
        self.use_proxy = tk.BooleanVar(value=True)
        self.proxy_host = tk.StringVar(value=DEFAULT_PROXY_HOST)
        self.proxy_port = tk.StringVar(value=DEFAULT_PROXY_PORT)
        self.threshold = tk.StringVar(value="0.5")
        self.interval = tk.StringVar(value="1")
        self.monitor_mode = tk.StringVar(value="余额阈值切换")
        self.rotate_seconds = tk.StringVar(value="3")
        self.take_over = tk.BooleanVar(value=False)
        self.monitor_dry_run = tk.BooleanVar(value=False)
        self.sync_codexpp = tk.BooleanVar(value=False)
        self.managed_base_url = tk.StringVar(value="https://api.xhhai.top")
        self.auth_path = tk.StringVar(value=str(Path.home() / ".codex" / "auth.json"))
        self.config_path = tk.StringVar(value=str(Path.home() / ".codex" / "config.toml"))
        self.codexpp_settings_path = tk.StringVar(value=str(DEFAULT_CODEXPP_SETTINGS_PATH))
        self.codexpp_webui_path = tk.StringVar(value=str(DEFAULT_WEBUI_CUSTOM_PROVIDERS_PATH))
        self.codexpp_profile_name = tk.StringVar(value=DEFAULT_CODEXPP_PROFILE_NAME)
        self.log_path = tk.StringVar(value=str(base / "xhhai_key_manager.log"))
        self.state_path = tk.StringVar(value=str(base / "xhhai_key_manager_state.json"))
        self.local_proxy_host = tk.StringVar(value=local_proxy.DEFAULT_HOST)
        self.local_proxy_port = tk.StringVar(value=str(local_proxy.DEFAULT_PORT))
        self.local_proxy_profile_name = tk.StringVar(value=DEFAULT_LOCAL_PROXY_PROFILE_NAME)
        self.claude_local_proxy_host = tk.StringVar(value=local_proxy.DEFAULT_HOST)
        self.claude_local_proxy_port = tk.StringVar(value=str(DEFAULT_CLAUDE_PROXY_PORT))
        self.claude_base_url_display = tk.StringVar(value="")
        self.claude_state_path = tk.StringVar(value=str(base / "xhhai_claude_key_manager_state.json"))
        self.claude_log_path = tk.StringVar(value=str(base / "xhhai_claude_key_manager.log"))
        self.claude_non200_log_dir = tk.StringVar(value=str(base / "xhhai_claude_upstream_non200_logs"))
        self.claude_model = tk.StringVar(value=DEFAULT_CLAUDE_MODEL)
        self.claude_auth_token = tk.StringVar(value=DEFAULT_CLAUDE_AUTH_TOKEN)
        self.ccswitch_db_path = tk.StringVar(value=str(ccswitch_sync.DEFAULT_CCSWITCH_DB))
        self.ccswitch_provider_name = tk.StringVar(value=ccswitch_sync.DEFAULT_PROVIDER_NAME)
        self.ccswitch_source_provider_name = tk.StringVar(value=ccswitch_sync.DEFAULT_SOURCE_PROVIDER_NAME)
        self.ccswitch_set_current = tk.BooleanVar(value=True)
        self.log_non200_details = tk.BooleanVar(value=False)
        self.non200_log_dir = tk.StringVar(value=str(base / "xhhai_upstream_non200_logs"))
        self.balance_total_accounts = tk.StringVar(value="0")
        self.balance_usable_accounts = tk.StringVar(value="0")
        self.balance_remaining_total = tk.StringVar(value="0.0000")
        self.balance_consumed_total = tk.StringVar(value="0.0000")
        self.balance_status = tk.StringVar(value="未刷新")
        self.discovery_count = tk.StringVar(value="1000")
        self.discovery_probe_concurrency = tk.StringVar(value="20")
        self.discovery_status = tk.StringVar(value="尚未开始")
        self.discovery_generated = tk.StringVar(value="0")
        self.discovery_probed = tk.StringVar(value="0")
        self.discovery_compatible = tk.StringVar(value="0")
        self.discovery_promoted = tk.StringVar(value="0")
        self.discovery_tree: ttk.Treeview | None = None
        self.discovery_detail_text: tk.Text | None = None
        self.discovery_rows: dict[str, str] = {}
        self.discovery_detail_records: dict[str, dict[str, Any]] = {}
        self.account_site_filter = tk.StringVar(value="全部网站")
        self.account_kind_filter = tk.StringVar(value="全部类型")
        self.account_route_filter = tk.StringVar(value="全部路线")
        self.account_key_filter = tk.StringVar(value="全部Key状态")
        self.manual_group = tk.StringVar(value="")
        self.manual_key_name = tk.StringVar(value=DEFAULT_KEY_NAME)
        self.include_existing_key = tk.BooleanVar(value=False)
        self.manual_group_choices: dict[str, dict[str, Any]] = {}
        self.account_item_paths: dict[str, Path] = {}
        self.checked_account_paths: set[str] = set()
        self.checked_account_count = tk.StringVar(value="已勾选 0 个")
        self.site_tree: ttk.Treeview | None = None
        self.site_detail_text: tk.Text | None = None
        self.site_editor_id = ""
        self.registration_site_box: ttk.Combobox | None = None
        self.claude_site_box: ttk.Combobox | None = None
        self.account_site_filter_box: ttk.Combobox | None = None
        self.account_route_filter_box: ttk.Combobox | None = None
        self.site_name = tk.StringVar(value="")
        self.site_home_url = tk.StringVar(value="")
        self.site_api_base = tk.StringVar(value="")
        self.site_enabled = tk.BooleanVar(value=True)
        self.site_default = tk.BooleanVar(value=False)
        self.site_protocol_text: tk.Text | None = None
        self.site_status = tk.StringVar(value="请选择或新建站点。")
        self.balance_query_interval = tk.StringVar(value="2")
        self.route_balance_summaries = {key: tk.StringVar(value="0个 / 0可用 / 剩余0.0000") for key in ROUTE_KEYS}
        self.claude_balance_summaries = {key: tk.StringVar(value="0个 / 0可用 / 剩余0.0000") for key in CLAUDE_ROUTE_KEYS}
        self.claude_status = tk.StringVar(value="未启动")
        self.claude_balance_tree: ttk.Treeview | None = None
        self.claude_detail_text: tk.Text | None = None
        self.claude_item_paths: dict[str, Path] = {}
        self.claude_log_text: tk.Text | None = None
        self.balance_tree: ttk.Treeview | None = None
        self.balance_detail_text: tk.Text | None = None
        self.balance_token_failed_paths: set[str] = set()
        accounts_root().mkdir(parents=True, exist_ok=True)
        (accounts_root() / "codex").mkdir(parents=True, exist_ok=True)
        (accounts_root() / "discovery").mkdir(parents=True, exist_ok=True)
        for key in ROUTE_KEYS:
            route_dir(key).mkdir(parents=True, exist_ok=True)
        for key in CLAUDE_ROUTE_KEYS:
            claude_route_dir(key).mkdir(parents=True, exist_ok=True)

    def _settings_vars(self) -> dict[str, tk.Variable]:
        return {
            "site_id": self.current_site_id,
            "proxy_mode": self.proxy_mode,
            "count": self.count,
            "email_domain": self.email_domain,
            "email_mode": self.email_mode,
            "discovery_count": self.discovery_count,
            "discovery_probe_concurrency": self.discovery_probe_concurrency,
            "group_name": self.group_name,
            "route_a_count": self.route_counts["A"],
            "route_b_count": self.route_counts["B"],
            "route_c_count": self.route_counts["C"],
            "route_d_count": self.route_counts["D"],
            "route_a_group": self.route_groups["A"],
            "route_b_group": self.route_groups["B"],
            "route_c_group": self.route_groups["C"],
            "route_d_group": self.route_groups["D"],
            "claude_route_a_count": self.claude_route_counts["A"],
            "claude_route_b_count": self.claude_route_counts["B"],
            "claude_route_a_group": self.claude_route_groups["A"],
            "claude_route_b_group": self.claude_route_groups["B"],
            "claude_active_pool": self.claude_active_pool,
            "claude_balance_pool": self.claude_balance_pool,
            "active_pool": self.active_pool,
            "balance_pool": self.balance_pool,
            "switch_source_pool": self.switch_source_pool,
            "switch_target_pool": self.switch_target_pool,
            "group_switch_limit": self.group_switch_limit,
            "include_depleted_switch": self.include_depleted_switch,
            "output_dir": self.output_dir,
            "account_pattern": self.account_pattern,
            "key_name": self.key_name,
            "claude_key_name": self.claude_key_name,
            "default_band": self.default_band,
            "low_band": self.low_band,
            "batch_mode": self.batch_mode,
            "paced_batch_size": self.paced_batch_size,
            "paced_interval": self.paced_interval,
            "paced_failure_step": self.paced_failure_step,
            "paced_recovery": self.paced_recovery,
            "initial_concurrency": self.initial_concurrency,
            "ramp_step": self.ramp_step,
            "max_backoff": self.max_backoff,
            "overwrite": self.overwrite,
            "batch_dry_run": self.batch_dry_run,
            "group_dry_run": self.group_dry_run,
            "group_batch_size": self.group_batch_size,
            "group_delay_seconds": self.group_delay_seconds,
            "no_refresh": self.no_refresh,
            "no_backup": self.no_backup,
            "use_proxy": self.use_proxy,
            "proxy_host": self.proxy_host,
            "proxy_port": self.proxy_port,
            "threshold": self.threshold,
            "interval": self.interval,
            "monitor_mode": self.monitor_mode,
            "rotate_seconds": self.rotate_seconds,
            "take_over": self.take_over,
            "monitor_dry_run": self.monitor_dry_run,
            "sync_codexpp": self.sync_codexpp,
            "auth_path": self.auth_path,
            "config_path": self.config_path,
            "codexpp_settings_path": self.codexpp_settings_path,
            "codexpp_webui_path": self.codexpp_webui_path,
            "codexpp_profile_name": self.codexpp_profile_name,
            "log_path": self.log_path,
            "state_path": self.state_path,
            "local_proxy_host": self.local_proxy_host,
            "local_proxy_port": self.local_proxy_port,
            "local_proxy_profile_name": self.local_proxy_profile_name,
            "claude_local_proxy_host": self.claude_local_proxy_host,
            "claude_local_proxy_port": self.claude_local_proxy_port,
            "claude_state_path": self.claude_state_path,
            "claude_log_path": self.claude_log_path,
            "claude_non200_log_dir": self.claude_non200_log_dir,
            "claude_model": self.claude_model,
            "claude_auth_token": self.claude_auth_token,
            "ccswitch_db_path": self.ccswitch_db_path,
            "ccswitch_provider_name": self.ccswitch_provider_name,
            "ccswitch_source_provider_name": self.ccswitch_source_provider_name,
            "ccswitch_set_current": self.ccswitch_set_current,
            "log_non200_details": self.log_non200_details,
            "non200_log_dir": self.non200_log_dir,
            "balance_query_interval": self.balance_query_interval,
            "receiver_domain_level": self.receiver_domain_level,
        }

    def _load_settings(self) -> None:
        path = settings_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        values = data.get("values") if isinstance(data, dict) else None
        if not isinstance(values, dict):
            return
        self._loaded_proxy_mode_present = "proxy_mode" in values
        for name, var in self._settings_vars().items():
            if name not in values:
                continue
            try:
                var.set(values[name])
            except tk.TclError:
                continue
        choices = data.get("group_choices") if isinstance(data, dict) else None
        if isinstance(choices, list):
            self.group_choices = [str(item) for item in choices if str(item).strip()]
            if DEFAULT_GROUP_NAME not in self.group_choices:
                self.group_choices.append(DEFAULT_GROUP_NAME)
        domains = data.get("email_domain_history") if isinstance(data, dict) else None
        if isinstance(domains, list):
            for domain in domains:
                value = str(domain).strip().lstrip("@").lower()
                if value and value not in self.email_domain_history:
                    self.email_domain_history.append(value)

    def _normalize_loaded_settings(self) -> None:
        if self.email_mode.get() not in {EMAIL_MODE_DIRECT_LABEL, EMAIL_MODE_RECEIVER_LABEL}:
            self.email_mode.set(EMAIL_MODE_DIRECT_LABEL)
        for variable in (self.balance_pool, self.switch_source_pool):
            if variable.get() in LEGACY_POOL_NAMES:
                variable.set(POOL_ALL)
        if self.active_pool.get() in LEGACY_POOL_NAMES or self.active_pool.get() not in {POOL_ALL, *route_pool_labels()}:
            self.active_pool.set(POOL_ALL)
        available_ids = {str(item.get("id")) for item in self.site_store.get("sites", [])}
        if self.current_site_id.get() not in available_ids:
            self.current_site_id.set(str(site_profiles.default_site(self.site_store)["id"]))
        self.current_site_label.set(self._site_label_for_id(self.current_site_id.get()))
        current_profile = self._current_site_profile()
        self.api_base.set(str(current_profile["api_base"]))
        self.managed_base_url.set(str(current_profile["home_url"]))
        if not self._loaded_proxy_mode_present or self.proxy_mode.get() not in site_profiles.PROXY_MODES:
            self.proxy_mode.set("自动" if self.use_proxy.get() else "直连")
        self.use_proxy.set(self.proxy_mode.get() != "直连")
        self.take_over.set(False)
        self.sync_codexpp.set(False)
        self.local_proxy_profile_name.set(DEFAULT_LOCAL_PROXY_PROFILE_NAME)

    def _sync_group_names_from_account_files(self) -> None:
        route_sets = (
            (ROUTE_KEYS, route_dir, self.route_groups, DEFAULT_ROUTE_GROUPS),
            (CLAUDE_ROUTE_KEYS, claude_route_dir, self.claude_route_groups, DEFAULT_CLAUDE_ROUTE_GROUPS),
        )
        for keys, directory_for, variables, defaults in route_sets:
            for key in keys:
                found = False
                pattern = CLAUDE_ACCOUNT_FILE_PATTERN if variables is self.claude_route_groups else ACCOUNT_FILE_PATTERN
                for path in sorted(directory_for(key).glob(pattern)):
                    try:
                        data = json.loads(path.read_text(encoding="utf-8-sig"))
                    except Exception:
                        continue
                    group = data.get("group")
                    name = str(group.get("name") or "").strip() if isinstance(group, dict) else ""
                    if name:
                        variables[key].set(name)
                        if name not in self.group_choices:
                            self.group_choices.append(name)
                        found = True
                        break
                if not found:
                    variables[key].set(defaults[key])

    def _save_settings(self) -> None:
        values: dict[str, Any] = {}
        for name, var in self._settings_vars().items():
            try:
                values[name] = var.get()
            except tk.TclError:
                continue
        data = {
            "version": SETTINGS_VERSION,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "values": values,
            "group_choices": self.group_choices,
            "email_domain_history": self.email_domain_history,
        }
        path = settings_path()
        write_json_atomic(path, data, trailing_newline=True)

    def _receiver_config_from_ui(self) -> dict[str, Any]:
        return {
            "api_base": self.receiver_api_base.get(),
            "api_key": self.receiver_api_key.get(),
            "domain": self.receiver_domain.get(),
            "prefix": self.receiver_prefix.get(),
            "random_length": self.receiver_random_length.get(),
            "domain_level": self.receiver_domain_level.get(),
            "poll_interval": self.receiver_poll_interval.get(),
            "timeout": self.receiver_timeout.get(),
        }

    def save_email_receiver_settings(self, *, show_message: bool = True) -> bool:
        try:
            email_receiver.save_receiver_config(self.receiver_config_path, self._receiver_config_from_ui())
            self.receiver_status.set(f"已保存：{self.receiver_config_path.name}")
            if show_message:
                messagebox.showinfo(APP_TITLE, "验证码邮箱服务配置已保存。")
            return True
        except Exception as exc:
            self.receiver_status.set(f"保存失败：{exc}")
            if show_message:
                messagebox.showerror(APP_TITLE, str(exc))
            return False

    def _email_mode_value(self) -> str:
        return "receiver" if self.email_mode.get() == EMAIL_MODE_RECEIVER_LABEL else "direct"

    def _receiver_test_signature_value(self) -> str:
        profile = self._current_site_profile()
        config = email_receiver.validate_receiver_config(self._receiver_config_from_ui())
        payload = {
            "site_id": profile.get("id"),
            "api_base": profile.get("api_base"),
            "send_operation": (profile.get("operations") or {}).get("send_verification_code"),
            "receiver": config,
            "proxy_mode": self.proxy_mode.get(),
            "proxy_host": self.proxy_host.get().strip(),
            "proxy_port": self.proxy_port.get().strip(),
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def _require_receiver_test(self) -> None:
        if self._email_mode_value() != "receiver":
            return
        if self.receiver_test_signature != self._receiver_test_signature_value():
            raise site_profiles.SiteError("方案二需要先点击“测试邮箱验证码”，并等待真实接码成功。")

    def test_email_receiver(self) -> None:
        if self._email_mode_value() != "receiver":
            messagebox.showinfo(APP_TITLE, "请先选择方案二（临时邮箱接码）。")
            return
        if self.receiver_test_running:
            messagebox.showinfo(APP_TITLE, "邮箱验证码测试正在运行。")
            return
        if not self.save_email_receiver_settings(show_message=False):
            messagebox.showerror(APP_TITLE, self.receiver_status.get())
            return
        try:
            signature = self._receiver_test_signature_value()
            profile = self._current_site_profile()
            decision = site_profiles.resolve_proxy(
                self.proxy_mode.get(),
                self.proxy_host.get().strip() or DEFAULT_PROXY_HOST,
                int(self.proxy_port.get().strip() or DEFAULT_PROXY_PORT),
            )
            config = email_receiver.load_receiver_config(self.receiver_config_path)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.receiver_test_running = True
        self.receiver_test_signature = ""
        self.receiver_status.set("正在创建邮箱并等待当前网站验证码…")

        def work() -> None:
            try:
                result = account_service.test_receiver_delivery(
                    profile,
                    config,
                    use_proxy=decision.use_proxy,
                    proxy_url=decision.proxy_url,
                )
                status = (
                    f"接码成功：{email_receiver.mask_email(result.email)}，{result.code_length} 位验证码，"
                    f"耗时 {result.elapsed_seconds:.1f} 秒，轮询 {result.poll_count} 次，"
                    f"主题：{result.subject[:80] or '（无主题）'}。"
                )
                succeeded = True
            except Exception as exc:
                status = f"接码测试失败：{account_service.redact_log_text(exc)[:300]}"
                succeeded = False
            self.after(0, lambda: self._finish_receiver_test(signature, status, succeeded))
            self.log_queue.put(f"[验证码邮箱] {status}\n")

        threading.Thread(target=work, daemon=True).start()

    def _finish_receiver_test(self, signature: str, status: str, succeeded: bool) -> None:
        self.receiver_test_running = False
        try:
            unchanged = signature == self._receiver_test_signature_value()
        except Exception:
            unchanged = False
        if succeeded and unchanged:
            self.receiver_test_signature = signature
            self.receiver_status.set(status)
        elif succeeded:
            self.receiver_test_signature = ""
            self.receiver_status.set("接码已成功，但配置在测试期间发生变更，请重新测试。")
        else:
            self.receiver_test_signature = ""
            self.receiver_status.set(status)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)

        notebook = ttk.Notebook(root)
        self.notebook = notebook
        notebook.pack(fill=tk.BOTH, expand=True)

        main_outer, main = self._scrollable_tab(notebook)
        discovery_outer, discovery = self._scrollable_tab(notebook)
        balances_outer, balances = self._scrollable_tab(notebook)
        sites_outer, sites_tab = self._scrollable_tab(notebook)
        claude_outer, claude = self._scrollable_tab(notebook)
        advanced_outer, advanced = self._scrollable_tab(notebook)
        log_tab = ttk.Frame(notebook, padding=12)
        notebook.add(main_outer, text="Codex注册")
        notebook.add(discovery_outer, text="网站搜索")
        notebook.add(balances_outer, text="号池账号")
        notebook.add(sites_outer, text="号池网站")
        notebook.add(claude_outer, text="Claude 独立")
        notebook.add(advanced_outer, text="设置与分组")
        notebook.add(log_tab, text="日志")

        self._build_main_tab(main)
        self._build_discovery_tab(discovery)
        self._build_balances_tab(balances)
        self._build_sites_tab(sites_tab)
        self._build_claude_tab(claude)
        self._build_advanced_tab(advanced)
        self._build_log_tab(log_tab)
        for variable in (
            self.current_site_id, self.email_mode, self.proxy_mode, self.proxy_host, self.proxy_port,
            self.receiver_api_base, self.receiver_api_key, self.receiver_domain, self.receiver_prefix,
            self.receiver_random_length, self.receiver_domain_level, self.receiver_poll_interval,
            self.receiver_timeout,
        ):
            variable.trace_add("write", self._invalidate_receiver_test)

    def _invalidate_receiver_test(self, *_args: object) -> None:
        if self.receiver_test_signature or self.receiver_test_running:
            self.receiver_test_signature = ""
            self.receiver_status.set("网站、邮箱或代理配置已变更，当前测试结果将作废。")

    def _scrollable_tab(self, parent: ttk.Notebook) -> tuple[ttk.Frame, ttk.Frame]:
        outer = ttk.Frame(parent)
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        canvas = tk.Canvas(outer, highlightthickness=0)
        yscroll = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        body = ttk.Frame(canvas, padding=12)
        window = canvas.create_window((0, 0), window=body, anchor=tk.NW)
        canvas.configure(yscrollcommand=yscroll.set)
        canvas.grid(row=0, column=0, sticky=tk.NSEW)
        yscroll.grid(row=0, column=1, sticky=tk.NS)

        def refresh_scrollregion(_event: tk.Event) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def fit_width(event: tk.Event) -> None:
            canvas.itemconfigure(window, width=event.width)

        def on_mousewheel(event: tk.Event) -> None:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def bind_mousewheel(_event: tk.Event) -> None:
            canvas.bind_all("<MouseWheel>", on_mousewheel)

        def unbind_mousewheel(_event: tk.Event) -> None:
            canvas.unbind_all("<MouseWheel>")

        body.bind("<Configure>", refresh_scrollregion)
        canvas.bind("<Configure>", fit_width)
        outer.bind("<Enter>", bind_mousewheel)
        outer.bind("<Leave>", unbind_mousewheel)
        return outer, body

    def _field(self, parent: ttk.Frame, row: int, label: str, widget: tk.Widget, *, columnspan: int = 1) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, padx=(0, 8), pady=5)
        widget.grid(row=row, column=1, sticky=tk.EW, columnspan=columnspan, pady=5)
        parent.columnconfigure(1, weight=1)

    def _set_detail_text(self, widget: tk.Text | None, value: str) -> None:
        if widget is None:
            return
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert("1.0", value)
        widget.configure(state=tk.DISABLED)

    def _copy_detail_text(self, widget: tk.Text | None) -> None:
        if widget is None:
            return
        value = widget.get("1.0", "end-1c")
        if not value:
            return
        self.clipboard_clear()
        self.clipboard_append(value)
        self.update_idletasks()

    def _create_detail_box(
        self,
        parent: ttk.Frame,
        *,
        row: int,
        tree: ttk.Treeview,
        kind: str,
        columnspan: int = 1,
    ) -> tk.Text:
        box = ttk.LabelFrame(parent, text="选中行详情（可选中复制）", padding=8)
        box.grid(row=row, column=0, columnspan=columnspan, sticky=tk.EW, pady=(8, 0))
        box.columnconfigure(0, weight=1)
        detail = tk.Text(box, height=6, wrap=tk.WORD, state=tk.DISABLED)
        detail.grid(row=0, column=0, sticky=tk.EW)
        ttk.Button(box, text="复制详情", command=lambda: self._copy_detail_text(detail)).grid(
            row=1, column=0, sticky=tk.W, pady=(6, 0)
        )
        tree.bind(
            "<<TreeviewSelect>>",
            lambda _event, tree=tree, detail=detail, kind=kind: self._show_tree_detail(tree, detail, kind),
            add="+",
        )
        return detail

    def _show_tree_detail(self, tree: ttk.Treeview, detail: tk.Text, kind: str) -> None:
        selected = tree.selection()
        if not selected:
            self._set_detail_text(detail, "")
            return
        item = selected[-1]
        values = list(tree.item(item, "values"))
        lines: list[str] = []
        for index, column in enumerate(tree["columns"]):
            title = str(tree.heading(column, "text") or column)
            value = values[index] if index < len(values) else ""
            lines.append(f"{title}: {value}")
        if kind == "account":
            path = self.account_item_paths.get(item)
            if path is not None:
                lines.append(f"账号文件: {path.resolve()}")
                try:
                    data = account_service.read_account(path)
                    provisioning = data.get("provisioning") if isinstance(data.get("provisioning"), dict) else {}
                    lines.append("开户状态: " + json.dumps(provisioning, ensure_ascii=False, indent=2))
                    lines.append("余额检查: " + json.dumps(data.get("balance_check") or {}, ensure_ascii=False, indent=2))
                except Exception as exc:
                    lines.append(f"详情读取失败: {account_service.redact_log_text(exc)}")
        elif kind == "claude":
            path = self.claude_item_paths.get(item)
            if path is not None:
                lines.append(f"账号文件: {path.resolve()}")
                try:
                    data = account_service.read_account(path)
                    provisioning = data.get("provisioning") if isinstance(data.get("provisioning"), dict) else {}
                    lines.append("开户状态: " + json.dumps(provisioning, ensure_ascii=False, indent=2))
                    lines.append("余额检查: " + json.dumps(data.get("balance_check") or {}, ensure_ascii=False, indent=2))
                except Exception as exc:
                    lines.append(f"详情读取失败: {account_service.redact_log_text(exc)}")
        elif kind == "site":
            try:
                profile = site_profiles.site_by_id(self.site_store, str(item))
                lines.append(f"配置文件: {self.site_store_path.resolve()}")
                lines.append("协议映射: " + json.dumps(
                    {key: profile.get(key, {}) for key in ("verification", "key_workflow", "auth", "envelope", "operations")},
                    ensure_ascii=False,
                    indent=2,
                ))
            except Exception as exc:
                lines.append(f"详情读取失败: {exc}")
        elif kind == "discovery" and values:
            record = self.discovery_detail_records.get(str(values[0]))
            if record:
                lines.append("探测记录: " + json.dumps(record, ensure_ascii=False, indent=2))
        self._set_detail_text(detail, "\n".join(lines))

    def _pool_options(self, *, include_all: bool = True) -> list[str]:
        values: list[str] = []
        if include_all:
            values.append(POOL_ALL)
        values.extend(route_pool_labels())
        return values

    def _site_label(self, profile: dict[str, Any]) -> str:
        suffix = "（默认）" if profile.get("is_default") else ""
        return f"{profile.get('name', profile.get('id', '站点'))}{suffix} [{profile.get('id', '')}]"

    def _site_label_for_id(self, site_id: str) -> str:
        try:
            return self._site_label(site_profiles.site_by_id(self.site_store, site_id))
        except Exception:
            return ""

    def _site_labels(self) -> list[str]:
        return [self._site_label(item) for item in self.site_store.get("sites", []) if item.get("enabled", True)]

    def _site_id_from_label(self, label: str) -> str:
        if label.endswith("]") and "[" in label:
            return label.rsplit("[", 1)[1][:-1]
        return self.current_site_id.get()

    def _select_current_site(self, _event: tk.Event | None = None) -> None:
        site_id = self._site_id_from_label(self.current_site_label.get())
        try:
            profile = site_profiles.site_by_id(self.site_store, site_id)
        except Exception:
            return
        self.current_site_id.set(str(profile["id"]))
        self.api_base.set(str(profile["api_base"]))
        self.managed_base_url.set(str(profile["home_url"]))
        self._save_settings()

    def _current_site_profile(self) -> dict[str, Any]:
        return site_profiles.site_by_id(self.site_store, self.current_site_id.get())

    def _route_key_from_pool(self, pool: str) -> str | None:
        for key, label in ROUTE_LABELS.items():
            if pool == label:
                return key
        return None

    def _claude_route_key_from_pool(self, pool: str) -> str | None:
        for key, label in CLAUDE_ROUTE_LABELS.items():
            if pool == label:
                return key
        return None

    def _route_group_name(self, route_key: str) -> str:
        return self.route_groups[route_key].get().strip() or DEFAULT_ROUTE_GROUPS[route_key]

    def _claude_route_group_name(self, route_key: str) -> str:
        return self.claude_route_groups[route_key].get().strip() or DEFAULT_CLAUDE_ROUTE_GROUPS[route_key]

    def _account_patterns_for_pool(self, pool: str | None = None) -> list[str]:
        value = (pool or self.active_pool.get() or "").strip()
        route_key = self._route_key_from_pool(value)
        if value == POOL_ALL:
            return all_known_account_patterns()
        if route_key:
            return [route_account_pattern(route_key)]
        custom = self.account_pattern.get().strip()
        return [custom] if custom else all_account_patterns()

    def _claude_pool_options(self, *, include_all: bool = True) -> list[str]:
        values: list[str] = []
        if include_all:
            values.append(CLAUDE_POOL_ALL)
        values.extend(claude_pool_labels())
        return values

    def _claude_account_patterns_for_pool(self, pool: str | None = None) -> list[str]:
        value = (pool or self.claude_active_pool.get() or "").strip()
        route_key = self._claude_route_key_from_pool(value)
        if value == CLAUDE_POOL_ALL:
            return all_claude_account_patterns()
        if route_key:
            return [claude_route_account_pattern(route_key)]
        return all_claude_account_patterns()

    def _add_account_args(self, args: list[str], patterns: list[str]) -> None:
        for pattern in patterns:
            if pattern:
                args += ["--accounts", pattern]

    def _keyed_account_paths_for_pool(self, pool: str, account_kind: str) -> list[Path]:
        patterns = self._claude_account_patterns_for_pool(pool) if account_kind == "claude" else self._account_patterns_for_pool(pool)
        selected: list[Path] = []
        for path in group_switch.resolve_account_paths(patterns):
            try:
                data = account_service.read_account(path)
                if account_service.account_site_id(data) != self.current_site_id.get():
                    continue
                if str(data.get("account_kind") or "codex") != account_kind:
                    continue
                if account_service.account_has_key(data):
                    selected.append(path)
            except Exception:
                continue
        return selected

    def _add_keyed_account_args(self, args: list[str], pool: str, account_kind: str) -> None:
        paths = self._keyed_account_paths_for_pool(pool, account_kind)
        if not paths:
            # Keep the worker from falling back to its broad legacy defaults.
            paths = [app_dir() / "__no_key_accounts__.json"]
        self._add_account_args(args, [str(path) for path in paths])

    def _filtered_account_paths(self, pool: str, *, account_kind: str | None = None, keyed_only: bool = False) -> list[Path]:
        patterns = self._claude_account_patterns_for_pool(pool) if account_kind == "claude" else self._account_patterns_for_pool(pool)
        selected: list[Path] = []
        for path in group_switch.resolve_account_paths(patterns):
            try:
                data = account_service.read_account(path)
                if account_kind and str(data.get("account_kind") or "codex") != account_kind:
                    continue
                if keyed_only and not account_service.account_has_key(data):
                    continue
                row = self._balance_row_values_from_file(path)
                if account_kind is None:
                    if self.account_site_filter.get() != "全部网站" and row[0] != self.account_site_filter.get():
                        continue
                    if self.account_kind_filter.get() != "全部类型" and row[1] != self.account_kind_filter.get():
                        continue
                    if self.account_route_filter.get() != "全部路线" and row[2] != self.account_route_filter.get():
                        continue
                    if self.account_key_filter.get() != "全部Key状态" and row[4] != self.account_key_filter.get():
                        continue
                selected.append(path)
            except Exception:
                continue
        return selected

    def _parse_nonnegative_int(self, value: str, fallback: int = 0) -> int:
        try:
            return max(int(str(value).strip() or fallback), 0)
        except ValueError:
            return fallback

    def _registration_jobs(self) -> list[tuple[str, int, Path]]:
        count = self._parse_nonnegative_int(self.count.get(), 0)
        return [("默认", count, accounts_root() / "codex")] if count > 0 else []

    def _claude_registration_jobs(self) -> list[tuple[str, int, Path]]:
        jobs: list[tuple[str, int, Path]] = []
        for key in CLAUDE_ROUTE_KEYS:
            count = self._parse_nonnegative_int(self.claude_route_counts[key].get(), 0)
            if count <= 0:
                continue
            jobs.append((key, count, claude_route_dir(key)))
        return jobs

    def _apply_target_route_group(self, _event: tk.Event | None = None) -> None:
        route_key = self._route_key_from_pool(self.switch_target_pool.get())
        if route_key:
            self.group_name.set(self._route_group_name(route_key))

    def _build_route_registration_box(self, parent: ttk.Frame) -> ttk.LabelFrame:
        box = ttk.LabelFrame(parent, text="分路线注册（数量为 0 则跳过）", padding=10)
        box.columnconfigure(2, weight=1)
        ttk.Label(box, text="路线").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=(0, 4))
        ttk.Label(box, text="注册数量").grid(row=0, column=1, sticky=tk.W, padx=(0, 8), pady=(0, 4))
        ttk.Label(box, text="保存目录").grid(row=0, column=2, sticky=tk.W, pady=(0, 4))
        for row, key in enumerate(ROUTE_KEYS, start=1):
            ttk.Label(box, text=ROUTE_LABELS[key]).grid(row=row, column=0, sticky=tk.W, padx=(0, 8), pady=3)
            ttk.Entry(box, textvariable=self.route_counts[key], width=8).grid(row=row, column=1, sticky=tk.W, padx=(0, 8), pady=3)
            ttk.Label(box, text=str(route_dir(key)), foreground="#555").grid(row=row, column=2, sticky=tk.W, pady=3)
        return box

    def _build_main_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(1, weight=1)
        site_box = ttk.Combobox(tab, textvariable=self.current_site_label, values=self._site_labels(), state="readonly")
        site_box.bind("<<ComboboxSelected>>", self._select_current_site)
        self.registration_site_box = site_box
        self._field(tab, 0, "号池网站", site_box)

        mode_box = ttk.Frame(tab)
        ttk.Combobox(
            mode_box, textvariable=self.email_mode,
            values=(EMAIL_MODE_DIRECT_LABEL, EMAIL_MODE_RECEIVER_LABEL), state="readonly", width=26,
        ).pack(side=tk.LEFT)
        ttk.Button(mode_box, text="测试邮箱验证码", command=self.test_email_receiver).pack(side=tk.LEFT, padx=(10, 8))
        ttk.Label(mode_box, textvariable=self.receiver_status, foreground="#555").pack(side=tk.LEFT)
        self._field(tab, 1, "邮箱方案", mode_box)

        domain_box = ttk.Combobox(tab, textvariable=self.email_domain, values=self.email_domain_history)
        self._field(tab, 2, "方案一邮箱后缀", domain_box)
        self._field(tab, 3, "注册数量", ttk.Entry(tab, textvariable=self.count))
        self._field(tab, 4, "Key 名称", ttk.Entry(tab, textvariable=self.key_name))

        proxy = ttk.Frame(tab)
        ttk.Combobox(proxy, textvariable=self.proxy_mode, values=site_profiles.PROXY_MODES, state="readonly", width=10).grid(row=0, column=0)
        ttk.Entry(proxy, textvariable=self.proxy_host, width=14).grid(row=0, column=1, padx=(14, 4))
        ttk.Label(proxy, text=":").grid(row=0, column=2)
        ttk.Entry(proxy, textvariable=self.proxy_port, width=8).grid(row=0, column=3, padx=(4, 0))
        self._field(tab, 5, "代理/VPN", proxy)

        concurrency = ttk.Frame(tab)
        ttk.Combobox(concurrency, textvariable=self.batch_mode, values=("自动并发 5-20/1-5", "节拍注册 x个/y秒"), state="readonly", width=24).pack(side=tk.LEFT)
        ttk.Label(concurrency, text="起始并发").pack(side=tk.LEFT, padx=(14, 6))
        ttk.Entry(concurrency, textvariable=self.initial_concurrency, width=8).pack(side=tk.LEFT)
        self._field(tab, 6, "注册并发", concurrency)

        buttons = ttk.Frame(tab)
        ttk.Button(buttons, text="开始 Codex 注册", command=self.run_selected_mode).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(buttons, text="停止注册任务", command=self.stop_codex_processes).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(buttons, text="打开账号目录", command=self.open_accounts_dir).pack(side=tk.LEFT, padx=(0, 8))
        buttons.grid(row=7, column=0, columnspan=2, sticky=tk.W, pady=(16, 8))
        ttk.Label(
            tab,
            text="固定密码 1234578Cao；方案二需先完成真实发码与收码测试，注册后查询账号余额、创建 Key 并选择首个分组。",
            wraplength=820,
            foreground="#555",
        ).grid(row=8, column=0, columnspan=2, sticky=tk.W)

    def _build_discovery_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(4, weight=1)

        options = ttk.LabelFrame(tab, text="XHHAI 兼容网站搜索", padding=10)
        options.grid(row=0, column=0, sticky=tk.EW, pady=(0, 8))
        ttk.Label(options, text="候选数量").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(options, textvariable=self.discovery_count, width=10).grid(row=0, column=1, padx=(6, 18))
        ttk.Label(options, text="探测并发").grid(row=0, column=2, sticky=tk.W)
        ttk.Entry(options, textvariable=self.discovery_probe_concurrency, width=8).grid(row=0, column=3, padx=(6, 18))
        ttk.Label(options, text="注册邮箱后缀").grid(row=0, column=4, sticky=tk.W)
        ttk.Combobox(options, textvariable=self.email_domain, values=self.email_domain_history, width=18).grid(row=0, column=5, padx=(6, 18))
        ttk.Label(options, text="Key 名称").grid(row=0, column=6, sticky=tk.W)
        ttk.Entry(options, textvariable=self.key_name, width=12).grid(row=0, column=7, padx=(6, 0))

        network = ttk.Frame(options)
        network.grid(row=1, column=0, columnspan=8, sticky=tk.W, pady=(10, 0))
        ttk.Label(network, text="邮箱方案").pack(side=tk.LEFT)
        ttk.Combobox(
            network, textvariable=self.email_mode,
            values=(EMAIL_MODE_DIRECT_LABEL, EMAIL_MODE_RECEIVER_LABEL), state="readonly", width=24,
        ).pack(side=tk.LEFT, padx=(6, 14))
        ttk.Label(network, text="代理").pack(side=tk.LEFT)
        ttk.Combobox(network, textvariable=self.proxy_mode, values=site_profiles.PROXY_MODES, state="readonly", width=10).pack(side=tk.LEFT, padx=(6, 8))
        ttk.Entry(network, textvariable=self.proxy_host, width=14).pack(side=tk.LEFT)
        ttk.Label(network, text=":").pack(side=tk.LEFT, padx=3)
        ttk.Entry(network, textvariable=self.proxy_port, width=7).pack(side=tk.LEFT)
        ttk.Button(network, text="开始搜索", command=self.start_site_discovery).pack(side=tk.LEFT, padx=(18, 8))
        ttk.Button(network, text="停止", command=self.stop_site_discovery).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(network, text="清空结果", command=self.clear_site_discovery_results).pack(side=tk.LEFT)

        metrics = ttk.Frame(tab)
        metrics.grid(row=1, column=0, sticky=tk.EW)
        self._metric_cell(metrics, 0, 0, "候选", self.discovery_generated)
        self._metric_cell(metrics, 0, 1, "已探测", self.discovery_probed)
        self._metric_cell(metrics, 0, 2, "兼容站点", self.discovery_compatible)
        self._metric_cell(metrics, 0, 3, "已加入", self.discovery_promoted)
        ttk.Label(tab, textvariable=self.discovery_status, foreground="#555").grid(row=2, column=0, sticky=tk.W, pady=(0, 8))

        columns = ("url", "fingerprint", "registration", "balance", "key", "group", "decision", "status")
        tree = ttk.Treeview(tab, columns=columns, show="headings", height=20)
        headings = {
            "url": "网址", "fingerprint": "指纹", "registration": "注册", "balance": "账号余额",
            "key": "Key", "group": "分组", "decision": "入库结论", "status": "状态",
        }
        widths = {"url": 220, "fingerprint": 90, "registration": 75, "balance": 90, "key": 80, "group": 80, "decision": 120, "status": 200}
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], anchor=tk.W)
        scroll = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.grid(row=4, column=0, sticky=tk.NSEW)
        scroll.grid(row=4, column=1, sticky=tk.NS)
        self.discovery_tree = tree
        self.discovery_detail_text = self._create_detail_box(tab, row=5, tree=tree, kind="discovery")

    def clear_site_discovery_results(self) -> None:
        if self.discovery_tree is not None:
            for item in self.discovery_tree.get_children():
                self.discovery_tree.delete(item)
        self.discovery_rows.clear()
        self.discovery_detail_records.clear()
        self._set_detail_text(self.discovery_detail_text, "")
        for variable in (self.discovery_generated, self.discovery_probed, self.discovery_compatible, self.discovery_promoted):
            variable.set("0")
        self.discovery_status.set("结果已清空")

    def start_site_discovery(self) -> None:
        try:
            count = max(1, int(self.discovery_count.get().strip()))
            concurrency = max(1, int(self.discovery_probe_concurrency.get().strip()))
            mode = self._email_mode_value()
            domain = account_service.normalize_email_domain(self.email_domain.get()) if mode == "direct" else (self.email_domain.get().strip().lstrip("@") or "example.com")
            if mode == "receiver":
                email_receiver.validate_receiver_config(self._receiver_config_from_ui())
                if not self.save_email_receiver_settings(show_message=False):
                    raise site_profiles.SiteError(self.receiver_status.get())
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.clear_site_discovery_results()
        self.discovery_status.set("正在生成并探测候选网站…")
        args = [
            "--worker", "discover-sites", "--count", str(count),
            "--probe-concurrency", str(concurrency), "--provision-concurrency", "2",
            "--email-domain", domain, "--key-name", self.key_name.get().strip() or "codex",
            "--email-mode", mode,
            "--receiver-config", str(self.receiver_config_path),
            "--proxy-mode", self.proxy_mode.get(),
            "--proxy-host", self.proxy_host.get().strip() or DEFAULT_PROXY_HOST,
            "--proxy-port", self.proxy_port.get().strip() or DEFAULT_PROXY_PORT,
            "--stop-file", str(app_dir() / "omini_site_discovery.stop"),
        ]
        self._start_process("网站搜索", args, replace=True)

    def stop_site_discovery(self) -> None:
        stop_path = app_dir() / "omini_site_discovery.stop"
        write_json_atomic(stop_path, {"stop": True}, trailing_newline=True)
        self.discovery_status.set("正在停止；已完成的探测和账号结果会保留…")
        self.after(1200, lambda: self.stop_process("网站搜索"))

    def _handle_discovery_line(self, line: str) -> None:
        try:
            event = json.loads(line.strip())
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(event, dict) or "event" not in event:
            return
        kind = str(event["event"])
        if kind == "generated":
            self.discovery_generated.set(str(event.get("total", 0)))
            self.discovery_status.set("候选生成完成，正在执行 DNS、TLS 和协议指纹检查…")
            return
        if kind == "progress":
            self.discovery_probed.set(str(event.get("probed", 0)))
            self.discovery_compatible.set(str(event.get("compatible", 0)))
            self.discovery_status.set(f"已探测 {event.get('probed', 0)}，发现兼容站点 {event.get('compatible', 0)}。")
            return
        if kind == "probe":
            if self.discovery_tree is None:
                return
            url = str(event.get("url") or "")
            values = (url, event.get("fingerprint", ""), event.get("registration", ""), "", "", "", "待验证" if event.get("fingerprint") == "XHHAI" else "跳过", event.get("reason", ""))
            item = self.discovery_tree.insert("", tk.END, values=values)
            self.discovery_rows[url] = item
            self.discovery_detail_records[url] = dict(event)
            return
        if kind == "result":
            if self.discovery_tree is None:
                return
            url = str(event.get("url") or "")
            item = self.discovery_rows.get(url)
            status = str(event.get("status") or "")
            registration = "成功" if status in {"complete", "partial"} else "失败"
            stage_labels = {"success": "成功", "failed": "失败", "pending": "待执行"}
            decisions = {"promoted": "已加入 Codex", "promoted_needs_repair": "已加入/待修复", "quarantined": "隔离保留"}
            values = (
                url, "XHHAI", registration, "" if event.get("balance") is None else event.get("balance"),
                stage_labels.get(str(event.get("key_status")), event.get("key_status", "")),
                stage_labels.get(str(event.get("group_status")), event.get("group_status", "")),
                decisions.get(str(event.get("decision")), event.get("decision", "")), event.get("error", "") or status,
            )
            if item and self.discovery_tree.exists(item):
                self.discovery_tree.item(item, values=values)
            else:
                self.discovery_rows[url] = self.discovery_tree.insert("", tk.END, values=values)
            self.discovery_detail_records[url] = dict(event)
            return
        if kind == "summary":
            self.discovery_probed.set(str(event.get("probed", self.discovery_probed.get())))
            self.discovery_compatible.set(str(event.get("compatible", 0)))
            self.discovery_promoted.set(str(event.get("promoted", 0)))
            prefix = "搜索已停止" if event.get("stopped") else "搜索完成"
            self.discovery_status.set(
                f"{prefix}：兼容 {event.get('compatible', 0)}，加入 {event.get('promoted', 0)}，零余额 {event.get('zero_balance', 0)}，余额失败 {event.get('balance_failed', 0)}。"
            )
            try:
                self.site_store = site_profiles.load_site_store(self.site_store_path)
                self._refresh_site_choices()
                self._refresh_site_tree()
                self.load_local_balance_snapshot()
            except Exception as exc:
                self.log_queue.put(f"[网站搜索] 刷新本地配置失败: {exc}\n")

    def _metric_cell(self, parent: ttk.Frame, row: int, column: int, title: str, variable: tk.StringVar) -> None:
        box = ttk.Frame(parent, padding=(10, 8))
        box.grid(row=row, column=column, sticky=tk.EW, padx=(0, 10), pady=(0, 8))
        ttk.Label(box, text=title).pack(anchor=tk.W)
        ttk.Label(box, textvariable=variable, font=("Segoe UI", 18, "bold")).pack(anchor=tk.W)
        parent.columnconfigure(column, weight=1)

    def _build_balances_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(7, weight=1)

        summary = ttk.Frame(tab)
        summary.grid(row=0, column=0, sticky=tk.EW)
        self._metric_cell(summary, 0, 0, "已勾选账号", self.balance_total_accounts)
        self._metric_cell(summary, 0, 1, "查询成功账号", self.balance_usable_accounts)
        self._metric_cell(summary, 0, 2, "剩余总余额", self.balance_remaining_total)
        self._metric_cell(summary, 0, 3, "已消耗余额", self.balance_consumed_total)

        filters = ttk.LabelFrame(tab, text="账号筛选", padding=8)
        filters.grid(row=1, column=0, sticky=tk.EW, pady=(4, 8))
        ttk.Label(filters, text="网站").pack(side=tk.LEFT)
        site_filter = ttk.Combobox(filters, textvariable=self.account_site_filter, values=["全部网站", *[str(p.get("name")) for p in self.site_store.get("sites", [])]], state="readonly", width=16)
        site_filter.pack(side=tk.LEFT, padx=(6, 12))
        self.account_site_filter_box = site_filter
        ttk.Label(filters, text="类型").pack(side=tk.LEFT)
        kind_filter = ttk.Combobox(filters, textvariable=self.account_kind_filter, values=("全部类型", "codex", "claude"), state="readonly", width=12)
        kind_filter.pack(side=tk.LEFT, padx=(6, 12))
        ttk.Label(filters, text="路线").pack(side=tk.LEFT)
        route_filter = ttk.Combobox(
            filters,
            textvariable=self.account_route_filter,
            values=("全部路线", *route_pool_labels(), *claude_pool_labels()),
            state="readonly",
            width=14,
        )
        route_filter.pack(side=tk.LEFT, padx=(6, 12))
        self.account_route_filter_box = route_filter
        ttk.Label(filters, text="Key").pack(side=tk.LEFT)
        key_filter = ttk.Combobox(filters, textvariable=self.account_key_filter, values=("全部Key状态", "已有Key", "待添加Key"), state="readonly", width=14)
        key_filter.pack(side=tk.LEFT, padx=(6, 12))
        for widget in (site_filter, kind_filter, route_filter, key_filter):
            widget.bind("<<ComboboxSelected>>", lambda _event: self.load_local_balance_snapshot())
        ttk.Button(filters, text="载入本地账号", command=self.load_local_balance_snapshot).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(filters, text="绑定到当前网站", command=self.rebind_selected_accounts).pack(side=tk.LEFT)
        ttk.Button(filters, text="重试自动开户", command=self.retry_selected_provisioning).pack(side=tk.LEFT, padx=(8, 0))

        key_box = ttk.LabelFrame(tab, text="为所选账号批量创建 / 更换 Key 与分组", padding=8)
        key_box.grid(row=2, column=0, sticky=tk.EW, pady=(0, 8))
        ttk.Button(key_box, text="检测共同分组", command=self.detect_groups_for_selected_accounts).pack(side=tk.LEFT, padx=(0, 8))
        self.manual_group_box = ttk.Combobox(key_box, textvariable=self.manual_group, state="readonly", width=30)
        self.manual_group_box.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(key_box, text="Key名").pack(side=tk.LEFT)
        ttk.Entry(key_box, textvariable=self.manual_key_name, width=14).pack(side=tk.LEFT, padx=(6, 8))
        ttk.Checkbutton(key_box, text="已有 Key 时新建并替换", variable=self.include_existing_key).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(key_box, text="批量创建 / 更换 Key", command=self.add_keys_to_selected_accounts).pack(side=tk.LEFT)

        controls = ttk.Frame(tab)
        controls.grid(row=3, column=0, sticky=tk.EW, pady=(0, 8))
        ttk.Label(controls, text="查询间隔秒").pack(side=tk.LEFT)
        ttk.Entry(controls, textvariable=self.balance_query_interval, width=6).pack(side=tk.LEFT, padx=(6, 10))
        ttk.Button(controls, text="查询账号余额", command=self.refresh_account_balances).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(controls, text="刷新勾选账号 Token", command=self.refresh_current_pool_tokens).pack(side=tk.LEFT)

        selection = ttk.Frame(tab)
        selection.grid(row=4, column=0, sticky=tk.EW, pady=(0, 8))
        ttk.Button(selection, text="全选当前结果", command=self.select_all_visible_accounts).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(selection, text="反选当前结果", command=self.invert_visible_account_selection).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(selection, text="清空选择", command=self.clear_account_selection).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(selection, textvariable=self.checked_account_count).pack(side=tk.LEFT)

        services = ttk.LabelFrame(tab, text="Codex 余额监控与本地轮换", padding=8)
        services.grid(row=5, column=0, sticky=tk.EW, pady=(0, 8))
        ttk.Combobox(services, textvariable=self.active_pool, values=self._pool_options(), state="readonly", width=16).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Combobox(services, textvariable=self.monitor_mode, values=("余额阈值切换", "定时轮换 key", "本地余额切换（不请求余额）"), state="readonly", width=22).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(services, text="启动余额监控", command=lambda: self._start_process("余额监控", self._monitor_args(local_proxy_mode=False), replace=True)).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(services, text="修复 Codex 本地供应商", command=self.install_local_proxy_profile).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(services, text="启动本地代理", command=self.start_local_proxy).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(services, text="测试本地代理（hello）", command=self.test_local_proxy).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(services, text="停止监控/代理", command=lambda: (self.stop_monitor(), self.stop_local_proxy())).pack(side=tk.LEFT)

        ttk.Label(tab, textvariable=self.balance_status, foreground="#555").grid(row=6, column=0, sticky=tk.W, pady=(0, 6))

        columns = ("site", "kind", "route", "email", "key_status", "group", "balance", "consumed", "status")
        tree = ttk.Treeview(tab, columns=columns, show="tree headings", height=16, selectmode="extended")
        tree.heading("#0", text="选择")
        tree.column("#0", width=54, minwidth=54, stretch=False, anchor=tk.CENTER)
        headings = {"site": "网站", "kind": "类型", "route": "路线", "email": "账号", "key_status": "Key", "group": "Key 分组 / 路线", "balance": "当前余额", "consumed": "已消耗", "status": "状态"}
        widths = {"site": 100, "kind": 65, "route": 70, "email": 200, "key_status": 85, "group": 160, "balance": 85, "consumed": 80, "status": 210}
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], anchor=tk.W)
        yscroll = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=yscroll.set)
        tree.grid(row=7, column=0, sticky=tk.NSEW)
        yscroll.grid(row=7, column=1, sticky=tk.NS)
        tree.bind("<Button-1>", self._toggle_account_checkbox_click, add="+")
        tree.bind("<space>", self._toggle_account_checkbox_space, add="+")
        self.balance_tree = tree
        self.balance_detail_text = self._create_detail_box(tab, row=8, tree=tree, kind="account")
        self.after(100, self.load_local_balance_snapshot)

    def _build_sites_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(1, weight=1)
        columns = ("name", "home", "api", "state")
        tree = ttk.Treeview(tab, columns=columns, show="headings", height=7)
        for column, title, width in (("name", "名称", 140), ("home", "主页", 230), ("api", "API Base", 260), ("state", "状态", 100)):
            tree.heading(column, text=title)
            tree.column(column, width=width, anchor=tk.W)
        tree.grid(row=0, column=0, columnspan=2, sticky=tk.EW)
        tree.bind("<<TreeviewSelect>>", self._load_selected_site_editor)
        self.site_tree = tree

        editor = ttk.LabelFrame(tab, text="站点与协议映射", padding=10)
        editor.grid(row=1, column=0, columnspan=2, sticky=tk.NSEW, pady=(8, 8))
        editor.columnconfigure(1, weight=1)
        editor.rowconfigure(5, weight=1)
        ttk.Label(editor, text="名称").grid(row=0, column=0, sticky=tk.W, pady=3)
        ttk.Entry(editor, textvariable=self.site_name).grid(row=0, column=1, sticky=tk.EW, pady=3)
        ttk.Label(editor, text="主页地址").grid(row=1, column=0, sticky=tk.W, pady=3)
        ttk.Entry(editor, textvariable=self.site_home_url).grid(row=1, column=1, sticky=tk.EW, pady=3)
        ttk.Label(editor, text="API Base").grid(row=2, column=0, sticky=tk.W, pady=3)
        ttk.Entry(editor, textvariable=self.site_api_base).grid(row=2, column=1, sticky=tk.EW, pady=3)
        flags = ttk.Frame(editor)
        ttk.Checkbutton(flags, text="启用", variable=self.site_enabled).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(flags, text="默认站点", variable=self.site_default).pack(side=tk.LEFT)
        flags.grid(row=3, column=1, sticky=tk.W, pady=3)
        ttk.Label(editor, text="协议映射 JSON（verification/key_workflow/operations/auth/envelope）").grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(8, 3))
        protocol = tk.Text(editor, height=15, wrap=tk.NONE)
        protocol.grid(row=5, column=0, columnspan=2, sticky=tk.NSEW)
        self.site_protocol_text = protocol

        buttons = ttk.Frame(tab)
        buttons.grid(row=2, column=0, columnspan=2, sticky=tk.W)
        ttk.Button(buttons, text="新建", command=self.new_site_profile).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text="保存", command=self.save_site_profile).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text="复制", command=self.clone_site_profile).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text="删除", command=self.delete_site_profile).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text="设为默认", command=self.set_default_site_profile).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text="恢复XHHAI默认", command=self.restore_default_site_profile).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text="测试连接", command=self.test_current_site_profile).pack(side=tk.LEFT)
        ttk.Label(tab, textvariable=self.site_status, foreground="#555").grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))
        self.site_detail_text = self._create_detail_box(tab, row=4, tree=tree, kind="site", columnspan=2)
        self._refresh_site_tree()
        if self.site_tree is not None and self.current_site_id.get() in self.site_tree.get_children():
            self.site_tree.selection_set(self.current_site_id.get())
            self._fill_site_editor(self._current_site_profile())

    def _refresh_site_choices(self) -> None:
        labels = self._site_labels()
        self.current_site_label.set(self._site_label_for_id(self.current_site_id.get()))
        if self.registration_site_box is not None:
            self.registration_site_box.configure(values=labels)
        if self.claude_site_box is not None:
            self.claude_site_box.configure(values=labels)
        names = ["全部网站", *[str(item.get("name")) for item in self.site_store.get("sites", [])]]
        if self.account_site_filter_box is not None:
            self.account_site_filter_box.configure(values=names)
        if self.account_site_filter.get() not in names:
            self.account_site_filter.set("全部网站")

    def _refresh_site_tree(self) -> None:
        if self.site_tree is None:
            return
        for item in self.site_tree.get_children():
            self.site_tree.delete(item)
        for profile in self.site_store.get("sites", []):
            state = "默认" if profile.get("is_default") else ("启用" if profile.get("enabled", True) else "停用")
            self.site_tree.insert("", tk.END, iid=str(profile["id"]), values=(profile.get("name"), profile.get("home_url"), profile.get("api_base"), state))
        self._refresh_site_choices()

    def _fill_site_editor(self, profile: dict[str, Any]) -> None:
        self.site_editor_id = str(profile.get("id") or "")
        self.site_name.set(str(profile.get("name") or ""))
        self.site_home_url.set(str(profile.get("home_url") or ""))
        self.site_api_base.set(str(profile.get("api_base") or ""))
        self.site_enabled.set(bool(profile.get("enabled", True)))
        self.site_default.set(bool(profile.get("is_default", False)))
        if self.site_protocol_text is not None:
            protocol = {
                key: profile.get(key, {})
                for key in ("verification", "key_workflow", "headers", "auth", "envelope", "operations")
            }
            self.site_protocol_text.delete("1.0", tk.END)
            self.site_protocol_text.insert("1.0", json.dumps(protocol, ensure_ascii=False, indent=2))

    def _load_selected_site_editor(self, _event: tk.Event | None = None) -> None:
        if self.site_tree is None or not self.site_tree.selection():
            return
        site_id = str(self.site_tree.selection()[0])
        try:
            self._fill_site_editor(site_profiles.site_by_id(self.site_store, site_id))
            self.site_status.set(f"正在编辑站点：{self.site_name.get()}")
        except Exception as exc:
            self.site_status.set(str(exc))

    def _profile_from_editor(self) -> dict[str, Any]:
        if self.site_protocol_text is None:
            raise site_profiles.SiteError("协议编辑器尚未初始化。")
        try:
            protocol = json.loads(self.site_protocol_text.get("1.0", tk.END).strip() or "{}")
        except json.JSONDecodeError as exc:
            raise site_profiles.SiteError(f"协议映射 JSON 格式错误: {exc}") from exc
        if not isinstance(protocol, dict):
            raise site_profiles.SiteError("协议映射必须是 JSON 对象。")
        profile = {
            "id": self.site_editor_id or f"site-{time.time_ns()}",
            "name": self.site_name.get().strip(),
            "home_url": self.site_home_url.get().strip().rstrip("/"),
            "api_base": self.site_api_base.get().strip().rstrip("/"),
            "enabled": self.site_enabled.get(),
            "is_default": self.site_default.get(),
            "verification": protocol.get("verification") or {"mode": "auto"},
            "key_workflow": protocol.get("key_workflow") or {"create_requires_group": True},
            "headers": protocol.get("headers") or {},
            "auth": protocol.get("auth") or {},
            "envelope": protocol.get("envelope") or {},
            "operations": protocol.get("operations") or {},
        }
        site_profiles.validate_profile(profile)
        return profile

    def new_site_profile(self) -> None:
        profile = site_profiles.clone_profile(site_profiles.default_xhhai_profile(), name="新号池网站")
        self._fill_site_editor(profile)
        self.site_status.set("已创建未保存的站点配置，请填写后保存。")

    def save_site_profile(self) -> None:
        try:
            profile = self._profile_from_editor()
            sites = self.site_store.setdefault("sites", [])
            if profile.get("is_default"):
                for item in sites:
                    item["is_default"] = False
            for index, item in enumerate(sites):
                if str(item.get("id")) == str(profile["id"]):
                    sites[index] = profile
                    break
            else:
                sites.append(profile)
            site_profiles.save_site_store(self.site_store_path, self.site_store)
            if profile.get("is_default") or not self.current_site_id.get():
                self.current_site_id.set(str(profile["id"]))
            self._refresh_site_tree()
            if self.site_tree is not None:
                self.site_tree.selection_set(str(profile["id"]))
            self.site_status.set(f"站点已保存：{profile['name']}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def clone_site_profile(self) -> None:
        try:
            source = self._profile_from_editor()
            cloned = site_profiles.clone_profile(source)
            self.site_store.setdefault("sites", []).append(cloned)
            site_profiles.save_site_store(self.site_store_path, self.site_store)
            self._refresh_site_tree()
            self._fill_site_editor(cloned)
            if self.site_tree is not None:
                self.site_tree.selection_set(str(cloned["id"]))
            self.site_status.set(f"已复制站点：{cloned['name']}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def delete_site_profile(self) -> None:
        site_id = self.site_editor_id
        sites = self.site_store.get("sites", [])
        if not site_id or len(sites) <= 1:
            messagebox.showinfo(APP_TITLE, "至少保留一个号池网站。")
            return
        if not messagebox.askyesno(APP_TITLE, f"删除站点“{self.site_name.get()}”？账号文件会继续保留。"):
            return
        self.site_store["sites"] = [item for item in sites if str(item.get("id")) != site_id]
        if self.current_site_id.get() == site_id:
            self.current_site_id.set(str(site_profiles.default_site(self.site_store)["id"]))
        site_profiles.save_site_store(self.site_store_path, self.site_store)
        self._refresh_site_tree()
        self._fill_site_editor(site_profiles.default_site(self.site_store))
        self.site_status.set("站点已删除，账号文件保持原样。")

    def set_default_site_profile(self) -> None:
        if not self.site_editor_id:
            return
        for item in self.site_store.get("sites", []):
            item["is_default"] = str(item.get("id")) == self.site_editor_id
        site_profiles.save_site_store(self.site_store_path, self.site_store)
        self.current_site_id.set(self.site_editor_id)
        self._refresh_site_tree()
        self.site_default.set(True)
        self.site_status.set("默认站点已更新。")

    def restore_default_site_profile(self) -> None:
        restored = site_profiles.default_xhhai_profile()
        sites = self.site_store.setdefault("sites", [])
        for item in sites:
            item["is_default"] = False
        for index, item in enumerate(sites):
            if str(item.get("id")) == site_profiles.DEFAULT_SITE_ID:
                sites[index] = restored
                break
        else:
            sites.insert(0, restored)
        site_profiles.save_site_store(self.site_store_path, self.site_store)
        self.current_site_id.set(site_profiles.DEFAULT_SITE_ID)
        self._refresh_site_tree()
        self._fill_site_editor(restored)
        self.site_status.set("XHHAI 默认配置已恢复。")

    def test_current_site_profile(self) -> None:
        try:
            profile = self._profile_from_editor()
            decision = site_profiles.resolve_proxy(self.proxy_mode.get(), self.proxy_host.get().strip(), int(self.proxy_port.get().strip()))
            if decision.note:
                self.site_status.set(decision.note)
            result = site_profiles.test_site(profile, use_proxy=decision.use_proxy, proxy_url=decision.proxy_url)
            self.site_status.set(f"{profile['name']}：{result}")
        except Exception as exc:
            self.site_status.set(f"连接测试失败：{exc}")

    def _build_claude_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(5, weight=1)

        info = ttk.LabelFrame(tab, text="Claude 本地轮换代理（独立于 Codex）", padding=10)
        info.grid(row=0, column=0, sticky=tk.EW)
        info.columnconfigure(1, weight=1)
        self._update_claude_base_url_display()
        self.claude_local_proxy_host.trace_add("write", self._update_claude_base_url_display)
        self.claude_local_proxy_port.trace_add("write", self._update_claude_base_url_display)
        ttk.Label(info, text="本地 Base URL").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        ttk.Label(info, textvariable=self.claude_base_url_display, font=("Segoe UI", 10, "bold")).grid(row=0, column=1, sticky=tk.W, pady=3)
        ttk.Label(info, text="本地 Auth Token").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        ttk.Entry(info, textvariable=self.claude_auth_token, width=32).grid(row=1, column=1, sticky=tk.W, pady=3)
        ttk.Label(info, text="默认模型名").grid(row=2, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        ttk.Entry(info, textvariable=self.claude_model, width=32).grid(row=2, column=1, sticky=tk.W, pady=3)
        ttk.Label(
            info,
            text="Claude Code / VS Code 插件可直接在 CCSwitch 里选择 Omini API 本地轮换；CCSwitch 供应商只需创建一次，日常启动只开本地代理和余额监控。",
            foreground="#555",
        ).grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))

        groups = ttk.LabelFrame(tab, text="Claude 账号全自动注册", padding=10)
        groups.grid(row=1, column=0, sticky=tk.EW, pady=(0, 8))
        groups.columnconfigure(2, weight=1)
        ttk.Label(groups, text="注册网站").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=(0, 6))
        claude_site_box = ttk.Combobox(groups, textvariable=self.current_site_label, values=self._site_labels(), state="readonly", width=34)
        claude_site_box.grid(row=0, column=1, columnspan=2, sticky=tk.W, pady=(0, 6))
        claude_site_box.bind("<<ComboboxSelected>>", self._select_current_site)
        self.claude_site_box = claude_site_box
        ttk.Label(groups, text="邮箱方案").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        ttk.Combobox(
            groups, textvariable=self.email_mode,
            values=(EMAIL_MODE_DIRECT_LABEL, EMAIL_MODE_RECEIVER_LABEL), state="readonly", width=26,
        ).grid(row=1, column=1, sticky=tk.W, pady=3)
        test_box = ttk.Frame(groups)
        ttk.Button(test_box, text="测试邮箱验证码", command=self.test_email_receiver).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(test_box, textvariable=self.receiver_status, foreground="#555").pack(side=tk.LEFT)
        test_box.grid(row=1, column=2, sticky=tk.W, pady=3)
        ttk.Label(groups, text="路线").grid(row=2, column=0, sticky=tk.W, padx=(0, 8))
        ttk.Label(groups, text="注册数量").grid(row=2, column=1, sticky=tk.W, padx=(0, 8))
        ttk.Label(groups, text="目录").grid(row=2, column=2, sticky=tk.W)
        for row, key in enumerate(CLAUDE_ROUTE_KEYS, start=3):
            ttk.Label(groups, text=CLAUDE_ROUTE_LABELS[key]).grid(row=row, column=0, sticky=tk.W, padx=(0, 8), pady=3)
            ttk.Entry(groups, textvariable=self.claude_route_counts[key], width=8).grid(row=row, column=1, sticky=tk.W, padx=(0, 8), pady=3)
            ttk.Label(groups, text=str(claude_route_dir(key)), foreground="#555").grid(row=row, column=2, sticky=tk.W, pady=3)
        ttk.Label(groups, text="Key 名称").grid(row=5, column=0, sticky=tk.W, padx=(0, 8), pady=(6, 3))
        ttk.Entry(groups, textvariable=self.claude_key_name, width=24).grid(row=5, column=1, sticky=tk.W, pady=(6, 3))
        ttk.Label(groups, text="固定密码 1234578Cao；方案二测试通过后接码、建 Key、选择首个分组", foreground="#555").grid(row=5, column=2, sticky=tk.W, pady=(6, 3))

        controls = ttk.Frame(tab)
        controls.grid(row=2, column=0, sticky=tk.EW, pady=(0, 8))
        ttk.Label(controls, text="轮换号池").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Combobox(controls, textvariable=self.claude_active_pool, values=self._claude_pool_options(), state="readonly", width=18).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(controls, text="监听").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Entry(controls, textvariable=self.claude_local_proxy_host, width=14).pack(side=tk.LEFT)
        ttk.Label(controls, text=":").pack(side=tk.LEFT, padx=(4, 4))
        ttk.Entry(controls, textvariable=self.claude_local_proxy_port, width=7).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(controls, text="注册 Claude 账号", command=self.run_claude_registration).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(controls, text="启动 Claude 代理+监控", command=self.start_claude_services).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(controls, text="停止 Claude 任务", command=self.stop_claude_processes).pack(side=tk.LEFT)

        balances = ttk.LabelFrame(tab, text="Claude 账号额度", padding=10)
        balances.grid(row=3, column=0, sticky=tk.EW, pady=(0, 8))
        for index, key in enumerate(CLAUDE_ROUTE_KEYS):
            cell = ttk.Frame(balances, padding=(6, 4))
            cell.grid(row=0, column=index, sticky=tk.EW, padx=(0, 8))
            ttk.Label(cell, text=CLAUDE_ROUTE_LABELS[key]).pack(anchor=tk.W)
            ttk.Label(cell, textvariable=self.claude_balance_summaries[key], font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)
            balances.columnconfigure(index, weight=1)
        balance_controls = ttk.Frame(balances)
        balance_controls.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))
        ttk.Label(balance_controls, text="查看号池").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Combobox(balance_controls, textvariable=self.claude_balance_pool, values=self._claude_pool_options(), state="readonly", width=18).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(balance_controls, text="载入本地数据", command=self.load_claude_local_balance_snapshot).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(balance_controls, text="查询当前号池余额", command=self.refresh_claude_account_balances).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(balance_controls, text="刷新当前号池 Token", command=lambda: self.refresh_claude_account_tokens(self.claude_balance_pool.get())).pack(side=tk.LEFT)

        ttk.Label(tab, textvariable=self.claude_status, foreground="#555").grid(row=4, column=0, sticky=tk.W, pady=(0, 6))

        columns = ("route", "email", "balance", "initial", "consumed", "status")
        tree = ttk.Treeview(tab, columns=columns, show="headings", height=10)
        for column, title, width, anchor in (
            ("route", "路线", 100, tk.W),
            ("email", "账号", 230, tk.W),
            ("balance", "当前余额", 100, tk.E),
            ("initial", "初始余额", 100, tk.E),
            ("consumed", "已消耗", 100, tk.E),
            ("status", "状态", 280, tk.W),
        ):
            tree.heading(column, text=title)
            tree.column(column, width=width, anchor=anchor)
        yscroll = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=yscroll.set)
        tree.grid(row=5, column=0, sticky=tk.NSEW)
        yscroll.grid(row=5, column=1, sticky=tk.NS)
        self.claude_balance_tree = tree
        self.claude_detail_text = self._create_detail_box(tab, row=6, tree=tree, kind="claude")

        log_box = ttk.LabelFrame(tab, text="Claude 运行日志", padding=8)
        log_box.grid(row=7, column=0, sticky=tk.EW, pady=(10, 0))
        log_box.columnconfigure(0, weight=1)
        self.claude_log_text = tk.Text(log_box, wrap=tk.WORD, height=8)
        self.claude_log_text.grid(row=0, column=0, sticky=tk.EW)
        ttk.Button(log_box, text="清空 Claude 日志", command=lambda: self.claude_log_text.delete("1.0", tk.END) if self.claude_log_text else None).grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        self.after(150, self.load_claude_local_balance_snapshot)

    def _build_advanced_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(1, weight=1)
        self._field(tab, 0, "当前站点 API Base", ttk.Entry(tab, textvariable=self.api_base, state="readonly"))
        self._field(tab, 1, "Key 名称", ttk.Entry(tab, textvariable=self.key_name))
        self._field(
            tab,
            2,
            "注册并发模式",
            ttk.Combobox(
                tab,
                textvariable=self.batch_mode,
                values=("自动并发 5-20/1-5", "节拍注册 x个/y秒"),
                state="readonly",
            ),
        )
        self._field(tab, 3, "正常并发区间", ttk.Entry(tab, textvariable=self.default_band))
        self._field(tab, 4, "高压低并发区间", ttk.Entry(tab, textvariable=self.low_band))

        paced = ttk.Frame(tab)
        ttk.Label(paced, text="每轮").grid(row=0, column=0, padx=(0, 6))
        ttk.Entry(paced, textvariable=self.paced_batch_size, width=8).grid(row=0, column=1)
        ttk.Label(paced, text="个，间隔").grid(row=0, column=2, padx=(10, 6))
        ttk.Entry(paced, textvariable=self.paced_interval, width=8).grid(row=0, column=3)
        ttk.Label(paced, text="秒；失败+").grid(row=0, column=4, padx=(10, 6))
        ttk.Entry(paced, textvariable=self.paced_failure_step, width=8).grid(row=0, column=5)
        ttk.Label(paced, text="秒，").grid(row=0, column=6, padx=(6, 6))
        ttk.Entry(paced, textvariable=self.paced_recovery, width=8).grid(row=0, column=7)
        ttk.Label(paced, text="秒无失败后清零").grid(row=0, column=8, padx=(6, 0))
        self._field(tab, 5, "节拍注册", paced)

        self._field(tab, 6, "起始并发", ttk.Entry(tab, textvariable=self.initial_concurrency))
        self._field(tab, 7, "并发增减步长", ttk.Entry(tab, textvariable=self.ramp_step))
        self._field(tab, 8, "最大退避秒数", ttk.Entry(tab, textvariable=self.max_backoff))

        flags = ttk.Frame(tab)
        ttk.Checkbutton(flags, text="注册覆盖已存在 JSON", variable=self.overwrite).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(flags, text="注册 dry-run", variable=self.batch_dry_run).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(flags, text="监控 dry-run（不写 Codex）", variable=self.monitor_dry_run).pack(side=tk.LEFT)
        flags.grid(row=9, column=0, columnspan=2, sticky=tk.W, pady=8)

        self._field(tab, 10, "Codex 监控日志", ttk.Entry(tab, textvariable=self.log_path))
        self._field(tab, 11, "Codex 轮换状态文件", ttk.Entry(tab, textvariable=self.state_path))

        local_proxy_box = ttk.Frame(tab)
        ttk.Label(local_proxy_box, text="监听").grid(row=0, column=0, padx=(0, 6))
        ttk.Entry(local_proxy_box, textvariable=self.local_proxy_host, width=14).grid(row=0, column=1)
        ttk.Label(local_proxy_box, text=":").grid(row=0, column=2, padx=(4, 4))
        ttk.Entry(local_proxy_box, textvariable=self.local_proxy_port, width=8).grid(row=0, column=3)
        ttk.Label(local_proxy_box, text="固定供应商名").grid(row=0, column=4, padx=(16, 6))
        ttk.Entry(local_proxy_box, textvariable=self.local_proxy_profile_name, width=22, state="readonly").grid(row=0, column=5)
        ttk.Checkbutton(local_proxy_box, text="记录上游非200详情", variable=self.log_non200_details).grid(row=0, column=6, padx=(16, 0))
        self._field(tab, 12, "Codex 本地轮换代理", local_proxy_box)
        self._field(tab, 13, "非200详情日志目录", ttk.Entry(tab, textvariable=self.non200_log_dir))

        group_box = ttk.LabelFrame(tab, text="批量切换已有账号分组", padding=10)
        group_box.grid(row=14, column=0, columnspan=2, sticky=tk.EW, pady=(14, 0))
        group_box.columnconfigure(1, weight=1)
        ttk.Label(group_box, text="来源号池").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        ttk.Combobox(
            group_box,
            textvariable=self.switch_source_pool,
            values=self._pool_options(),
            state="readonly",
        ).grid(row=0, column=1, sticky=tk.W, pady=4)
        ttk.Label(group_box, text="目标路线").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        target_pool = ttk.Combobox(
            group_box,
            textvariable=self.switch_target_pool,
            values=route_pool_labels(),
            state="readonly",
        )
        target_pool.grid(row=1, column=1, sticky=tk.W, pady=4)
        target_pool.bind("<<ComboboxSelected>>", self._apply_target_route_group)
        ttk.Label(group_box, text="目标分组").grid(row=2, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        group_select = ttk.Frame(group_box)
        group_select.columnconfigure(0, weight=1)
        self.advanced_group_box = ttk.Combobox(group_select, textvariable=self.group_name, values=self.group_choices)
        self.advanced_group_box.grid(row=0, column=0, sticky=tk.EW)
        ttk.Button(group_select, text="刷新分组", command=self.refresh_group_choices).grid(row=0, column=1, padx=(8, 0))
        group_select.grid(row=2, column=1, sticky=tk.EW, pady=4)
        ttk.Label(group_box, text="切换数量").grid(row=3, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        ttk.Entry(group_box, textvariable=self.group_switch_limit, width=10).grid(row=3, column=1, sticky=tk.W, pady=4)
        group_flags = ttk.Frame(group_box)
        ttk.Checkbutton(group_flags, text="仅预览，不实际切换", variable=self.group_dry_run).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(group_flags, text="不刷新 token", variable=self.no_refresh).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(group_flags, text="不备份 JSON", variable=self.no_backup).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(group_flags, text="包含余额<=0账号", variable=self.include_depleted_switch).pack(side=tk.LEFT)
        group_flags.grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(6, 8))
        throttle = ttk.Frame(group_box)
        ttk.Label(throttle, text="每次切换").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Entry(throttle, textvariable=self.group_batch_size, width=8).pack(side=tk.LEFT)
        ttk.Label(throttle, text="个，间隔").pack(side=tk.LEFT, padx=(10, 6))
        ttk.Entry(throttle, textvariable=self.group_delay_seconds, width=8).pack(side=tk.LEFT)
        ttk.Label(throttle, text="秒").pack(side=tk.LEFT, padx=(6, 0))
        throttle.grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))
        ttk.Button(group_box, text="执行批量切分组", command=self.run_group_switch).grid(row=6, column=0, columnspan=2, sticky=tk.W)

        ccswitch_box = ttk.LabelFrame(tab, text="Claude CCSwitch 修复（一般无需使用）", padding=10)
        ccswitch_box.grid(row=15, column=0, columnspan=2, sticky=tk.EW, pady=(14, 0))
        ccswitch_box.columnconfigure(1, weight=1)
        ttk.Label(ccswitch_box, text="数据库").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        ttk.Entry(ccswitch_box, textvariable=self.ccswitch_db_path).grid(row=0, column=1, sticky=tk.EW, pady=3)
        ttk.Label(ccswitch_box, text="供应商名").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        ttk.Entry(ccswitch_box, textvariable=self.ccswitch_provider_name, width=24).grid(row=1, column=1, sticky=tk.W, pady=3)
        ttk.Label(ccswitch_box, text="参考供应商").grid(row=2, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        ttk.Entry(ccswitch_box, textvariable=self.ccswitch_source_provider_name, width=24).grid(row=2, column=1, sticky=tk.W, pady=3)
        ccswitch_buttons = ttk.Frame(ccswitch_box)
        ttk.Checkbutton(ccswitch_buttons, text="创建后设为当前", variable=self.ccswitch_set_current).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(ccswitch_buttons, text="读取参考模型", command=self.load_ccswitch_source_model).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(ccswitch_buttons, text="修复/重建供应商", command=self.install_ccswitch_claude_provider).pack(side=tk.LEFT)
        ccswitch_buttons.grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))

        receiver_box = ttk.LabelFrame(tab, text="验证码邮箱服务", padding=10)
        receiver_box.grid(row=16, column=0, columnspan=2, sticky=tk.EW, pady=(14, 0))
        receiver_box.columnconfigure(1, weight=1)
        ttk.Label(receiver_box, text="API Base").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        ttk.Entry(receiver_box, textvariable=self.receiver_api_base).grid(row=0, column=1, columnspan=5, sticky=tk.EW, pady=3)
        ttk.Label(receiver_box, text="API Key").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        ttk.Entry(receiver_box, textvariable=self.receiver_api_key, show="•").grid(row=1, column=1, columnspan=5, sticky=tk.EW, pady=3)
        ttk.Label(receiver_box, text="邮箱域名").grid(row=2, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        ttk.Entry(receiver_box, textvariable=self.receiver_domain, width=22).grid(row=2, column=1, sticky=tk.W, pady=3)
        ttk.Label(receiver_box, text="留空时随机可用域名", foreground="#555").grid(row=2, column=2, columnspan=4, sticky=tk.W, padx=(8, 0), pady=3)
        ttk.Label(receiver_box, text="前缀").grid(row=3, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        ttk.Entry(receiver_box, textvariable=self.receiver_prefix, width=16).grid(row=3, column=1, sticky=tk.W, pady=3)
        ttk.Label(receiver_box, text="随机长度").grid(row=3, column=2, sticky=tk.W, padx=(14, 6), pady=3)
        ttk.Entry(receiver_box, textvariable=self.receiver_random_length, width=8).grid(row=3, column=3, sticky=tk.W, pady=3)
        ttk.Label(receiver_box, text="轮询秒").grid(row=3, column=4, sticky=tk.W, padx=(14, 6), pady=3)
        ttk.Entry(receiver_box, textvariable=self.receiver_poll_interval, width=8).grid(row=3, column=5, sticky=tk.W, pady=3)
        ttk.Label(receiver_box, text="超时秒").grid(row=4, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        ttk.Entry(receiver_box, textvariable=self.receiver_timeout, width=10).grid(row=4, column=1, sticky=tk.W, pady=3)
        ttk.Label(receiver_box, text="域名级数").grid(row=4, column=2, sticky=tk.W, padx=(14, 6), pady=3)
        ttk.Combobox(receiver_box, textvariable=self.receiver_domain_level, values=("2", "3"), state="readonly", width=6).grid(row=4, column=3, sticky=tk.W, pady=3)
        ttk.Label(receiver_box, text="2=主域名，3=随机三级域名", foreground="#555").grid(row=4, column=4, columnspan=2, sticky=tk.W, padx=(8, 0), pady=3)
        receiver_buttons = ttk.Frame(receiver_box)
        ttk.Button(receiver_buttons, text="保存邮箱配置", command=self.save_email_receiver_settings).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(receiver_buttons, text="测试邮箱验证码", command=self.test_email_receiver).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(receiver_buttons, textvariable=self.receiver_status, foreground="#555").pack(side=tk.LEFT)
        receiver_buttons.grid(row=5, column=0, columnspan=6, sticky=tk.W, pady=(8, 0))

    def _build_log_tab(self, tab: ttk.Frame) -> None:
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        self.log_text = tk.Text(tab, wrap=tk.WORD, height=24)
        yscroll = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=yscroll.set)
        self.log_text.grid(row=0, column=0, sticky=tk.NSEW)
        yscroll.grid(row=0, column=1, sticky=tk.NS)
        controls = ttk.Frame(tab)
        ttk.Button(controls, text="清空日志", command=lambda: self.log_text.delete("1.0", tk.END)).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(controls, text="停止 Codex", command=self.stop_codex_processes).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(controls, text="停止 Claude", command=self.stop_claude_processes).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(controls, text="停止全部任务", command=self.stop_all_processes).pack(side=tk.LEFT)
        controls.grid(row=1, column=0, sticky=tk.W, pady=(8, 0))

    def _choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_dir.get() or str(app_dir()))
        if selected:
            self.output_dir.set(selected)
            self.account_pattern.set(str(Path(selected) / "xhhai_codex_account*.json"))

    def _append_log(self, text: str) -> None:
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)

    def _drain_logs(self) -> None:
        try:
            while True:
                self._append_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(150, self._drain_logs)

    def _proxy_env(self) -> dict[str, str]:
        env = os.environ.copy()
        use_proxy = self.proxy_mode.get() != "直连"
        self.use_proxy.set(use_proxy)
        if use_proxy:
            proxy = f"http://{self.proxy_host.get().strip()}:{self.proxy_port.get().strip()}"
            env["HTTP_PROXY"] = proxy
            env["HTTPS_PROXY"] = proxy
            env["http_proxy"] = proxy
            env["https_proxy"] = proxy
        return env

    def _process_env(self, *, apply_proxy: bool = True) -> dict[str, str]:
        if apply_proxy:
            return self._proxy_env()
        env = os.environ.copy()
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env.pop(key, None)
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["no_proxy"] = "127.0.0.1,localhost"
        return env

    def _common_worker_args(self) -> list[str]:
        args = ["--api-base", self.api_base.get().strip()]
        # "自动" means use the system proxy only when its configured local
        # listener is actually available.  Passing --use-system-proxy merely
        # because Auto is selected made the local relay wait for a dead proxy
        # and eventually return 502 for every upstream request.
        decision = site_profiles.resolve_proxy(
            self.proxy_mode.get(),
            self.proxy_host.get().strip() or DEFAULT_PROXY_HOST,
            int(self.proxy_port.get().strip() or DEFAULT_PROXY_PORT),
        )
        if decision.note:
            self.log_queue.put(f"[NETWORK] {decision.note}\n")
        bool_flag(args, decision.use_proxy, "--use-system-proxy")
        return args

    def _batch_args(
        self,
        *,
        disable_codexpp_sync: bool = False,
        count: int | str | None = None,
        output_dir: Path | str | None = None,
        account_kind: str = "codex",
        route: str = "",
    ) -> list[str]:
        batch_mode = "paced" if "节拍" in self.batch_mode.get() else "adaptive"
        selected_count = str(count if count is not None else (self.count.get().strip() or "20"))
        selected_output = str(output_dir if output_dir is not None else (self.output_dir.get().strip() or accounts_root()))
        mode = self._email_mode_value()
        domain = account_service.normalize_email_domain(self.email_domain.get()) if mode == "direct" else (self.email_domain.get().strip().lstrip("@") or "example.com")
        selected_key_name = (
            self.claude_key_name.get().strip() or "claude"
            if account_kind == "claude"
            else self.key_name.get().strip() or "codex"
        )
        if mode == "direct" and domain not in self.email_domain_history:
            self.email_domain_history.append(domain)
        args = [
            "--worker",
            "register-batch",
            "--site-id",
            self.current_site_id.get(),
            "--account-kind",
            account_kind,
            "--route",
            route,
            "--count",
            selected_count,
            "--email-domain",
            domain,
            "--email-mode",
            mode,
            "--receiver-config",
            str(self.receiver_config_path),
            "--key-name",
            selected_key_name,
            "--output-dir",
            selected_output,
            "--default-band",
            self.default_band.get().strip() or "5-20",
            "--low-band",
            self.low_band.get().strip() or "1-5",
            "--batch-mode",
            batch_mode,
            "--paced-batch-size",
            self.paced_batch_size.get().strip() or "2",
            "--paced-interval-seconds",
            self.paced_interval.get().strip() or "2",
            "--paced-failure-step-seconds",
            self.paced_failure_step.get().strip() or "1",
            "--paced-recovery-seconds",
            self.paced_recovery.get().strip() or "10",
            "--ramp-step",
            self.ramp_step.get().strip() or "1",
            "--max-backoff-seconds",
            self.max_backoff.get().strip() or "300",
            "--metrics-log",
            str(app_dir() / "xhhai_register_metrics.jsonl"),
            "--proxy-mode",
            self.proxy_mode.get(),
            "--proxy-host",
            self.proxy_host.get().strip() or DEFAULT_PROXY_HOST,
            "--proxy-port",
            self.proxy_port.get().strip() or DEFAULT_PROXY_PORT,
        ]
        if self.initial_concurrency.get().strip():
            args += ["--initial-concurrency", self.initial_concurrency.get().strip()]
        bool_flag(args, self.overwrite.get(), "--overwrite")
        bool_flag(args, self.batch_dry_run.get(), "--dry-run")
        if disable_codexpp_sync or not self.sync_codexpp.get():
            args.append("--no-sync-codexpp")
        args += ["--codexpp-settings-path", self.codexpp_settings_path.get().strip()]
        args += ["--codexpp-webui-path", self.codexpp_webui_path.get().strip()]
        args += ["--codexpp-profile-name", self.codexpp_profile_name.get().strip() or DEFAULT_CODEXPP_PROFILE_NAME]
        return args

    def _monitor_args(self, *, local_proxy_mode: bool = False) -> list[str]:
        monitor_mode = self.monitor_mode.get()
        if "本地余额" in monitor_mode:
            switch_mode = "local-balance"
        elif "定时" in monitor_mode:
            switch_mode = "time"
        else:
            switch_mode = "balance"
        args = [
            "--worker",
            "monitor",
            "--switch-mode",
            switch_mode,
            "--threshold",
            self.threshold.get().strip() or "0.5",
            "--interval",
            self.interval.get().strip() or "1",
            "--rotate-seconds",
            self.rotate_seconds.get().strip() or "3",
            "--state-path",
            self.state_path.get().strip(),
            "--managed-base-url",
            self.managed_base_url.get().strip() or "https://api.xhhai.top",
            "--auth-path",
            self.auth_path.get().strip(),
            "--config-path",
            self.config_path.get().strip(),
            "--log-path",
            self.log_path.get().strip(),
        ]
        self._add_keyed_account_args(args, self.active_pool.get(), "codex")
        args += self._common_worker_args()
        bool_flag(args, self.take_over.get() and not local_proxy_mode, "--take-over")
        bool_flag(args, local_proxy_mode, "--local-proxy-mode")
        bool_flag(args, self.monitor_dry_run.get(), "--dry-run")
        if local_proxy_mode or not self.sync_codexpp.get():
            args.append("--no-sync-codexpp")
        args += ["--codexpp-settings-path", self.codexpp_settings_path.get().strip()]
        args += ["--codexpp-webui-path", self.codexpp_webui_path.get().strip()]
        args += ["--codexpp-profile-name", self.codexpp_profile_name.get().strip() or DEFAULT_CODEXPP_PROFILE_NAME]
        return args

    def _claude_monitor_args(self) -> list[str]:
        monitor_mode = self.monitor_mode.get()
        if "本地余额" in monitor_mode:
            switch_mode = "local-balance"
        elif "定时" in monitor_mode:
            switch_mode = "time"
        else:
            switch_mode = "balance"
        args = [
            "--worker",
            "monitor",
            "--switch-mode",
            switch_mode,
            "--threshold",
            self.threshold.get().strip() or "0.5",
            "--interval",
            self.interval.get().strip() or "1",
            "--rotate-seconds",
            self.rotate_seconds.get().strip() or "3",
            "--state-path",
            self.claude_state_path.get().strip(),
            "--managed-base-url",
            self.managed_base_url.get().strip() or str(self._current_site_profile()["home_url"]),
            "--auth-path",
            self.auth_path.get().strip(),
            "--config-path",
            self.config_path.get().strip(),
            "--log-path",
            self.claude_log_path.get().strip(),
            "--local-proxy-mode",
            "--no-sync-codexpp",
        ]
        self._add_keyed_account_args(args, self.claude_active_pool.get(), "claude")
        args += self._common_worker_args()
        bool_flag(args, self.monitor_dry_run.get(), "--dry-run")
        args += ["--codexpp-settings-path", self.codexpp_settings_path.get().strip()]
        args += ["--codexpp-webui-path", self.codexpp_webui_path.get().strip()]
        args += ["--codexpp-profile-name", self.codexpp_profile_name.get().strip() or DEFAULT_CODEXPP_PROFILE_NAME]
        return args

    def _local_proxy_base_url(self) -> str:
        host = self.local_proxy_host.get().strip() or local_proxy.DEFAULT_HOST
        port = self.local_proxy_port.get().strip() or str(local_proxy.DEFAULT_PORT)
        return f"http://{host}:{port}/api/v1"

    def _claude_base_url(self) -> str:
        host = self.claude_local_proxy_host.get().strip() or local_proxy.DEFAULT_HOST
        port = self.claude_local_proxy_port.get().strip() or str(DEFAULT_CLAUDE_PROXY_PORT)
        return f"http://{host}:{port}"

    def _update_claude_base_url_display(self, *_args: Any) -> None:
        self.claude_base_url_display.set(self._claude_base_url())


    def _local_proxy_args(self, *, precheck: bool = False) -> list[str]:
        # Start listening immediately.  Account balances are already available
        # in the account view; a network precheck can otherwise delay startup
        # for the full upstream timeout when an optional system proxy is down.
        args = [
            "--worker",
            "local-proxy",
            "--host",
            self.local_proxy_host.get().strip() or local_proxy.DEFAULT_HOST,
            "--port",
            self.local_proxy_port.get().strip() or str(local_proxy.DEFAULT_PORT),
            "--state-path",
            self.state_path.get().strip(),
            "--log-path",
            self.log_path.get().strip(),
            "--non200-log-dir",
            self.non200_log_dir.get().strip(),
            "--upstream-base-url",
            self.managed_base_url.get().strip() or str(self._current_site_profile()["home_url"]),
        ]
        self._add_keyed_account_args(args, self.active_pool.get(), "codex")
        bool_flag(args, precheck, "--precheck")
        bool_flag(args, self.log_non200_details.get(), "--log-non200-details")
        args += self._common_worker_args()
        decision = site_profiles.resolve_proxy(
            self.proxy_mode.get(),
            self.proxy_host.get().strip() or DEFAULT_PROXY_HOST,
            int(self.proxy_port.get().strip() or DEFAULT_PROXY_PORT),
        )
        if decision.use_proxy and decision.proxy_url:
            args += ["--proxy-url", decision.proxy_url]
        return args

    def _test_local_proxy_args(self) -> list[str]:
        return [
            "--worker",
            "test-local-proxy",
            "--host",
            self.local_proxy_host.get().strip() or local_proxy.DEFAULT_HOST,
            "--port",
            self.local_proxy_port.get().strip() or str(local_proxy.DEFAULT_PORT),
            "--model",
            "gpt-5.6-sol",
            "--timeout",
            "25",
        ]

    def _claude_local_proxy_args(self, *, precheck: bool = False) -> list[str]:
        args = [
            "--worker",
            "local-proxy",
            "--protocol",
            "anthropic",
            "--host",
            self.claude_local_proxy_host.get().strip() or local_proxy.DEFAULT_HOST,
            "--port",
            self.claude_local_proxy_port.get().strip() or str(DEFAULT_CLAUDE_PROXY_PORT),
            "--state-path",
            self.claude_state_path.get().strip(),
            "--log-path",
            self.claude_log_path.get().strip(),
            "--non200-log-dir",
            self.claude_non200_log_dir.get().strip(),
            "--upstream-base-url",
            self.managed_base_url.get().strip() or str(self._current_site_profile()["home_url"]),
        ]
        self._add_keyed_account_args(args, self.claude_active_pool.get(), "claude")
        bool_flag(args, precheck, "--precheck")
        bool_flag(args, self.log_non200_details.get(), "--log-non200-details")
        args += self._common_worker_args()
        decision = site_profiles.resolve_proxy(
            self.proxy_mode.get(),
            self.proxy_host.get().strip() or DEFAULT_PROXY_HOST,
            int(self.proxy_port.get().strip() or DEFAULT_PROXY_PORT),
        )
        if decision.use_proxy and decision.proxy_url:
            args += ["--proxy-url", decision.proxy_url]
        return args

    def _install_proxy_profile_args(self) -> list[str]:
        return [
            "--worker",
            "install-proxy-profile",
            "--codexpp-settings-path",
            self.codexpp_settings_path.get().strip(),
            "--profile-name",
            DEFAULT_LOCAL_PROXY_PROFILE_NAME,
            "--base-url",
            self._local_proxy_base_url(),
            "--api-key",
            DEFAULT_LOCAL_PROXY_API_KEY,
            "--activate",
        ]

    def _switch_args(self) -> list[str]:
        target_route = self._route_key_from_pool(self.switch_target_pool.get())
        target_dir = route_dir(target_route) if target_route else Path(self.output_dir.get().strip() or accounts_root())
        args = [
            "--worker",
            "switch-group",
            "--source-group-name",
            "",
            "--group-name",
            self.group_name.get().strip() or DEFAULT_GROUP_NAME,
            "--key-name",
            self.key_name.get().strip() or DEFAULT_KEY_NAME,
            "--batch-size",
            self.group_batch_size.get().strip() or "1",
            "--delay-seconds",
            self.group_delay_seconds.get().strip() or "2",
            "--limit",
            self.group_switch_limit.get().strip() or "0",
            "--target-dir",
            str(target_dir),
        ]
        self._add_keyed_account_args(args, self.switch_source_pool.get(), "codex")
        args += self._common_worker_args()
        bool_flag(args, self.group_dry_run.get(), "--dry-run")
        bool_flag(args, self.no_refresh.get(), "--no-refresh")
        bool_flag(args, self.no_backup.get(), "--no-backup")
        bool_flag(args, self.include_depleted_switch.get(), "--include-depleted")
        return args

    def _list_groups_args(self) -> list[str]:
        args = [
            "--worker",
            "list-groups",
        ]
        self._add_keyed_account_args(args, POOL_ALL, "codex")
        args += self._common_worker_args()
        bool_flag(args, self.no_refresh.get(), "--no-refresh")
        return args

    def _account_balances_args(self, *, paths: list[Path] | None = None) -> list[str]:
        args = [
            "--worker",
            "account-balances",
            "--available-threshold",
            "0",
            "--query-interval-seconds",
            self.balance_query_interval.get().strip() or "2",
            "--state-path",
            self.state_path.get().strip(),
            "--log-path",
            self.log_path.get().strip(),
        ]
        selected_paths = paths if paths is not None else self._selected_account_paths()
        self._add_account_args(
            args,
            [str(path) for path in selected_paths] or [str(app_dir() / "__no_accounts_selected__.json")],
        )
        args += self._common_worker_args()
        args += [
            "--proxy-mode",
            self.proxy_mode.get(),
            "--proxy-host",
            self.proxy_host.get().strip() or DEFAULT_PROXY_HOST,
            "--proxy-port",
            self.proxy_port.get().strip() or DEFAULT_PROXY_PORT,
        ]
        return args

    def _claude_account_balances_args(self, *, pool: str | None = None) -> list[str]:
        args = [
            "--worker",
            "account-balances",
            "--available-threshold",
            "0",
            "--query-interval-seconds",
            self.balance_query_interval.get().strip() or "2",
            "--state-path",
            self.claude_state_path.get().strip(),
            "--log-path",
            self.claude_log_path.get().strip(),
        ]
        paths = self._filtered_account_paths(pool or self.claude_balance_pool.get(), account_kind="claude", keyed_only=False)
        self._add_account_args(args, [str(path) for path in paths] or [str(app_dir() / "__no_accounts__.json")])
        args += self._common_worker_args()
        args += [
            "--proxy-mode",
            self.proxy_mode.get(),
            "--proxy-host",
            self.proxy_host.get().strip() or DEFAULT_PROXY_HOST,
            "--proxy-port",
            self.proxy_port.get().strip() or DEFAULT_PROXY_PORT,
        ]
        return args

    def _ccswitch_claude_args(self) -> list[str]:
        args = [
            "--worker",
            "install-ccswitch-claude",
            "--all-claude",
            "--db-path",
            self.ccswitch_db_path.get().strip() or str(ccswitch_sync.DEFAULT_CCSWITCH_DB),
            "--provider-name",
            self.ccswitch_provider_name.get().strip() or ccswitch_sync.DEFAULT_PROVIDER_NAME,
            "--source-provider-name",
            self.ccswitch_source_provider_name.get().strip() or ccswitch_sync.DEFAULT_SOURCE_PROVIDER_NAME,
            "--base-url",
            self._claude_base_url(),
            "--auth-token",
            self.claude_auth_token.get().strip() or DEFAULT_CLAUDE_AUTH_TOKEN,
            "--model",
            self.claude_model.get().strip(),
        ]
        bool_flag(args, not self.ccswitch_set_current.get(), "--no-set-current")
        return args

    def _set_group_choices(self, names: list[str]) -> None:
        merged: list[str] = []
        route_names = [self._route_group_name(key) for key in ROUTE_KEYS]
        claude_names = [self._claude_route_group_name(key) for key in CLAUDE_ROUTE_KEYS]
        for name in [self.group_name.get().strip(), *route_names, *claude_names, DEFAULT_GROUP_NAME, *names]:
            if name and name not in merged:
                merged.append(name)
        self.group_choices = merged
        for widget in (self.main_group_box, self.advanced_group_box, *self.route_group_boxes, *self.claude_group_boxes):
            if widget is not None:
                widget.configure(values=merged)
        self._save_settings()

    def _append_claude_log(self, text: str) -> None:
        if self.claude_log_text is None:
            return
        self.claude_log_text.insert(tk.END, text)
        self.claude_log_text.see(tk.END)

    def _queue_process_log(self, name: str, text: str, *, target: str = "main") -> None:
        line = f"[{name}] {text}"
        self.log_queue.put(line)
        if target == "claude":
            self.after(0, lambda line=line: self._append_claude_log(line))

    def _start_process(self, name: str, args: list[str], *, replace: bool = False, log_target: str = "main") -> subprocess.Popen[str] | None:
        self._save_settings()
        if name in self.running_processes and self.running_processes[name].poll() is None:
            if not replace:
                messagebox.showwarning(APP_TITLE, f"{name} 已经在运行。")
                return None
            self.stop_process(name)

        cmd = worker_base_command() + args
        self._queue_process_log(name, f"启动: {' '.join(cmd)}\n", target=log_target)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            cmd,
            cwd=str(app_dir()),
            env=self._process_env(apply_proxy=not (len(args) >= 2 and args[0] == "--worker" and args[1] == "local-proxy")),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        self.running_processes[name] = process
        threading.Thread(target=self._read_process_output, args=(name, process, log_target), daemon=True).start()
        return process

    def _run_worker_sync(self, name: str, args: list[str], *, log_target: str = "main") -> int:
        self._save_settings()
        cmd = worker_base_command() + args
        self._queue_process_log(name, f"执行: {' '.join(cmd)}\n", target=log_target)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(
                cmd,
                cwd=str(app_dir()),
                env=self._process_env(apply_proxy=not (len(args) >= 2 and args[0] == "--worker" and args[1] == "local-proxy")),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        except Exception as exc:
            self._queue_process_log(name, f"启动失败: {exc}\n", target=log_target)
            return 1

        self.running_processes[name] = process
        assert process.stdout is not None
        for line in process.stdout:
            self._queue_process_log(name, line, target=log_target)
        code = process.wait()
        if self.running_processes.get(name) is process:
            self.running_processes.pop(name, None)
        self._queue_process_log(name, f"已结束，退出码={code}\n", target=log_target)
        return code

    def _read_process_output(self, name: str, process: subprocess.Popen[str], log_target: str = "main") -> None:
        assert process.stdout is not None
        for line in process.stdout:
            self._queue_process_log(name, line, target=log_target)
            if name == "网站搜索":
                self.after(0, lambda line=line: self._handle_discovery_line(line))
            elif name == "本地代理测试" and line.startswith("[LOCAL_PROXY_TEST]"):
                values = self._parse_key_value_line(line)
                if values.get("ok") == "true":
                    message = (
                        f"本地代理测试成功：HTTP {values.get('http', '')}，"
                        f"账号 {values.get('account', '')}，耗时 {values.get('elapsed', '')}。"
                    )
                else:
                    message = (
                        f"本地代理测试失败：HTTP {values.get('http', '') or '连接失败'}；"
                        f"{values.get('response') or values.get('error') or '请查看日志。'}"
                    )
                self.after(0, lambda message=message: self.balance_status.set(message))
        code = process.wait()
        self._queue_process_log(name, f"已结束，退出码={code}\n", target=log_target)
        if name == "网站搜索" and code != 0:
            self.after(0, lambda: self.discovery_status.set("搜索已停止，或部分网站开户检测失败；已完成结果均已保留。"))
        if self.running_processes.get(name) is process:
            self.running_processes.pop(name, None)

    def stop_process(self, name: str) -> None:
        process = self.running_processes.get(name)
        if process is None or process.poll() is not None:
            return
        self.log_queue.put(f"[{name}] 正在停止...\n")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        self.running_processes.pop(name, None)

    def _find_orphan_worker_pids(self) -> list[int]:
        current_pid = os.getpid()
        current_exe = str(Path(sys.executable).resolve())
        script_path = str(Path(__file__).resolve().parents[1] / "omini_api_runner.py")
        current_app_dir = str(app_dir())
        is_frozen = "$true" if getattr(sys, "frozen", False) else "$false"

        def ps_quote(value: str) -> str:
            return "'" + value.replace("'", "''") + "'"

        ps_script = f"""
$CurrentPid = {current_pid}
$CurrentExe = {ps_quote(current_exe)}
$ScriptPath = {ps_quote(script_path)}
$AppDir = {ps_quote(current_app_dir)}
$IsFrozen = {is_frozen}
Get-CimInstance Win32_Process | Where-Object {{
    if (-not $_.CommandLine) {{ return $false }}
    if ($_.ProcessId -eq $CurrentPid) {{ return $false }}
    # The executable is shared with other toolbox instances; stop this session only.
    if ($_.ParentProcessId -ne $CurrentPid) {{ return $false }}
    if ($_.CommandLine -notlike '*--worker*') {{ return $false }}
    $exe = [string]$_.ExecutablePath
    $cmd = [string]$_.CommandLine
    $sameFrozenExe = $IsFrozen -and $exe -and $exe.Equals($CurrentExe, [System.StringComparison]::OrdinalIgnoreCase) -and $cmd.Contains('--omini-api')
    $sameScript = $cmd.IndexOf($ScriptPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    $sameAppExe = $exe -and $exe.StartsWith($AppDir, [System.StringComparison]::OrdinalIgnoreCase) -and (
        $exe.EndsWith('\\Omini API工具箱.exe', [System.StringComparison]::OrdinalIgnoreCase) -or
        $exe.EndsWith('\\Omini_API工具箱.exe', [System.StringComparison]::OrdinalIgnoreCase)
    )
    return ($sameFrozenExe -or $sameScript -or $sameAppExe)
}} | Select-Object -ExpandProperty ProcessId
"""
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                timeout=10,
            )
        except Exception as exc:
            self.log_queue.put(f"[停止全部任务] 查找遗留 worker 失败: {exc}\n")
            return []

        pids: list[int] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                pid = int(line)
                if pid != current_pid:
                    pids.append(pid)
        if result.stderr.strip():
            self.log_queue.put(f"[停止全部任务] PowerShell 提示: {result.stderr.strip()}\n")
        return sorted(set(pids))

    def stop_all_processes(self) -> None:
        self.stop_all_requested = True
        tracked = [(name, proc) for name, proc in list(self.running_processes.items()) if proc.poll() is None]
        if not tracked:
            self.log_queue.put("[停止全部任务] 当前没有工具箱记录中的运行任务。\n")
        for name, _proc in tracked:
            self.stop_process(name)

        orphan_pids = self._find_orphan_worker_pids()
        if not orphan_pids:
            self.log_queue.put("[停止全部任务] 没有发现遗留 worker 进程。\n")
            return

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for pid in orphan_pids:
            self.log_queue.put(f"[停止全部任务] 正在终止遗留 worker PID={pid}...\n")
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )

    def stop_codex_processes(self) -> None:
        self.stop_all_requested = True
        for name in (
            "批量注册",
            "批量注册-A",
            "批量注册-B",
            "批量注册-C",
            "批量注册-D",
            "余额监控",
            "本地轮换代理",
            "创建本地轮换供应商",
            "账号额度",
        ):
            self.stop_process(name)
        self.log_queue.put("[Codex] 已请求停止 Codex 注册、监控和代理任务。\n")

    def start_claude_services(self) -> None:
        self.stop_all_requested = False
        self.start_claude_local_proxy()
        self.start_claude_monitor()

    def stop_claude_processes(self) -> None:
        for name in ("Claude注册-A", "Claude注册-B", "Claude余额监控", "Claude本地轮换代理"):
            self.stop_process(name)
        self.claude_status.set("Claude 任务已请求停止。")

    def run_selected_mode(self) -> None:
        try:
            if self._email_mode_value() == "direct":
                domain = account_service.normalize_email_domain(self.email_domain.get())
                self.email_domain.set(domain)
            self._current_site_profile()
            if not (self.key_name.get().strip() or ""):
                raise site_profiles.SiteError("Key 名称不能为空。")
            if self._email_mode_value() == "receiver":
                if not self.save_email_receiver_settings(show_message=False):
                    raise site_profiles.SiteError(self.receiver_status.get())
                self._require_receiver_test()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.stop_all_requested = False
        threading.Thread(target=self._run_selected_mode_sequence, daemon=True).start()

    def _run_registration_jobs_sequence(self, *, disable_codexpp_sync: bool) -> None:
        jobs = self._registration_jobs()
        if not jobs:
            self.log_queue.put("[批量注册] 没有可执行的注册数量；请至少填写一个大于 0 的数量。\n")
            return
        for route_key, count, output_dir in jobs:
            if self.stop_all_requested:
                self.log_queue.put("[批量注册] 已请求停止全部任务，跳过剩余注册任务。\n")
                return
            output_dir.mkdir(parents=True, exist_ok=True)
            label = "批量注册" if route_key == "默认" else f"批量注册-{route_key}"
            self.log_queue.put(
                f"[{label}] 计划: site={self.current_site_id.get()}, count={count}, output_dir={output_dir}\n"
            )
            self._run_worker_sync(
                label,
                self._batch_args(
                    disable_codexpp_sync=disable_codexpp_sync,
                    count=count,
                    output_dir=output_dir,
                    account_kind="codex",
                    route=route_key,
                ),
            )

    def _run_claude_registration_jobs_sequence(self) -> None:
        jobs = self._claude_registration_jobs()
        if not jobs:
            self._queue_process_log("Claude注册", "没有可执行的注册数量；请在 Claude 页至少填写一个大于 0 的数量。\n", target="claude")
            return
        for route_key, count, output_dir in jobs:
            if self.stop_all_requested:
                self._queue_process_log("Claude注册", "已请求停止全部任务，跳过剩余注册任务。\n", target="claude")
                return
            output_dir.mkdir(parents=True, exist_ok=True)
            label = f"Claude注册-{route_key}"
            self._queue_process_log(label, f"计划: site={self.current_site_id.get()}, count={count}, output_dir={output_dir}\n", target="claude")
            self._run_worker_sync(
                label,
                self._batch_args(
                    disable_codexpp_sync=True,
                    count=count,
                    output_dir=output_dir,
                    account_kind="claude",
                    route=route_key,
                ),
                log_target="claude",
            )

    def _run_selected_mode_sequence(self) -> None:
        self._run_registration_jobs_sequence(disable_codexpp_sync=True)

    def _run_claude_mode_sequence(self, mode: str) -> None:
        needs_monitor = "余额监控" in mode
        needs_registration = "只启动" not in mode

        if needs_monitor:
            self._queue_process_log(
                "Claude一键启动",
                f"启动 ClaudeCode 本地轮换代理：{self._claude_base_url()}；不会重复创建 CCSwitch 供应商。\n",
                target="claude",
            )
            self._start_process("Claude本地轮换代理", self._claude_local_proxy_args(), replace=True, log_target="claude")
            self._queue_process_log(
                "Claude余额监控",
                f"使用 Claude 号池：{self.claude_active_pool.get()}；只更新 {self.claude_state_path.get().strip()}。\n",
                target="claude",
            )
            self._start_process("Claude余额监控", self._claude_monitor_args(), replace=True, log_target="claude")

        if not needs_registration:
            return
        self._run_claude_registration_jobs_sequence()

    def install_local_proxy_profile(self) -> None:
        self._start_process("创建本地轮换供应商", self._install_proxy_profile_args(), replace=True)

    def start_local_proxy(self) -> None:
        self._start_process("本地轮换代理", self._local_proxy_args(), replace=True)

    def test_local_proxy(self) -> None:
        self.balance_status.set("正在向本地轮换代理发送 hello（gpt-5.6-sol）…")
        self._start_process("本地代理测试", self._test_local_proxy_args(), replace=True)

    def run_claude_registration(self) -> None:
        try:
            if self._email_mode_value() == "direct":
                domain = account_service.normalize_email_domain(self.email_domain.get())
                self.email_domain.set(domain)
            self._current_site_profile()
            if not self.claude_key_name.get().strip():
                raise site_profiles.SiteError("Claude Key 名称不能为空。")
            if self._email_mode_value() == "receiver":
                if not self.save_email_receiver_settings(show_message=False):
                    raise site_profiles.SiteError(self.receiver_status.get())
                self._require_receiver_test()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.stop_all_requested = False
        threading.Thread(target=self._run_claude_registration_jobs_sequence, daemon=True).start()

    def load_ccswitch_source_model(self) -> None:
        try:
            db_path = Path(self.ccswitch_db_path.get().strip() or ccswitch_sync.DEFAULT_CCSWITCH_DB)
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            try:
                source = ccswitch_sync.source_provider_row(con, self.ccswitch_source_provider_name.get().strip() or ccswitch_sync.DEFAULT_SOURCE_PROVIDER_NAME)
                settings = ccswitch_sync.load_json_object(source["settings_config"]) if source is not None else {}
                model = ccswitch_sync.derive_model(settings)
            finally:
                con.close()
            self.claude_model.set(model)
            self.claude_status.set(f"已读取 CCSwitch 参考供应商默认模型：{model}")
        except Exception as exc:
            self.claude_status.set(f"读取 CCSwitch 默认模型失败：{exc}")
            self._queue_process_log("CCSwitch", f"读取默认模型失败: {exc}\n", target="claude")

    def install_ccswitch_claude_provider(self) -> None:
        self._start_process("创建CCSwitch本地供应商", self._ccswitch_claude_args(), replace=True, log_target="claude")

    def start_claude_monitor(self) -> None:
        self.claude_status.set(f"Claude 余额监控启动中：号池 {self.claude_active_pool.get()}")
        self._start_process("Claude余额监控", self._claude_monitor_args(), replace=True, log_target="claude")

    def start_claude_local_proxy(self) -> None:
        self.claude_status.set(f"Claude 本地代理启动中：{self._claude_base_url()}")
        self._start_process("Claude本地轮换代理", self._claude_local_proxy_args(), replace=True, log_target="claude")

    def stop_claude_local_proxy(self) -> None:
        self.stop_process("Claude本地轮换代理")
        self.claude_status.set("Claude 本地代理已请求停止。")

    def stop_claude_monitor(self) -> None:
        self.stop_process("Claude余额监控")
        self.claude_status.set("Claude 余额监控已请求停止。")

    def _run_batch_then_monitor(self) -> None:
        if "批量注册" in self.running_processes and self.running_processes["批量注册"].poll() is None:
            messagebox.showwarning(APP_TITLE, "批量注册已经在运行。")
            return
        cmd = worker_base_command() + self._batch_args(disable_codexpp_sync=True)
        self.log_queue.put(f"\n[注册后监控] 启动注册: {' '.join(cmd)}\n")

        def run_sequence() -> None:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.Popen(
                cmd,
                cwd=str(app_dir()),
                env=self._proxy_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
            self.running_processes["批量注册"] = process
            assert process.stdout is not None
            for line in process.stdout:
                self.log_queue.put(f"[批量注册] {line}")
            code = process.wait()
            self.running_processes.pop("批量注册", None)
            self.log_queue.put(f"[批量注册] 已结束，退出码={code}\n")
            has_accounts = account_glob_has_files(self.account_pattern.get().strip())
            if not self.stop_all_requested and (code == 0 or has_accounts):
                if code == 0:
                    self.log_queue.put("[注册后监控] 注册成功，启动余额监控。\n")
                else:
                    self.log_queue.put("[注册后监控] 注册有失败，但检测到已有账号 JSON，继续启动余额监控。\n")
                self._start_process("余额监控", self._monitor_args(local_proxy_mode=True), replace=True)
            elif code == 0:
                self.log_queue.put("[注册后监控] 已请求停止全部任务，跳过启动余额监控。\n")
            elif not has_accounts:
                self.log_queue.put("[注册后监控] 注册失败且没有找到账号 JSON，未启动余额监控。\n")
            else:
                self.log_queue.put("[注册后监控] 注册失败，未启动余额监控。\n")

        threading.Thread(target=run_sequence, daemon=True).start()

    def run_group_switch(self) -> None:
        self.stop_all_requested = False
        self._start_process("批量切分组", self._switch_args())

    def refresh_group_choices(self) -> None:
        threading.Thread(target=self._refresh_group_choices_worker, daemon=True).start()

    def _refresh_group_choices_worker(self) -> None:
        name = "刷新分组"
        cmd = worker_base_command() + self._list_groups_args()
        self.log_queue.put(f"\n[{name}] 执行: {' '.join(cmd)}\n")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(
                cmd,
                cwd=str(app_dir()),
                env=self._process_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        except Exception as exc:
            self.log_queue.put(f"[{name}] 启动失败: {exc}\n")
            return

        groups: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            self.log_queue.put(f"[{name}] {line}")
            marker = " name="
            if line.startswith("[GROUP]") and marker in line:
                value = line.split(marker, 1)[1].strip()
                for sep in (" model=", " multiplier="):
                    if sep in value:
                        value = value.split(sep, 1)[0].strip()
                if value and value not in groups:
                    groups.append(value)
        code = process.wait()
        if groups:
            self.after(0, lambda values=groups: self._set_group_choices(values))
            self.log_queue.put(f"[{name}] 已更新下拉分组，共 {len(groups)} 个。\n")
        self.log_queue.put(f"[{name}] 已结束，退出码={code}\n")

    def _parse_key_value_line(self, line: str) -> dict[str, str]:
        # Worker values are JSON-quoted.  ``shlex`` treats the quote after
        # ``path=`` as ordinary text, so a Windows path containing spaces was
        # truncated at e.g. ``...\\Omini Open Ai``.  Parse the key/value wire
        # format directly and let json restore backslashes and Unicode.
        values: dict[str, str] = {}
        pattern = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=(?:"((?:\\.|[^"\\])*)"|(\S+))')
        for match in pattern.finditer(line):
            key = match.group(1)
            quoted, bare = match.group(2), match.group(3)
            if quoted is None:
                values[key] = bare or ""
                continue
            try:
                values[key] = json.loads(f'"{quoted}"')
            except json.JSONDecodeError:
                values[key] = quoted
        return values

    def _clear_balance_rows(self) -> None:
        if self.balance_tree is None:
            return
        for item in self.balance_tree.get_children():
            self.balance_tree.delete(item)
        self.account_item_paths.clear()
        self._set_detail_text(self.balance_detail_text, "")

    def _account_path_key(self, path: Path) -> str:
        try:
            return str(path.resolve())
        except OSError:
            return str(path.absolute())

    def _update_checked_account_count(self) -> None:
        self.checked_account_paths = {value for value in self.checked_account_paths if Path(value).exists()}
        self.checked_account_count.set(f"已勾选 {len(self.checked_account_paths)} 个")

    def _render_account_checks(self) -> None:
        if self.balance_tree is None:
            return
        for item, path in self.account_item_paths.items():
            if self.balance_tree.exists(item):
                self.balance_tree.item(item, text="☑" if self._account_path_key(path) in self.checked_account_paths else "☐")
        self._update_checked_account_count()
        self._recalculate_balance_summary_from_table()

    def _toggle_account_item(self, item: str) -> None:
        path = self.account_item_paths.get(item)
        if path is None:
            return
        key = self._account_path_key(path)
        if key in self.checked_account_paths:
            self.checked_account_paths.remove(key)
        else:
            self.checked_account_paths.add(key)
        self._render_account_checks()

    def _toggle_account_checkbox_click(self, event: tk.Event) -> str | None:
        if self.balance_tree is None or self.balance_tree.identify_column(event.x) != "#0":
            return None
        item = self.balance_tree.identify_row(event.y)
        if item:
            self._toggle_account_item(item)
        return "break"

    def _toggle_account_checkbox_space(self, _event: tk.Event) -> str:
        if self.balance_tree is not None:
            items = list(self.balance_tree.selection())
            if not items and self.balance_tree.focus():
                items = [self.balance_tree.focus()]
            for item in items:
                self._toggle_account_item(item)
        return "break"

    def select_all_visible_accounts(self) -> None:
        for path in self.account_item_paths.values():
            self.checked_account_paths.add(self._account_path_key(path))
        self._render_account_checks()

    def invert_visible_account_selection(self) -> None:
        for path in self.account_item_paths.values():
            key = self._account_path_key(path)
            if key in self.checked_account_paths:
                self.checked_account_paths.remove(key)
            else:
                self.checked_account_paths.add(key)
        self._render_account_checks()

    def clear_account_selection(self) -> None:
        self.checked_account_paths.clear()
        self._render_account_checks()

    def _to_float(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _route_label_for_path(self, path: Path) -> str:
        parent = path.parent
        for key in CLAUDE_ROUTE_KEYS:
            try:
                if parent.resolve() == claude_route_dir(key).resolve():
                    return CLAUDE_ROUTE_LABELS[key]
            except OSError:
                continue
        for key in ROUTE_KEYS:
            try:
                if parent.resolve() == route_dir(key).resolve():
                    return ROUTE_LABELS[key]
            except OSError:
                continue
        try:
            if parent.resolve() == accounts_root().resolve():
                return "unassigned"
        except OSError:
            pass
        return parent.name or "unassigned"

    def _recalculate_balance_summary_from_table(self, status_prefix: str | None = None) -> None:
        if self.balance_tree is None:
            return
        total = 0
        usable = 0
        failed = 0
        remaining = 0.0
        consumed_total = 0.0
        failure_markers = (
            "失败",
            "error",
            "token已过期",
            "自动刷新/登录失败",
            "无本地余额",
            "load failed",
            "query failed",
        )
        for item in self.balance_tree.get_children():
            path = self.account_item_paths.get(item)
            if path is None or self._account_path_key(path) not in self.checked_account_paths:
                continue
            values = self.balance_tree.item(item, "values")
            if len(values) < 8:
                continue
            total += 1
            balance = self._to_float(values[6])
            consumed = self._to_float(values[7])
            status = str(values[8])
            if balance is not None:
                remaining += balance
                if balance > 0:
                    usable += 1
            if consumed is not None:
                consumed_total += consumed
            status_lower = status.lower()
            if any(marker in status_lower for marker in failure_markers):
                failed += 1
        self.balance_total_accounts.set(str(total))
        successful = max(total - failed, 0)
        self.balance_usable_accounts.set(f"{successful} / 失败 {failed}" if failed else str(successful))
        self.balance_remaining_total.set(f"{remaining:.6f}")
        self.balance_consumed_total.set(f"{consumed_total:.6f}")
        if status_prefix:
            self.balance_status.set(f"{status_prefix}：已勾选 {total} 个账号。")

    def _balance_row_values_from_file(self, path: Path) -> tuple[str, str, str, str, str, str, str, str, str]:
        data = account_service.read_account(path)
        registration = data.get("registration") if isinstance(data, dict) else None
        user = registration.get("user") if isinstance(registration, dict) else None
        email = str(data.get("email") or (user or {}).get("email") or path.stem)
        site = data.get("site") if isinstance(data.get("site"), dict) else {}
        site_name = str(site.get("name") or site.get("id") or "XHHAI")
        kind = str(data.get("account_kind") or "codex")
        route_raw = str(data.get("route") or self._route_label_for_path(path))
        if kind == "claude":
            route = CLAUDE_ROUTE_LABELS.get(route_raw, route_raw)
        else:
            route = ROUTE_LABELS.get(route_raw, route_raw)
        has_key = bool(str(data.get("api_key") or (data.get("key") or {}).get("key") or ""))
        key_status = "已有Key" if has_key else "待添加Key"
        group = data.get("group") if isinstance(data.get("group"), dict) else {}
        key_record = data.get("key") if isinstance(data.get("key"), dict) else {}
        group_name = str(group.get("name") or key_record.get("group_name") or "").strip()
        group_id = str(group.get("id") or key_record.get("group_id") or "").strip()
        if group_name:
            group_text = f"{group_name} [{group_id}]" if group_id else group_name
        elif has_key:
            group_text = "未记录"
        else:
            group_text = "—"
        provisioning = data.get("provisioning") if isinstance(data.get("provisioning"), dict) else {}
        provision_status = str(provisioning.get("status") or "")
        provision_stage = str(provisioning.get("stage") or "")
        provision_error = str(provisioning.get("error") or "")
        cached_balance, source = key_manager.read_account_cached_balance(path)
        initial = key_manager.read_account_initial_balance(path)
        if cached_balance is None:
            balance_text = ""
            consumed_text = ""
            status = "待添加 Key" if not has_key else "无本地余额"
        else:
            if initial is None:
                initial = cached_balance
            consumed = max(initial - cached_balance, 0.0)
            balance_text = f"{cached_balance:.6f}"
            consumed_text = f"{consumed:.6f}"
            cache_status = "本地缓存" if source.startswith("balance_cache") else "初始余额"
            status = f"待添加 Key / {cache_status}" if not has_key else cache_status
        if provision_status in {"partial", "failed"}:
            status = f"开户{provision_status}: {provision_stage}" + (f" - {provision_error[:100]}" if provision_error else "")
        return site_name, kind, route, email, key_status, group_text, balance_text, consumed_text, status

    def load_local_balance_snapshot(self) -> None:
        if self.balance_tree is None:
            return
        self._clear_balance_rows()
        self.account_item_paths.clear()
        patterns = self._account_patterns_for_pool(self.balance_pool.get())
        paths = group_switch.resolve_account_paths(patterns)
        known_sites = {str(item.get("name")) for item in self.site_store.get("sites", [])}
        known_routes = {*route_pool_labels(), *claude_pool_labels()}
        for path in paths:
            try:
                row = self._balance_row_values_from_file(path)
            except Exception as exc:
                self.log_queue.put(f"[账号载入] {path}: {exc}\n")
                continue
            known_sites.add(row[0])
            known_routes.add(row[2])
            if self.account_site_filter.get() != "全部网站" and row[0] != self.account_site_filter.get():
                continue
            if self.account_kind_filter.get() != "全部类型" and row[1] != self.account_kind_filter.get():
                continue
            if self.account_route_filter.get() != "全部路线" and row[2] != self.account_route_filter.get():
                continue
            if self.account_key_filter.get() != "全部Key状态" and row[4] != self.account_key_filter.get():
                continue
            checked = self._account_path_key(path) in self.checked_account_paths
            item = self.balance_tree.insert("", tk.END, text="☑" if checked else "☐", values=row)
            self.account_item_paths[item] = path
        if self.account_site_filter_box is not None:
            self.account_site_filter_box.configure(values=("全部网站", *sorted(known_sites)))
        if self.account_route_filter_box is not None:
            self.account_route_filter_box.configure(values=("全部路线", *sorted(known_routes)))
        self._update_checked_account_count()
        self._recalculate_balance_summary_from_table("本地快照")

    def _selected_account_paths(self) -> list[Path]:
        self._update_checked_account_count()
        return sorted((Path(value) for value in self.checked_account_paths), key=lambda path: str(path).casefold())

    def _selected_site_and_paths(self) -> tuple[dict[str, Any], list[Path]]:
        paths = self._selected_account_paths()
        if not paths:
            raise site_profiles.SiteError("请先在账号表格中勾选账号。")
        site_ids = {account_service.account_site_id(account_service.read_account(path)) for path in paths}
        if len(site_ids) != 1:
            raise site_profiles.SiteError("一次只能处理同一个号池网站的账号，请先使用网站筛选。")
        profile = site_profiles.site_by_id(self.site_store, next(iter(site_ids)))
        return profile, paths

    def _require_registered_accounts(self, paths: list[Path]) -> None:
        pending = []
        for path in paths:
            data = account_service.read_account(path)
            if not str((data.get("registration") or {}).get("access_token") or "").strip():
                pending.append(path)
        if pending:
            raise site_profiles.SiteError(
                f"已选账号中有 {len(pending)} 个尚未完成注册，请先点击“重试自动开户”。"
            )

    def rebind_selected_accounts(self) -> None:
        paths = self._selected_account_paths()
        if not paths:
            messagebox.showinfo(APP_TITLE, "请先在账号表格中勾选账号。")
            return
        try:
            source_site_ids = {account_service.account_site_id(account_service.read_account(path)) for path in paths}
            if len(source_site_ids) != 1:
                raise site_profiles.SiteError("绑定网站一次只能处理同一来源网站的账号。")
            profile = self._current_site_profile()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        if not messagebox.askyesno(APP_TITLE, f"把 {len(paths)} 个账号绑定到网站“{profile['name']}”？"):
            return
        ok_count = fail_count = 0
        for path in paths:
            try:
                account_service.rebind_account_site(profile, path)
                ok_count += 1
            except Exception as exc:
                fail_count += 1
                self.log_queue.put(f"[账号绑定] {path}: {exc}\n")
        self.load_local_balance_snapshot()
        self.balance_status.set(f"账号绑定完成：成功 {ok_count}，失败 {fail_count}。")

    def retry_selected_provisioning(self) -> None:
        try:
            profile, paths = self._selected_site_and_paths()
            if not self.save_email_receiver_settings(show_message=False):
                raise site_profiles.SiteError(self.receiver_status.get())
            decision = site_profiles.resolve_proxy(
                self.proxy_mode.get(), self.proxy_host.get().strip(), int(self.proxy_port.get().strip())
            )
            config = email_receiver.load_receiver_config(self.receiver_config_path)
            codex_key_name = self.key_name.get().strip() or "codex"
            claude_key_name = self.claude_key_name.get().strip() or "claude"
        except Exception as exc:
            messagebox.showinfo(APP_TITLE, str(exc))
            return
        self.balance_status.set(f"正在重试 {len(paths)} 个账号的自动开户流程…")

        def worker() -> None:
            ok_count = fail_count = 0
            for path in paths:
                try:
                    data = account_service.read_account(path)
                    key_name = claude_key_name if data.get("account_kind") == "claude" else codex_key_name
                    account_service.retry_provision(
                        profile, path, key_name=key_name, receiver_config=config,
                        use_proxy=decision.use_proxy, proxy_url=decision.proxy_url,
                    )
                    ok_count += 1
                    self.log_queue.put(f"[重试开户] ok=true path={path}\n")
                except Exception as exc:
                    fail_count += 1
                    self.log_queue.put(f"[重试开户] ok=false path={path} error={account_service.redact_log_text(exc)[:300]}\n")
            self.after(0, self.load_local_balance_snapshot)
            self.after(0, lambda: self.balance_status.set(f"重试完成：成功 {ok_count}，失败 {fail_count}。"))

        threading.Thread(target=worker, daemon=True).start()

    def detect_groups_for_selected_accounts(self) -> None:
        try:
            profile, paths = self._selected_site_and_paths()
            self._require_registered_accounts(paths)
        except Exception as exc:
            messagebox.showinfo(APP_TITLE, str(exc))
            return
        self.balance_status.set(f"正在检测 {len(paths)} 个账号的共同分组...")

        def worker() -> None:
            try:
                decision = site_profiles.resolve_proxy(self.proxy_mode.get(), self.proxy_host.get().strip(), int(self.proxy_port.get().strip()))
                if decision.note:
                    self.log_queue.put(f"[检测分组] {decision.note}\n")
                groups = account_service.common_groups(
                    profile, paths, use_proxy=decision.use_proxy, proxy_url=decision.proxy_url
                )
                choices = [f"{item['name']} [{item['id']}]" for item in groups]
                self.manual_group_choices = {label: item for label, item in zip(choices, groups)}

                def update() -> None:
                    self.manual_group_box.configure(values=choices)
                    self.manual_group.set(choices[0] if choices else "")
                    self.balance_status.set(f"共同可用分组：{len(choices)} 个。" if choices else "所选账号没有共同可用分组。")

                self.after(0, update)
            except Exception as exc:
                message = account_service.redact_log_text(exc)
                self.after(0, lambda message=message: self.balance_status.set(f"分组检测失败：{message}"))

        threading.Thread(target=worker, daemon=True).start()

    def add_keys_to_selected_accounts(self) -> None:
        try:
            profile, paths = self._selected_site_and_paths()
            self._require_registered_accounts(paths)
            group = self.manual_group_choices.get(self.manual_group.get())
            if not group:
                raise site_profiles.SiteError("请先检测并选择共同分组。")
            key_name = self.manual_key_name.get().strip() or DEFAULT_KEY_NAME
        except Exception as exc:
            messagebox.showinfo(APP_TITLE, str(exc))
            return
        self.balance_status.set(f"正在为 {len(paths)} 个账号创建 Key...")

        def worker() -> None:
            try:
                decision = site_profiles.resolve_proxy(self.proxy_mode.get(), self.proxy_host.get().strip(), int(self.proxy_port.get().strip()))
                if decision.note:
                    self.log_queue.put(f"[创建Key] {decision.note}\n")
                ok_count, skipped, failed = account_service.add_keys(
                    profile,
                    paths,
                    group_id=group["id"],
                    group_name=str(group["name"]),
                    key_name=key_name,
                    include_existing=self.include_existing_key.get(),
                    use_proxy=decision.use_proxy,
                    proxy_url=decision.proxy_url,
                    delay_seconds=max(float(self.group_delay_seconds.get().strip() or 0), 0.0),
                )
                self.after(0, self.load_local_balance_snapshot)
                self.after(0, lambda: self.balance_status.set(f"Key 创建完成：成功 {ok_count}，跳过 {skipped}，失败 {failed}。"))
            except Exception as exc:
                message = account_service.redact_log_text(exc)
                self.after(0, lambda message=message: self.balance_status.set(f"Key 创建失败：{message}"))

        threading.Thread(target=worker, daemon=True).start()

    def _add_balance_row(self, values: dict[str, str]) -> None:
        if self.balance_tree is None:
            return
        ok = values.get("ok") == "true"
        balance = values.get("balance", "")
        consumed = values.get("consumed", "")
        error = values.get("error", "失败")
        if values.get("keyless") == "true":
            status = values.get("status", "待添加 Key")
        elif not ok and "token" in error.lower() and "expired" in error.lower():
            status = "Token已过期，自动刷新/登录失败"
        else:
            status = "正常" if ok else error
        path = values.get("path", "")
        if not ok and path and is_token_error_text(error):
            self.balance_token_failed_paths.add(str(Path(path).resolve()))
        if path:
            try:
                account_path = Path(path)
                row = list(self._balance_row_values_from_file(account_path))
                if ok:
                    row[6] = balance
                    row[7] = consumed
                row[8] = status
                path_key = self._account_path_key(account_path)
                existing = next(
                    (
                        item for item, item_path in self.account_item_paths.items()
                        if self.balance_tree.exists(item) and self._account_path_key(item_path) == path_key
                    ),
                    None,
                )
                checked = path_key in self.checked_account_paths
                if existing:
                    self.balance_tree.item(existing, text="☑" if checked else "☐", values=tuple(row))
                else:
                    item = self.balance_tree.insert("", tk.END, text="☑" if checked else "☐", values=tuple(row))
                    self.account_item_paths[item] = account_path
            except Exception as exc:
                self.log_queue.put(
                    f"[账号额度] 表格更新失败 path={path} "
                    f"error={account_service.redact_log_text(exc)}\n"
                )

    def _set_balance_summary(self, values: dict[str, str]) -> None:
        total = values.get("total", "0")
        self.balance_total_accounts.set(total)
        usable = values.get("usable", "0")
        failed = values.get("failed", "0")
        try:
            successful = max(int(total) - int(failed or 0), 0)
        except ValueError:
            successful = 0
        self.balance_usable_accounts.set(str(successful) if failed in {"", "0"} else f"{successful} / 失败 {failed}")
        self.balance_remaining_total.set(values.get("remaining", "0.0000"))
        self.balance_consumed_total.set(values.get("consumed", "0.0000"))
        refreshed = values.get("refreshed_tokens", "0")
        self.balance_status.set(f"查询完成：已勾选 {total}，成功 {successful}，失败 {failed}，正余额账号 {usable}，刷新 token {refreshed}")

    def _clear_claude_balance_rows(self) -> None:
        if self.claude_balance_tree is None:
            return
        for item in self.claude_balance_tree.get_children():
            self.claude_balance_tree.delete(item)
        self.claude_item_paths.clear()
        self._set_detail_text(self.claude_detail_text, "")

    def _recalculate_claude_balance_summary_from_table(self, status_prefix: str | None = None) -> None:
        if self.claude_balance_tree is None:
            return
        stats = {key: {"total": 0, "usable": 0, "remaining": 0.0} for key in CLAUDE_ROUTE_KEYS}
        total = 0
        usable = 0
        remaining = 0.0
        for item in self.claude_balance_tree.get_children():
            values = self.claude_balance_tree.item(item, "values")
            if len(values) < 6:
                continue
            total += 1
            route_label = str(values[0])
            balance = self._to_float(values[2])
            if balance is not None:
                remaining += balance
                if balance > 0:
                    usable += 1
            route_key = self._claude_route_key_from_pool(route_label)
            if route_key:
                stats[route_key]["total"] += 1
                if balance is not None:
                    stats[route_key]["remaining"] += balance
                    if balance > 0:
                        stats[route_key]["usable"] += 1
        for key, item in stats.items():
            self.claude_balance_summaries[key].set(
                f"{int(item['total'])}个 / {int(item['usable'])}可用 / 剩余{item['remaining']:.4f}"
            )
        if status_prefix:
            self.claude_status.set(f"{status_prefix}：已载入 {total} 个 Claude 账号，可用 {usable}，剩余 {remaining:.4f}。")

    def load_claude_local_balance_snapshot(self) -> None:
        if self.claude_balance_tree is None:
            return
        self._clear_claude_balance_rows()
        paths = group_switch.resolve_account_paths(self._claude_account_patterns_for_pool(self.claude_balance_pool.get()))
        for path in paths:
            row = self._balance_row_values_from_file(path)
            initial = key_manager.read_account_initial_balance(path)
            initial_text = f"{initial:.6f}" if initial is not None else ""
            item = self.claude_balance_tree.insert(
                "", tk.END, values=(row[2], row[3], row[6], initial_text, row[7], row[8])
            )
            self.claude_item_paths[item] = path
        self._recalculate_claude_balance_summary_from_table("Claude 本地快照")

    def _add_claude_balance_row(self, values: dict[str, str]) -> None:
        if self.claude_balance_tree is None:
            return
        ok = values.get("ok") == "true"
        path = values.get("path", "")
        route_label = self._route_label_for_path(Path(path)) if path else ""
        status = values.get("status", "正常") if ok else values.get("error", "失败")
        item = self.claude_balance_tree.insert(
            "",
            tk.END,
            values=(
                route_label,
                values.get("email", ""),
                values.get("balance", ""),
                values.get("initial", ""),
                values.get("consumed", ""),
                status,
            ),
        )
        if path:
            self.claude_item_paths[item] = Path(path)
        self._recalculate_claude_balance_summary_from_table("正在刷新 Claude")

    def refresh_claude_account_tokens(self, pool: str | None = None) -> None:
        patterns = self._claude_account_patterns_for_pool(pool or self.claude_balance_pool.get())
        paths = group_switch.resolve_account_paths(patterns)
        if not paths:
            self.claude_status.set("没有找到 Claude 账号 JSON。")
            return
        label = "Claude全部Token" if (pool or self.claude_balance_pool.get()) == CLAUDE_POOL_ALL else f"{pool or self.claude_balance_pool.get()} Token"
        threading.Thread(target=self._refresh_account_tokens_worker, args=(paths, label, "claude"), daemon=True).start()

    def refresh_claude_account_balances(self, pool: str | None = None) -> None:
        selected_pool = pool or self.claude_balance_pool.get()
        if not self._filtered_account_paths(selected_pool, account_kind="claude", keyed_only=False):
            self.claude_status.set("当前 Claude 号池没有账号。")
            return
        self.claude_status.set(f"正在刷新{selected_pool}额度...")
        self._clear_claude_balance_rows()
        threading.Thread(target=self._refresh_claude_account_balances_worker, args=(selected_pool,), daemon=True).start()

    def _refresh_claude_account_balances_worker(self, pool: str) -> None:
        name = "Claude账号额度"
        cmd = worker_base_command() + self._claude_account_balances_args(pool=pool)
        self._queue_process_log(name, f"执行: {' '.join(cmd)}\n", target="claude")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(
                cmd,
                cwd=str(app_dir()),
                env=self._process_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        except Exception as exc:
            self.after(0, lambda: self.claude_status.set(f"刷新失败：{exc}"))
            self._queue_process_log(name, f"启动失败: {exc}\n", target="claude")
            return

        assert process.stdout is not None
        for line in process.stdout:
            self._queue_process_log(name, line, target="claude")
            text = line.strip()
            if text.startswith("[BALANCE]"):
                values = self._parse_key_value_line(text)
                self.after(0, lambda values=values: self._add_claude_balance_row(values))
            elif text.startswith("[BALANCE_WAIT]"):
                values = self._parse_key_value_line(text)
                seconds = values.get("seconds", self.balance_query_interval.get())
                self.after(0, lambda seconds=seconds: self.claude_status.set(f"等待 {seconds} 秒后查询下一个 Claude 账号..."))
            elif text.startswith("[SUMMARY]"):
                values = self._parse_key_value_line(text)
                self.after(0, lambda values=values: self.claude_status.set(
                    f"Claude 刷新完成：总账号 {values.get('total', '0')}，可用 {values.get('usable', '0')}，失败 {values.get('failed', '0')}"
                ))
        code = process.wait()
        if code != 0:
            self.after(0, lambda: self.claude_status.set("Claude 额度刷新完成，但有账号查询失败；请看 Claude 日志。"))
        self._queue_process_log(name, f"已结束，退出码={code}\n", target="claude")

    def refresh_failed_account_tokens(self) -> None:
        paths = sorted(Path(p) for p in self.balance_token_failed_paths if p)
        if not paths:
            self.balance_status.set("没有上次余额查询留下的 Token 失败账号。")
            return
        threading.Thread(target=self._refresh_account_tokens_worker, args=(paths, "失败Token"), daemon=True).start()

    def refresh_current_pool_tokens(self) -> None:
        paths = self._selected_account_paths()
        if not paths:
            self.balance_status.set("请先勾选要刷新 Token 的账号。")
            return
        threading.Thread(target=self._refresh_account_tokens_worker, args=(paths, "勾选账号Token"), daemon=True).start()

    def refresh_all_account_tokens(self, pool: str | None = None) -> None:
        patterns = self._account_patterns_for_pool(pool or self.balance_pool.get())
        paths = group_switch.resolve_account_paths(patterns)
        if not paths:
            self.balance_status.set("没有找到账号 JSON。")
            return
        label = "全部Token" if (pool or self.balance_pool.get()) == POOL_ALL else f"{pool or self.balance_pool.get()} Token"
        threading.Thread(target=self._refresh_account_tokens_worker, args=(paths, label), daemon=True).start()

    def _refresh_account_tokens_worker(self, paths: list[Path], label: str, log_target: str = "main") -> None:
        total = len(paths)
        ok_count = 0
        fail_count = 0
        try:
            decision = site_profiles.resolve_proxy(
                self.proxy_mode.get(), self.proxy_host.get().strip(), int(self.proxy_port.get().strip())
            )
        except Exception as exc:
            self._queue_process_log(f"刷新{label}", f"代理预检失败: {exc}\n", target=log_target)
            return
        if decision.note:
            self._queue_process_log(f"刷新{label}", decision.note + "\n", target=log_target)
        if log_target == "claude":
            self.after(0, lambda: self.claude_status.set(f"正在刷新{label}：0/{total}"))
        else:
            self.after(0, lambda: self.balance_status.set(f"正在刷新{label}：0/{total}"))
        self._queue_process_log(f"刷新{label}", f"开始，共 {total} 个账号。\n", target=log_target)

        for index, path in enumerate(paths, start=1):
            try:
                data = account_service.read_account(path)
                profile = site_profiles.site_by_id(self.site_store, account_service.account_site_id(data))
                source = account_service.refresh_account_token(
                    profile, path, use_proxy=decision.use_proxy, proxy_url=decision.proxy_url
                )
                email = str(data.get("email") or path.stem)
                ok_count += 1
                self._queue_process_log(f"刷新{label}", f"[OK] {email} token={source} path={path}\n", target=log_target)
                self.balance_token_failed_paths.discard(str(path.resolve()))
            except Exception as exc:
                fail_count += 1
                self._queue_process_log(
                    f"刷新{label}", f"[FAIL] {path}: {account_service.redact_log_text(exc)}\n", target=log_target
                )
            if log_target == "claude":
                self.after(
                    0,
                    lambda index=index, ok_count=ok_count, fail_count=fail_count: self.claude_status.set(
                        f"正在刷新{label}：{index}/{total}，成功 {ok_count}，失败 {fail_count}"
                    ),
                )
            else:
                self.after(
                    0,
                    lambda index=index, ok_count=ok_count, fail_count=fail_count: self.balance_status.set(
                        f"正在刷新{label}：{index}/{total}，成功 {ok_count}，失败 {fail_count}"
                    ),
                )

        if log_target == "claude":
            self.after(0, lambda: self.claude_status.set(f"刷新{label}完成：成功 {ok_count}，失败 {fail_count}。"))
        else:
            self.after(0, lambda: self.balance_status.set(f"刷新{label}完成：成功 {ok_count}，失败 {fail_count}。可重新查询当前号池复查。"))
        self._queue_process_log(f"刷新{label}", f"完成。成功={ok_count}, 失败={fail_count}\n", target=log_target)

    def refresh_account_balances(self, pool: str | None = None) -> None:
        paths = self._selected_account_paths()
        if not paths:
            self.balance_status.set("请先勾选要查询余额的账号。")
            return
        query_paths: list[Path] = []
        retry_paths: list[Path] = []
        for path in paths:
            try:
                data = account_service.read_account(path)
                (retry_paths if account_service.needs_registration_retry(data) else query_paths).append(path)
            except Exception:
                # Let the worker report malformed or unreadable files with its
                # normal per-account error line.
                query_paths.append(path)
        if not query_paths:
            self.balance_status.set(
                f"所选 {len(retry_paths)} 个账号尚未完成注册；请先点击“重试自动开户”。"
            )
            return
        suffix = f"；跳过 {len(retry_paths)} 个未完成开户账号" if retry_paths else ""
        self.balance_status.set(f"正在查询已勾选的 {len(query_paths)} 个账号余额{suffix}...")
        self.balance_token_failed_paths.clear()
        threading.Thread(
            target=self._refresh_account_balances_worker,
            args=("已勾选账号", query_paths, len(retry_paths)),
            daemon=True,
        ).start()

    def _refresh_account_balances_worker(self, pool: str, paths: list[Path], skipped_unregistered: int = 0) -> None:
        name = "账号额度"
        cmd = worker_base_command() + self._account_balances_args(paths=paths)
        self.log_queue.put(f"\n[{name}] 执行: {' '.join(cmd)}\n")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(
                cmd,
                cwd=str(app_dir()),
                env=self._process_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        except Exception as exc:
            self.balance_status.set(f"刷新失败：{exc}")
            self.log_queue.put(f"[{name}] 启动失败: {exc}\n")
            return

        assert process.stdout is not None
        for line in process.stdout:
            self.log_queue.put(f"[{name}] {line}")
            text = line.strip()
            if text.startswith("[BALANCE]"):
                values = self._parse_key_value_line(text)
                self.after(0, lambda values=values: self._add_balance_row(values))
            elif text.startswith("[BALANCE_WAIT]"):
                values = self._parse_key_value_line(text)
                seconds = values.get("seconds", self.balance_query_interval.get())
                self.after(0, lambda seconds=seconds: self.balance_status.set(f"等待 {seconds} 秒后查询下一个账号..."))
            elif text.startswith("[SUMMARY]"):
                values = self._parse_key_value_line(text)
                self.after(0, lambda values=values: self._set_balance_summary(values))
        code = process.wait()
        if code != 0:
            self.after(0, lambda: self.balance_status.set("刷新完成，但有账号查询失败；请看运行日志里的 [BALANCE] 失败行。"))
        elif skipped_unregistered:
            self.after(
                0,
                lambda: self.balance_status.set(
                    f"查询完成；已跳过 {skipped_unregistered} 个尚未完成注册的账号，请先重试自动开户。"
                ),
            )
        self.log_queue.put(f"[{name}] 已结束，退出码={code}\n")

    def stop_monitor(self) -> None:
        self.stop_process("余额监控")

    def stop_local_proxy(self) -> None:
        self.stop_process("本地轮换代理")

    def open_accounts_dir(self) -> None:
        path = accounts_root()
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def open_log_file(self) -> None:
        path = Path(self.log_path.get().strip())
        if not path.exists():
            messagebox.showinfo(APP_TITLE, f"日志文件还不存在:\n{path}")
            return
        os.startfile(path)

    def open_claude_log_file(self) -> None:
        path = Path(self.claude_log_path.get().strip())
        if not path.exists():
            messagebox.showinfo(APP_TITLE, f"Claude 日志文件还不存在:\n{path}")
            return
        os.startfile(path)

    def _on_close(self) -> None:
        self._save_settings()
        self.save_email_receiver_settings(show_message=False)
        running = [name for name, proc in self.running_processes.items() if proc.poll() is None]
        if running:
            if not messagebox.askyesno(APP_TITLE, "还有任务在运行，是否停止并退出？"):
                return
            for name in running:
                self.stop_process(name)
        self.destroy()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        return run_worker(sys.argv[2:])
    app = ToolboxApp()
    if os.environ.get("OMINI_TOOLBOX_SMOKE_TEST") == "1":
        app.update_idletasks()
        app.destroy()
        return 0
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
