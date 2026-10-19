#!/usr/bin/env python3
"""
Switch existing xhhai account JSON files to another group.

The script uses the same public APIs as the registration tools:
- GET  /api/v1/groups/available
- POST /api/v1/keys

It creates a new key in the target group, then updates the local account JSON's
api_key/group/key fields. The old file is backed up before writing.
"""

from __future__ import annotations

import argparse
import glob
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from omini_api.xhhai_create_codex_key import (
    DEFAULT_GROUP_NAME,
    DEFAULT_KEY_NAME,
    create_key,
    extract_api_key,
    get_available_groups,
    pick_group,
)
from omini_api.xhhai_register import DEFAULT_API_BASE, ApiError, request_json
from omini_api.xhhai_file_utils import write_json_atomic


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


_configure_utf8_stdio()


DEFAULT_ACCOUNT_PATTERNS = [
    "xhhai_codex_account*.json",
    str(Path("accounts") / "xhhai_codex_account*.json"),
    str(Path("accounts") / "*" / "xhhai_codex_account*.json"),
]


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ApiError(f"{path} is not a JSON object.")
    return data


def resolve_account_paths(patterns: list[str]) -> list[Path]:
    paths: list[Path] = []
    for pattern in patterns:
        matches = sorted(Path(item) for item in glob.glob(pattern))
        if matches:
            paths.extend(matches)
            continue
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
    return unique


def backup_file(path: Path) -> Path:
    backup = path.with_name(f"{path.name}.bak-{now_stamp()}")
    shutil.copy2(path, backup)
    return backup


def read_cached_balance(data: dict[str, Any]) -> float | None:
    cache = data.get("balance_cache")
    if isinstance(cache, dict):
        for key in ("balance", "current_balance", "remaining"):
            try:
                return float(cache[key])
            except (KeyError, TypeError, ValueError):
                continue
    return None


def account_group_name(data: dict[str, Any]) -> str:
    group = data.get("group")
    if isinstance(group, dict):
        return str(group.get("name") or "")
    return ""


def group_name_matches(actual: str, expected: str) -> bool:
    actual_norm = actual.strip().lower()
    expected_norm = expected.strip().lower()
    return not expected_norm or actual_norm == expected_norm or expected_norm in actual_norm


