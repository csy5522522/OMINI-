#!/usr/bin/env python3
"""Best-effort Codex++ provider/profile synchronization."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_PROVIDER_NAME = "custom"
DEFAULT_PROFILE_NAME = "\u6279\u91cf\u6ce8\u518c"
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_WIRE_API = "responses"
DEFAULT_BASE_URL = "https://api.xhhai.top"
DEFAULT_AUTH_KEY_FIELD = "OPENAI_API_KEY"
DEFAULT_CODEXPP_SETTINGS_PATH = Path.home() / ".codex-session-delete" / "settings.json"
DEFAULT_WEBUI_CUSTOM_PROVIDERS_PATH = Path.home() / ".codex" / "webui-custom-providers.json"
DEFAULT_LOCAL_PROXY_PROFILE_NAME = "Omini API本地轮换代理"
DEFAULT_LOCAL_PROXY_BASE_URL = "http://127.0.0.1:8787/api/v1"
DEFAULT_LOCAL_PROXY_API_KEY = "xhhai-local-proxy"


@dataclass
class CodexPlusSyncResult:
    updated_profiles: list[str] = field(default_factory=list)
    updated_webui: bool = False
    matched_profiles: list[str] = field(default_factory=list)
    matched_webui: bool = False
    backups: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def updated(self) -> bool:
        return bool(self.updated_profiles or self.updated_webui)

    def summary(self) -> str:
        parts: list[str] = []
        if self.updated_profiles:
            parts.append("profiles=" + ",".join(self.updated_profiles))
        if self.updated_webui:
            parts.append("webui-custom-providers")
        if self.matched_profiles:
            parts.append("already_profiles=" + ",".join(self.matched_profiles))
        if self.matched_webui:
            parts.append("already_webui-custom-providers")
        if self.backups:
            parts.append("backups=" + ",".join(str(path) for path in self.backups))
        if self.skipped:
            parts.append("skipped=" + "; ".join(self.skipped))
        return "; ".join(parts) if parts else "no Codex++ changes"


@dataclass
class CodexPlusProfileState:
    found: bool
    profile_name: str
    api_key: str | None = None
    base_url: str | None = None
    matched_by_active: bool = False
    skipped: str | None = None

    @property
    def label(self) -> str:
        return f"{self.profile_name}(active)" if self.matched_by_active else self.profile_name


def normalize_base_url(url: str | None) -> str | None:
    if not url:
        return None
    return url.strip().rstrip("/").lower()


def urls_match(current: str | None, expected: str | None) -> bool:
    current_normalized = normalize_base_url(current)
    expected_normalized = normalize_base_url(expected)
    return bool(current_normalized and expected_normalized and current_normalized == expected_normalized)


def _is_legacy_local_proxy_profile(profile: dict[str, Any], provider_name: str) -> bool:
    """Recognize only an older, locally generated HTTP proxy profile.

    Older toolbox versions wrote ``https://127.0.0.1:8787`` even though the
    bundled proxy is plain HTTP and expects the ``/api/v1`` prefix.  Limit the
    migration to locally addressed profiles with our generated id so unrelated
    providers are never changed.
    """
    if not _profile_id(profile).casefold().startswith("relay-xhhai-local-"):
        return False
    current = profile_base_url(profile, provider_name) or str(profile.get("upstreamBaseUrl") or "")
    normalized = normalize_base_url(current)
    return normalized in {
        "http://127.0.0.1:8787",
        "https://127.0.0.1:8787",
        "http://127.0.0.1:8787/api/v1",
        "https://127.0.0.1:8787/api/v1",
    }


def provider_base_url_from_api_base(api_base: str) -> str:
    value = api_base.strip().rstrip("/")
    for suffix in ("/api/v1", "/api"):
        if value.lower().endswith(suffix):
            return value[: -len(suffix)]
    return value or DEFAULT_BASE_URL


def backup_file(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    return backup


def build_auth_contents(api_key: str, auth_key_field: str = DEFAULT_AUTH_KEY_FIELD) -> str:
    return json.dumps({auth_key_field: api_key}, ensure_ascii=False, indent=2) + "\n"


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _parse_string_value(raw: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    if raw[0] in ("'", '"') and raw[-1:] == raw[0]:
        return raw[1:-1]
    return raw


def parse_auth_contents(auth_contents: Any, auth_key_field: str = DEFAULT_AUTH_KEY_FIELD) -> str | None:
    if not isinstance(auth_contents, str) or not auth_contents.strip():
        return None
    try:
        data = json.loads(auth_contents)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(auth_key_field)
    return value if isinstance(value, str) and value else None


def find_model_provider(config_text: str) -> str:
    for line in config_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("model_provider") and "=" in stripped:
            value = _parse_string_value(stripped.split("=", 1)[1])
            if value:
                return value
    return DEFAULT_PROVIDER_NAME


def extract_provider_base_url(config_text: str | None, provider_name: str = DEFAULT_PROVIDER_NAME) -> str | None:
    if not config_text:
        return None
    target_section = f"[model_providers.{provider_name}]"
    in_target = False
    for line in config_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_target = stripped == target_section
            continue
        if in_target and stripped.startswith("base_url") and "=" in stripped:
            return _parse_string_value(stripped.split("=", 1)[1])
    return None


def profile_base_url(profile: dict[str, Any], provider_name: str = DEFAULT_PROVIDER_NAME) -> str | None:
    upstream = profile.get("upstreamBaseUrl")
    if isinstance(upstream, str) and upstream.strip():
        return upstream.strip().rstrip("/")
    config_contents = profile.get("configContents")
    if isinstance(config_contents, str):
        return extract_provider_base_url(config_contents, provider_name)
    return None


def read_codexpp_profile_state(
    *,
    settings_path: Path = DEFAULT_CODEXPP_SETTINGS_PATH,
    profile_name: str = DEFAULT_PROFILE_NAME,
    expected_base_url: str = DEFAULT_BASE_URL,
    provider_name: str = DEFAULT_PROVIDER_NAME,
    auth_key_field: str = DEFAULT_AUTH_KEY_FIELD,
) -> CodexPlusProfileState:
    if not settings_path.exists():
        return CodexPlusProfileState(False, profile_name, skipped=f"Codex++ settings not found: {settings_path}")

    try:
        data = _load_json_object(settings_path)
    except Exception as exc:
        return CodexPlusProfileState(False, profile_name, skipped=f"Codex++ settings read failed: {exc}")

    profiles = data.get("relayProfiles")
    if not isinstance(profiles, list):
        return CodexPlusProfileState(False, profile_name, skipped=f"Codex++ settings has no relayProfiles: {settings_path}")

    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        matched_by_name = _same_profile_name(profile.get("name"), profile_name)
        if not matched_by_name:
            continue
        return CodexPlusProfileState(
            True,
            str(profile.get("name") or profile_name),
            api_key=parse_auth_contents(profile.get("authContents"), auth_key_field),
            base_url=profile_base_url(profile, provider_name),
            matched_by_active=False,
        )

    return CodexPlusProfileState(False, profile_name, skipped=f"Codex++ profile not found: {profile_name}")


def _load_json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _write_json(path: Path, data: dict[str, Any], *, dry_run: bool) -> Path | None:
    if dry_run:
        return None
    backup = backup_file(path)
    temp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, path)
    return backup


def _same_profile_name(value: Any, wanted: str) -> bool:
    return str(value or "").strip().casefold() == wanted.strip().casefold()


def _active_relay_id_candidates(data: dict[str, Any]) -> set[str]:
    active = data.get("activeRelayId")
    if not isinstance(active, str) or not active.strip():
        return set()
    value = active.strip()
    candidates = {value.casefold()}
    if value.casefold().startswith("ccs-"):
        candidates.add(value[4:].casefold())
    return candidates


def _profile_id(profile: dict[str, Any]) -> str:
    value = profile.get("id")
    return str(value or "").strip()


def _profile_label(profile: dict[str, Any], fallback: str, *, matched_by_active: bool) -> str:
    name = str(profile.get("name") or fallback)
    return f"{name}(active)" if matched_by_active else name


def build_local_proxy_config_contents(
    *,
    base_url: str = DEFAULT_LOCAL_PROXY_BASE_URL,
    provider_name: str = DEFAULT_PROVIDER_NAME,
    model: str = DEFAULT_MODEL,
    wire_api: str = DEFAULT_WIRE_API,
) -> str:
    return (
        f"model_provider = {_toml_string(provider_name)}\n"
        f"model = {_toml_string(model)}\n"
        'model_reasoning_effort = "xhigh"\n'
        "disable_response_storage = true\n\n"
        f"[model_providers.{provider_name}]\n"
        f"name = {_toml_string(provider_name)}\n"
        f"base_url = {_toml_string(base_url.rstrip('/'))}\n"
        f"wire_api = {_toml_string(wire_api)}\n"
        "requires_openai_auth = true\n"
    )


def update_local_proxy_config_base_url(
    config_text: str | None,
    *,
    base_url: str,
    provider_name: str = DEFAULT_PROVIDER_NAME,
    model: str = DEFAULT_MODEL,
    wire_api: str = DEFAULT_WIRE_API,
) -> str:
    if not isinstance(config_text, str) or not config_text.strip():
        return build_local_proxy_config_contents(
            base_url=base_url,
            provider_name=provider_name,
            model=model,
            wire_api=wire_api,
        )

    target_section = f"[model_providers.{provider_name}]"
    lines = config_text.splitlines()
    in_target = False
    found_section = False
    replaced = False
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_target = stripped == target_section
            found_section = found_section or in_target
        if in_target and stripped.startswith("base_url") and "=" in stripped:
            indent = line[: len(line) - len(line.lstrip())]
            output.append(f"{indent}base_url = {_toml_string(base_url.rstrip('/'))}")
            replaced = True
        else:
            output.append(line)

    if found_section and replaced:
        return "\n".join(output).rstrip() + "\n"
    return build_local_proxy_config_contents(
        base_url=base_url,
        provider_name=provider_name,
        model=model,
        wire_api=wire_api,
    )


def install_local_proxy_profile(
    *,
    settings_path: Path = DEFAULT_CODEXPP_SETTINGS_PATH,
    profile_name: str = DEFAULT_LOCAL_PROXY_PROFILE_NAME,
    base_url: str = DEFAULT_LOCAL_PROXY_BASE_URL,
    api_key: str = DEFAULT_LOCAL_PROXY_API_KEY,
    provider_name: str = DEFAULT_PROVIDER_NAME,
    model: str = DEFAULT_MODEL,
    wire_api: str = DEFAULT_WIRE_API,
    auth_key_field: str = DEFAULT_AUTH_KEY_FIELD,
    activate: bool = False,
    dry_run: bool = False,
) -> CodexPlusSyncResult:
    result = CodexPlusSyncResult()
    if not _same_profile_name(profile_name, DEFAULT_LOCAL_PROXY_PROFILE_NAME):
        result.skipped.append(
            f"Refusing unsafe local proxy profile name: {profile_name}; "
            f"required={DEFAULT_LOCAL_PROXY_PROFILE_NAME}"
        )
        return result
    if not settings_path.exists():
        result.skipped.append(f"Codex++ settings not found: {settings_path}")
        return result

    data = _load_json_object(settings_path)
    profiles = data.get("relayProfiles")
    if not isinstance(profiles, list):
        result.skipped.append(f"Codex++ settings has no relayProfiles: {settings_path}")
        return result

    profile: dict[str, Any] | None = None
    for item in profiles:
        if isinstance(item, dict) and _same_profile_name(item.get("name"), profile_name):
            profile = item
            break

    # Upgrade the previous generated local profile in place.  Keeping its
    # existing display name avoids leaving a currently selected Codex profile
    # pointed at a TLS endpoint the proxy does not serve.
    if profile is None and _same_profile_name(profile_name, DEFAULT_LOCAL_PROXY_PROFILE_NAME):
        legacy_profiles = [
            item for item in profiles
            if isinstance(item, dict) and _is_legacy_local_proxy_profile(item, provider_name)
        ]
        if len(legacy_profiles) == 1:
            profile = legacy_profiles[0]

    if profile is not None:
        profile_id = _profile_id(profile).casefold()
        current_base_url = profile_base_url(profile, provider_name)
        managed_profile = profile_id.startswith("relay-xhhai-local-") or urls_match(current_base_url, base_url)
        if not managed_profile:
            result.skipped.append(
                f"Refusing to overwrite non-XHHAI Codex++ profile: {profile_name} "
                f"current={current_base_url or 'empty'} target={base_url}"
            )
            return result

    if profile is None:
        profile = {
            "id": f"relay-xhhai-local-{int(time.time())}",
            "linkedCcsProviderId": "",
            "name": profile_name,
            "upstreamBaseUrl": base_url.rstrip("/"),
            "protocol": "responses",
            "relayMode": "pureApi",
            "officialMixApiKey": False,
            "testModel": "",
            "configContents": build_local_proxy_config_contents(
                base_url=base_url,
                provider_name=provider_name,
                model=model,
                wire_api=wire_api,
            ),
            "authContents": build_auth_contents(api_key, auth_key_field),
            "useCommonConfig": False,
            "contextSelection": {"mcpServers": [], "skills": [], "plugins": []},
            "contextSelectionInitialized": True,
            "contextWindow": "",
            "autoCompactLimit": "",
            "modelInsertMode": "patch",
            "modelList": "gpt-5.5\ngpt-5.4\ngpt-5.4-mini\ngpt-5.3-codex\ngpt-5.3-codex-spark\ngpt-5.2",
        }
        profiles.append(profile)
        result.updated_profiles.append(profile_name)
    else:
        old = json.dumps(profile, ensure_ascii=False, sort_keys=True)
        profile["upstreamBaseUrl"] = base_url.rstrip("/")
        profile["protocol"] = "responses"
        profile["relayMode"] = "pureApi"
        profile["authContents"] = build_auth_contents(api_key, auth_key_field)
        profile["useCommonConfig"] = False
        profile["configContents"] = update_local_proxy_config_base_url(
            profile.get("configContents"),
            base_url=base_url,
            provider_name=provider_name,
            model=model,
            wire_api=wire_api,
        )
        new = json.dumps(profile, ensure_ascii=False, sort_keys=True)
        if old != new:
            result.updated_profiles.append(profile_name)
        else:
            result.matched_profiles.append(profile_name)

    if activate:
        profile_id = _profile_id(profile)
        if not profile_id:
            result.skipped.append(f"Codex++ profile has no id: {profile_name}")
        else:
            active_id = str(data.get("activeRelayId") or "").strip()
            active_candidates = _active_relay_id_candidates(data)
            active_profile_name = f"{profile_name}(active)"
            if profile_id.casefold() in active_candidates:
                result.matched_profiles.append(active_profile_name)
            else:
                data["activeRelayId"] = profile_id
                result.updated_profiles.append(active_profile_name)

    if result.updated_profiles:
        backup = _write_json(settings_path, data, dry_run=dry_run)
        if backup:
            result.backups.append(backup)
    return result


def sync_codexpp_settings(
    *,
    api_key: str,
    base_url: str,
    settings_path: Path = DEFAULT_CODEXPP_SETTINGS_PATH,
    profile_name: str = DEFAULT_PROFILE_NAME,
    provider_name: str = DEFAULT_PROVIDER_NAME,
    model: str = DEFAULT_MODEL,
    wire_api: str = DEFAULT_WIRE_API,
    auth_key_field: str = DEFAULT_AUTH_KEY_FIELD,
    dry_run: bool = False,
) -> CodexPlusSyncResult:
    result = CodexPlusSyncResult()
    if not settings_path.exists():
        result.skipped.append(f"Codex++ settings not found: {settings_path}")
        return result

    data = _load_json_object(settings_path)
    profiles = data.get("relayProfiles")
    if not isinstance(profiles, list):
        result.skipped.append(f"Codex++ settings has no relayProfiles: {settings_path}")
        return result

    changed = False
    matched = False
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        matched_by_name = _same_profile_name(profile.get("name"), profile_name)
        if not matched_by_name:
            continue
        label = _profile_label(profile, profile_name, matched_by_active=False)
        matched = True
        current_base_url = profile_base_url(profile, provider_name)
        if not urls_match(current_base_url, base_url):
            result.skipped.append(
                f"Codex++ profile URL is not target: {label} "
                f"current={current_base_url or 'empty'} target={base_url}"
            )
            continue
        old = json.dumps(profile, ensure_ascii=False, sort_keys=True)
        profile["authContents"] = build_auth_contents(api_key, auth_key_field)
        new = json.dumps(profile, ensure_ascii=False, sort_keys=True)
        if old != new:
            changed = True
            result.updated_profiles.append(label)
        else:
            result.matched_profiles.append(label)

    if not matched:
        result.skipped.append(f"Codex++ profile not found: {profile_name}")
        return result
    if changed:
        backup = _write_json(settings_path, data, dry_run=dry_run)
        if backup:
            result.backups.append(backup)
    return result


def sync_webui_custom_providers(
    *,
    api_key: str,
    base_url: str,
    webui_path: Path = DEFAULT_WEBUI_CUSTOM_PROVIDERS_PATH,
    provider_name: str = DEFAULT_PROVIDER_NAME,
    model: str = DEFAULT_MODEL,
    wire_api: str = DEFAULT_WIRE_API,
    dry_run: bool = False,
) -> CodexPlusSyncResult:
    result = CodexPlusSyncResult()
    if not webui_path.exists():
        result.skipped.append(f"Codex++ webui provider state not found: {webui_path}")
        return result

    data = _load_json_object(webui_path)
    current_base_url = data.get("customBaseUrl")
    if not isinstance(current_base_url, str):
        current_base_url = None
    if not urls_match(current_base_url, base_url):
        result.skipped.append(
            f"Codex++ webui provider URL is not target: current={current_base_url or 'empty'} target={base_url}"
        )
        return result

    old = json.dumps(data, ensure_ascii=False, sort_keys=True)
    data["apiKey"] = api_key
    provider = data.get("provider")
    if not isinstance(provider, str) or not provider:
        provider = provider_name
    provider_keys = data.get("providerKeys")
    if not isinstance(provider_keys, dict):
        provider_keys = {}
        data["providerKeys"] = provider_keys
    provider_keys[provider] = api_key

    new = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if old != new:
        backup = _write_json(webui_path, data, dry_run=dry_run)
        if backup:
            result.backups.append(backup)
        result.updated_webui = True
    else:
        result.matched_webui = True
    return result


def sync_codexpp_if_present(
    *,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    settings_path: Path = DEFAULT_CODEXPP_SETTINGS_PATH,
    webui_path: Path = DEFAULT_WEBUI_CUSTOM_PROVIDERS_PATH,
    profile_name: str = DEFAULT_PROFILE_NAME,
    provider_name: str = DEFAULT_PROVIDER_NAME,
    model: str = DEFAULT_MODEL,
    wire_api: str = DEFAULT_WIRE_API,
    auth_key_field: str = DEFAULT_AUTH_KEY_FIELD,
    dry_run: bool = False,
) -> CodexPlusSyncResult:
    result = CodexPlusSyncResult()
    if not api_key:
        result.skipped.append("empty API key")
        return result

    settings_result = sync_codexpp_settings(
        api_key=api_key,
        base_url=base_url,
        settings_path=settings_path,
        profile_name=profile_name,
        provider_name=provider_name,
        model=model,
        wire_api=wire_api,
        auth_key_field=auth_key_field,
        dry_run=dry_run,
    )
    webui_result = sync_webui_custom_providers(
        api_key=api_key,
        base_url=base_url,
        webui_path=webui_path,
        provider_name=provider_name,
        model=model,
        wire_api=wire_api,
        dry_run=dry_run,
    )

    result.updated_profiles.extend(settings_result.updated_profiles)
    result.updated_webui = webui_result.updated_webui
    result.matched_profiles.extend(settings_result.matched_profiles)
    result.matched_webui = webui_result.matched_webui
    result.backups.extend(settings_result.backups)
    result.backups.extend(webui_result.backups)
    result.skipped.extend(settings_result.skipped)
    result.skipped.extend(webui_result.skipped)
    return result
