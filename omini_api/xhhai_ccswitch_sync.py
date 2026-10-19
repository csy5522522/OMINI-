#!/usr/bin/env python3
"""Create or update a CCSwitch Claude provider for the local xhhai proxy."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CCSWITCH_DB = Path.home() / ".cc-switch" / "cc-switch.db"
DEFAULT_PROVIDER_ID = "xhhai-local-claude"
DEFAULT_DESKTOP_PROVIDER_ID = "xhhai-local-claude-desktop"
DEFAULT_PROVIDER_NAME = "Omini API 本地轮换"
DEFAULT_DESKTOP_PROVIDER_NAME = "Omini API 本地轮换 Desktop"
DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:8788"
DEFAULT_LOCAL_AUTH_TOKEN = "xhhai-local-claude"
DEFAULT_SOURCE_PROVIDER_NAME = "极智"
DEFAULT_MODEL = "claude-opus-4-8"


@dataclass
class CCSwitchInstallResult:
    db_path: Path
    provider_id: str
    provider_name: str
    base_url: str
    model: str
    backup_path: Path | None
    source_provider: str
    set_current: bool
    created: bool
    app_type: str = "claude"

    def summary(self) -> str:
        action = "创建" if self.created else "更新"
        current = "并设为当前" if self.set_current else "未切换当前供应商"
        backup = f"，备份={self.backup_path}" if self.backup_path else ""
        return (
            f"{action} CCSwitch {self.app_type} 本地供应商成功：{self.provider_name} "
            f"base_url={self.base_url} model={self.model}，模板={self.source_provider}，{current}{backup}"
        )


def now_ms() -> int:
    return int(time.time() * 1000)


def backup_db(db_path: Path) -> Path:
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"db_backup_xhhai_local_claude_{time.time_ns()}.db"
    shutil.copy2(db_path, backup)
    return backup


def load_json_object(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def fallback_settings(model: str) -> dict[str, Any]:
    return {
        "$schema": "https://json.schemastore.org/claude-code-settings.json",
        "availableModels": [model],
        "env": {
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": model,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        },
        "model": model,
        "permissions": {"defaultMode": "bypassPermissions"},
        "skipDangerousModePermissionPrompt": True,
    }


def source_provider_row(con: sqlite3.Connection, source_name: str, app_type: str = "claude") -> sqlite3.Row | None:
    row = con.execute(
        "select * from providers where app_type=? and name=? order by is_current desc limit 1",
        (app_type, source_name),
    ).fetchone()
    if row is not None:
        return row
    return None


def derive_model(settings: dict[str, Any], fallback: str = DEFAULT_MODEL) -> str:
    for key in ("model",):
        value = settings.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    env = settings.get("env")
    if isinstance(env, dict):
        for key in ("ANTHROPIC_DEFAULT_SONNET_MODEL_NAME", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL"):
            value = env.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    models = settings.get("availableModels")
    if isinstance(models, list):
        for value in models:
            if isinstance(value, str) and value.strip():
                return value.strip()
    return fallback


def make_local_settings(source_settings: dict[str, Any], *, base_url: str, auth_token: str, model: str) -> dict[str, Any]:
    settings = json.loads(json.dumps(source_settings, ensure_ascii=False))
    if not settings:
        settings = fallback_settings(model)
    env = settings.setdefault("env", {})
    if not isinstance(env, dict):
        env = {}
        settings["env"] = env
    env["ANTHROPIC_BASE_URL"] = base_url.rstrip("/")
    env["ANTHROPIC_AUTH_TOKEN"] = auth_token
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    env.setdefault("DISABLE_AUTOUPDATER", "1")
    settings["model"] = model
    models = settings.get("availableModels")
    if not isinstance(models, list) or not models:
        settings["availableModels"] = [model]
    elif model not in models:
        settings["availableModels"] = [model, *models]
    return settings


def install_provider(
    *,
    db_path: Path = DEFAULT_CCSWITCH_DB,
    provider_id: str = DEFAULT_PROVIDER_ID,
    provider_name: str = DEFAULT_PROVIDER_NAME,
    source_provider_name: str = DEFAULT_SOURCE_PROVIDER_NAME,
    app_type: str = "claude",
    base_url: str = DEFAULT_LOCAL_BASE_URL,
    auth_token: str = DEFAULT_LOCAL_AUTH_TOKEN,
    model: str | None = None,
    set_current: bool = True,
    create_backup: bool = True,
) -> CCSwitchInstallResult:
    db_path = db_path.expanduser()
    if not db_path.exists():
        raise FileNotFoundError(f"CCSwitch db not found: {db_path}")

    backup_path = backup_db(db_path) if create_backup else None
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        source = source_provider_row(con, source_provider_name, app_type=app_type)
        source_settings = load_json_object(source["settings_config"]) if source is not None else {}
        selected_model = (model or "").strip() or derive_model(source_settings)
        settings = make_local_settings(
            source_settings,
            base_url=base_url,
            auth_token=auth_token,
            model=selected_model,
        )
        meta = load_json_object(source["meta"]) if source is not None else {}
        meta.setdefault("apiFormat", "anthropic")
        meta["endpointAutoSelect"] = False
        existing = con.execute(
            "select id from providers where id=? and app_type=?",
            (provider_id, app_type),
        ).fetchone()
        created = existing is None
        created_at = now_ms()
        sort_index = source["sort_index"] if source is not None else None
        if set_current:
            con.execute("update providers set is_current=0 where app_type=?", (app_type,))
        values = {
            "id": provider_id,
            "app_type": app_type,
            "name": provider_name,
            "settings_config": json.dumps(settings, ensure_ascii=False, indent=2),
            "website_url": base_url.rstrip("/"),
            "category": "custom",
            "created_at": created_at,
            "sort_index": sort_index,
            "notes": "Omini API local rotating Claude provider. Upstream key is injected by the local proxy.",
            "icon": source["icon"] if source is not None else "anthropic",
            "icon_color": source["icon_color"] if source is not None else "#D4915D",
            "meta": json.dumps(meta, ensure_ascii=False, separators=(",", ":")),
            "is_current": 1 if set_current else 0,
            "in_failover_queue": 0,
            "cost_multiplier": source["cost_multiplier"] if source is not None else "1.0",
            "limit_daily_usd": source["limit_daily_usd"] if source is not None else None,
            "limit_monthly_usd": source["limit_monthly_usd"] if source is not None else None,
            "provider_type": source["provider_type"] if source is not None else None,
        }
        columns = ", ".join(values)
        placeholders = ", ".join(":" + key for key in values)
        update_clause = ", ".join(f"{key}=excluded.{key}" for key in values if key not in {"id", "app_type"})
        con.execute(
            f"insert into providers ({columns}) values ({placeholders}) "
            f"on conflict(id, app_type) do update set {update_clause}",
            values,
        )
        con.execute("delete from provider_endpoints where provider_id=? and app_type=?", (provider_id, app_type))
        con.execute(
            "insert into provider_endpoints (provider_id, app_type, url, added_at) values (?, ?, ?, ?)",
            (provider_id, app_type, base_url.rstrip("/"), created_at),
        )
        con.commit()
        return CCSwitchInstallResult(
            db_path=db_path,
            provider_id=provider_id,
            provider_name=provider_name,
            base_url=base_url.rstrip("/"),
            model=selected_model,
            backup_path=backup_path,
            source_provider=source["name"] if source is not None else "fallback",
            set_current=set_current,
            created=created,
            app_type=app_type,
        )
    finally:
        con.close()


def install_all_claude_providers(
    *,
    db_path: Path = DEFAULT_CCSWITCH_DB,
    provider_name: str = DEFAULT_PROVIDER_NAME,
    source_provider_name: str = DEFAULT_SOURCE_PROVIDER_NAME,
    base_url: str = DEFAULT_LOCAL_BASE_URL,
    auth_token: str = DEFAULT_LOCAL_AUTH_TOKEN,
    model: str | None = None,
    set_current: bool = True,
    create_backup: bool = True,
) -> list[CCSwitchInstallResult]:
    backup = backup_db(db_path.expanduser()) if create_backup else None
    results: list[CCSwitchInstallResult] = []
    # Reuse the same backup for both writes.
    results.append(
        install_provider(
            db_path=db_path,
            provider_id=DEFAULT_PROVIDER_ID,
            provider_name=provider_name,
            source_provider_name=source_provider_name,
            app_type="claude",
            base_url=base_url,
            auth_token=auth_token,
            model=model,
            set_current=set_current,
            create_backup=False,
        )
    )
    # Claude Desktop in this CCSwitch DB uses a separate app_type and a provider named "机智".
    results.append(
        install_provider(
            db_path=db_path,
            provider_id=DEFAULT_DESKTOP_PROVIDER_ID,
            provider_name=f"{provider_name} Desktop",
            source_provider_name="机智",
            app_type="claude-desktop",
            base_url=base_url,
            auth_token=auth_token,
            model=model,
            set_current=set_current,
            create_backup=False,
        )
    )
    for result in results:
        result.backup_path = backup
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create/update CCSwitch local Claude provider for Omini API Toolbox.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_CCSWITCH_DB)
    parser.add_argument("--provider-id", default=DEFAULT_PROVIDER_ID)
    parser.add_argument("--provider-name", default=DEFAULT_PROVIDER_NAME)
    parser.add_argument("--source-provider-name", default=DEFAULT_SOURCE_PROVIDER_NAME)
    parser.add_argument("--app-type", default="claude")
    parser.add_argument("--base-url", default=DEFAULT_LOCAL_BASE_URL)
    parser.add_argument("--auth-token", default=DEFAULT_LOCAL_AUTH_TOKEN)
    parser.add_argument("--model", default="")
    parser.add_argument("--no-set-current", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--all-claude", action="store_true", help="Install both claude and claude-desktop providers.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.all_claude:
        results = install_all_claude_providers(
            db_path=args.db_path,
            provider_name=args.provider_name,
            source_provider_name=args.source_provider_name,
            base_url=args.base_url,
            auth_token=args.auth_token,
            model=args.model or None,
            set_current=not args.no_set_current,
            create_backup=not args.no_backup,
        )
        for result in results:
            print(result.summary())
    else:
        result = install_provider(
            db_path=args.db_path,
            provider_id=args.provider_id,
            provider_name=args.provider_name,
            source_provider_name=args.source_provider_name,
            app_type=args.app_type,
            base_url=args.base_url,
            auth_token=args.auth_token,
            model=args.model or None,
            set_current=not args.no_set_current,
            create_backup=not args.no_backup,
        )
        print(result.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