def target_path_for(path: Path, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    if not target.exists() or target.resolve() == path.resolve():
        return target
    stem = path.stem
    suffix = path.suffix
    return target_dir / f"{stem}_{now_stamp()}{suffix}"


def move_account_json(path: Path, target_dir: Path) -> Path:
    target = target_path_for(path, target_dir)
    if target.resolve() == path.resolve():
        return path
    shutil.move(str(path), str(target))
    return target


def refresh_registration(
    api_base: str,
    registration: dict[str, Any],
    *,
    use_system_proxy: bool,
) -> dict[str, Any]:
    refresh_token = registration.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        return registration

    token_data = request_json(
        "POST",
        f"{api_base}/auth/refresh",
        {"refresh_token": refresh_token},
        op="refresh_account_token",
        use_system_proxy=use_system_proxy,
    )
    if not isinstance(token_data, dict) or not token_data.get("access_token"):
        raise ApiError("Refresh response did not include access_token.")

    updated = dict(registration)
    updated["access_token"] = token_data.get("access_token", registration.get("access_token"))
    updated["refresh_token"] = token_data.get("refresh_token", registration.get("refresh_token"))
    updated["expires_in"] = token_data.get("expires_in", registration.get("expires_in"))
    updated["token_type"] = token_data.get("token_type", registration.get("token_type", "Bearer"))
    if isinstance(token_data.get("user"), dict):
        updated["user"] = token_data["user"]
    return updated


def login_registration(
    api_base: str,
    email: str,
    password: str,
    *,
    use_system_proxy: bool,
) -> dict[str, Any]:
    token_data = request_json(
        "POST",
        f"{api_base}/auth/login",
        {"email": email, "password": password},
        op="login_account",
        use_system_proxy=use_system_proxy,
    )
    if not isinstance(token_data, dict) or not token_data.get("access_token"):
        raise ApiError("Login response did not include access_token.")
    return token_data


def write_registration(path: Path, data: dict[str, Any], registration: dict[str, Any], *, no_backup: bool) -> Path | None:
    data["registration"] = registration
    backup = None if no_backup else backup_file(path)
    write_json_atomic(path, data)
    return backup


def refresh_or_login_registration(
    path: Path,
    data: dict[str, Any],
    api_base: str,
    registration: dict[str, Any],
    *,
    use_system_proxy: bool,
    no_backup: bool,
    persist: bool,
) -> tuple[dict[str, Any], Path | None, str]:
    try:
        updated = refresh_registration(api_base, registration, use_system_proxy=use_system_proxy)
        if updated is registration:
            return registration, None, "access_token"
        backup = write_registration(path, data, updated, no_backup=no_backup) if persist else None
        return updated, backup, "refresh_token"
    except Exception as refresh_exc:
        email = str(data.get("email") or (registration.get("user") or {}).get("email") or "")
        password = str(data.get("password") or "")
        if not email or not password:
            raise refresh_exc
        token_data = login_registration(api_base, email, password, use_system_proxy=use_system_proxy)
        updated = dict(registration)
        updated["access_token"] = token_data.get("access_token", updated.get("access_token"))
        updated["refresh_token"] = token_data.get("refresh_token", updated.get("refresh_token"))
        updated["expires_in"] = token_data.get("expires_in", updated.get("expires_in"))
        updated["token_type"] = token_data.get("token_type", updated.get("token_type", "Bearer"))
        if isinstance(token_data.get("user"), dict):
            updated["user"] = token_data["user"]
        backup = write_registration(path, data, updated, no_backup=no_backup) if persist else None
        return updated, backup, "login"


def load_groups_for_account(
    path: Path,
    *,
    api_base: str,
    use_system_proxy: bool,
    refresh_first: bool,
    no_backup: bool,
    persist_tokens: bool,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], Path | None, str]:
    data = load_json(path)
    registration = data.get("registration")
    if not isinstance(registration, dict):
        raise ApiError(f"{path} has no registration object.")

    token_backup: Path | None = None
    token_source = "access_token"
    if refresh_first:
        registration, token_backup, token_source = refresh_or_login_registration(
            path,
            data,
            api_base,
            registration,
            use_system_proxy=use_system_proxy,
            no_backup=no_backup,
            persist=persist_tokens,
        )

    access_token = registration.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ApiError(f"{path} has no registration.access_token.")

    try:
        groups = get_available_groups(api_base, access_token, use_system_proxy=use_system_proxy)
    except Exception:
        registration, token_backup, token_source = refresh_or_login_registration(
            path,
            data,
            api_base,
            registration,
            use_system_proxy=use_system_proxy,
            no_backup=no_backup,
            persist=persist_tokens,
        )
        access_token = registration.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ApiError(f"{path} has no registration.access_token after token repair.")
        groups = get_available_groups(api_base, access_token, use_system_proxy=use_system_proxy)

    return data, registration, groups, token_backup, token_source


def switch_one(
    path: Path,
    *,
    api_base: str,
    group_name: str,
    key_name: str,
    use_system_proxy: bool,
    refresh_first: bool,
    dry_run: bool,
    no_backup: bool,
    target_dir: Path | None = None,
) -> Path:
    data, registration, groups, token_backup, token_source = load_groups_for_account(
        path,
        api_base=api_base,
        use_system_proxy=use_system_proxy,
        refresh_first=refresh_first and not dry_run,
        no_backup=no_backup,
        persist_tokens=not dry_run,
    )
    access_token = registration.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ApiError(f"{path} has no registration.access_token.")

    group = pick_group(groups, group_name)
    group_id = group.get("id")
    if group_id is None:
        raise ApiError(f"Matched group has no id: {group}")

    old_group = data.get("group") if isinstance(data.get("group"), dict) else {}
    old_key = data.get("key") if isinstance(data.get("key"), dict) else {}
    email = str(data.get("email") or (registration.get("user") or {}).get("email") or path.stem)

    if dry_run:
        print(
            f"[DRY-RUN] {path}: {email} "
            f"{old_group.get('name', '?')} -> {group.get('name')} (id={group_id}) token={token_source}",
            flush=True,
        )
        return path

    key = create_key(
        api_base,
        access_token,
        key_name,
        group_id,
        use_system_proxy=use_system_proxy,
    )
    api_key = extract_api_key(key)

    event = {
        "switched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "create_new_key",
        "previous_group": {
            "id": old_group.get("id"),
            "name": old_group.get("name"),
        },
        "previous_key": {
            "id": old_key.get("id"),
            "name": old_key.get("name"),
        },
        "new_group": {
            "id": group.get("id"),
            "name": group.get("name"),
        },
        "new_key": {
            "id": key.get("id"),
            "name": key.get("name"),
        },
    }

    data["api_key"] = api_key
    data["group"] = group
    data["key"] = key
    data["registration"] = registration
    data["group_switch_last"] = event
    history = data.setdefault("group_switch_history", [])
    if isinstance(history, list):
        history.append(event)

    backup = token_backup if token_backup else (None if no_backup else backup_file(path))
    write_json_atomic(path, data)
    final_path = move_account_json(path, target_dir) if target_dir else path
    backup_text = f" backup={backup}" if backup else ""
    moved_text = f" moved={final_path}" if final_path != path else ""
    print(f"[OK] {path}: {email} -> {group.get('name')} key={key.get('name', key_name)}{backup_text}{moved_text}", flush=True)
    return final_path


def switch_many(
    paths: list[Path],
    *,
    api_base: str,
    group_name: str,
    key_name: str,
    use_system_proxy: bool,
    refresh_first: bool,
    dry_run: bool,
    no_backup: bool,
    batch_size: int = 1,
    delay_seconds: float = 2.0,
    source_group_name: str = "",
    limit: int = 0,
    target_dir: Path | None = None,
    skip_depleted: bool = True,
) -> tuple[int, int]:
    ok_count = 0
    fail_count = 0
    batch_size = max(int(batch_size or 1), 1)
    delay_seconds = max(float(delay_seconds or 0), 0.0)
    limit = max(int(limit or 0), 0)

    selected: list[Path] = []
    skipped_depleted = 0
    skipped_group = 0
    skipped_invalid = 0
    for path in paths:
        try:
            data = load_json(path)
        except Exception as exc:
            skipped_invalid += 1
            print(f"[SKIP] {path}: invalid JSON or unreadable: {exc}", file=sys.stderr, flush=True)
            continue

        if source_group_name and not group_name_matches(account_group_name(data), source_group_name):
            skipped_group += 1
            continue

        cached_balance = read_cached_balance(data)
        if skip_depleted and cached_balance is not None and cached_balance <= 0:
            skipped_depleted += 1
            print(f"[SKIP] {path}: local balance <= 0 ({cached_balance:g})", flush=True)
            continue

        selected.append(path)
        if limit and len(selected) >= limit:
            break

    if skipped_group or skipped_depleted or skipped_invalid:
        print(
            f"[FILTER] selected={len(selected)} skipped_group={skipped_group} "
            f"skipped_depleted={skipped_depleted} skipped_invalid={skipped_invalid}",
            flush=True,
        )
    if not selected:
        print("[FILTER] no account selected for switching.", flush=True)
        return 0, 0

    for index, path in enumerate(selected, start=1):
        try:
            switch_one(
                path,
                api_base=api_base,
                group_name=group_name,
                key_name=key_name,
                use_system_proxy=use_system_proxy,
                refresh_first=refresh_first,
                dry_run=dry_run,
                no_backup=no_backup,
                target_dir=target_dir,
            )
            ok_count += 1
        except Exception as exc:
            fail_count += 1
            print(f"[FAIL] {path}: {exc}", file=sys.stderr, flush=True)

        has_more = index < len(selected)
        if has_more and index % batch_size == 0 and delay_seconds > 0:
            print(f"[WAIT] processed {index}/{len(selected)} accounts; sleeping {delay_seconds:g}s.", flush=True)
            time.sleep(delay_seconds)

    return ok_count, fail_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="批量把已有 xhhai 账号 JSON 切换到另一个分组。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python xhhai_switch_account_group.py --group-name \"GPT Pro分组-低延迟稳定-线路B\"\n"
            "  python xhhai_switch_account_group.py --accounts \"accounts\\route_A\\xhhai_codex_account*.json\" --group-name \"GPT Pro分组-线路B\" --target-dir accounts\\route_B\n"
            "  python xhhai_switch_account_group.py --batch-size 2 --delay-seconds 3\n"
            "  python xhhai_switch_account_group.py --dry-run\n\n"
            "说明:\n"
            "  脚本会使用账号 JSON 里的 refresh_token/access_token 获取可用分组，\n"
            "  在目标分组中新建一个 key，然后更新本地 JSON 的 api_key/group/key 字段。\n"
            "  默认会先备份原 JSON 为 .bak-时间戳 文件，并跳过本地缓存余额 <= 0 的账号。\n"
        ),
    )
    parser.add_argument("--accounts", action="append", default=[], help="账号 JSON 文件或通配符；可重复传。默认搜索当前目录、accounts 目录和 accounts 子目录。")
    parser.add_argument("--source-group-name", default="", help="只处理本地 JSON 当前分组名匹配这个值的账号；留空则不按分组名过滤。")
    parser.add_argument("--group-name", default=DEFAULT_GROUP_NAME, help="目标分组名称，支持完整名或部分匹配。默认 %(default)s。")
    parser.add_argument("--key-name", default=DEFAULT_KEY_NAME, help="目标分组中新建 key 的名称。默认 %(default)s。")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="接口基础地址。默认 %(default)s。")
    parser.add_argument("--use-system-proxy", action="store_true", help="使用 Windows/环境变量里的系统代理或 VPN HTTP 代理路线。")
    parser.add_argument("--no-refresh", action="store_true", help="切换前不刷新 access_token。")
    parser.add_argument("--no-backup", action="store_true", help="写回 JSON 前不创建 .bak 备份。")
    parser.add_argument("--dry-run", action="store_true", help="只显示计划切换，不创建 key、不写文件、不移动 JSON。")
    parser.add_argument("--batch-size", type=int, default=1, help="每批切换多少个账号。默认 1。")
    parser.add_argument("--delay-seconds", type=float, default=2.0, help="每批之间等待多少秒。默认 2。")
    parser.add_argument("--limit", type=int, default=0, help="最多选择多少个账号执行切换；0 表示不限制。")
    parser.add_argument("--target-dir", type=Path, default=None, help="切换成功后把账号 JSON 移动到这个目录；留空则不移动。")
    parser.add_argument("--include-depleted", action="store_true", help="默认跳过本地缓存余额 <= 0 的账号；加上此参数则不跳过。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    patterns = args.accounts or DEFAULT_ACCOUNT_PATTERNS
    paths = resolve_account_paths(patterns)
    if not paths:
        print("No account JSON files found.", file=sys.stderr)
        return 1

    ok_count, fail_count = switch_many(
        paths,
        api_base=args.api_base.rstrip("/"),
        group_name=args.group_name,
        key_name=args.key_name,
        use_system_proxy=args.use_system_proxy,
        refresh_first=not args.no_refresh,
        dry_run=args.dry_run,
        no_backup=args.no_backup,
        batch_size=args.batch_size,
        delay_seconds=args.delay_seconds,
        source_group_name=args.source_group_name,
        limit=args.limit,
        target_dir=args.target_dir,
        skip_depleted=not args.include_depleted,
    )

    print(f"Done. ok={ok_count}, failed={fail_count}")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
