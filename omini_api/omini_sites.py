from __future__ import annotations

import copy
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SITE_ID = "xhhai"
PROXY_MODES = ("自动", "强制代理", "直连")


class SiteError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProxyDecision:
    mode: str
    use_proxy: bool
    note: str = ""
    proxy_url: str = ""


def default_xhhai_profile() -> dict[str, Any]:
    return {
        "id": DEFAULT_SITE_ID,
        "name": "XHHAI",
        "home_url": "https://api.xhhai.top",
        "api_base": "https://api.xhhai.top/api/v1",
        "enabled": True,
        "is_default": True,
        "verification": {"mode": "auto", "code_pattern": r"\b\d{4,8}\b"},
        "key_workflow": {"create_requires_group": True},
        "auth": {"header": "Authorization", "template": "Bearer {access_token}"},
        "envelope": {
            "code_path": "code",
            "success_values": [0],
            "data_path": "data",
            "message_path": "message",
        },
        "operations": {
            "settings": {
                "method": "GET",
                "path": "/settings/public",
                "response": {
                    "registration_enabled": "registration_enabled",
                    "email_verify_enabled": "email_verify_enabled",
                    "turnstile_enabled": "turnstile_enabled",
                    "invitation_code_enabled": "invitation_code_enabled",
                    "requires_manual": "requires_manual",
                },
            },
            "register": {
                "method": "POST",
                "path": "/auth/register",
                "body_map": {
                    "email": "email",
                    "password": "password",
                    "verification_code": "verify_code",
                    "promo_code": "promo_code",
                    "invitation_code": "invitation_code",
                    "aff_code": "aff_code",
                },
                "response": {
                    "access_token": "access_token",
                    "refresh_token": "refresh_token",
                    "expires_in": "expires_in",
                    "token_type": "token_type",
                    "user": "user",
                },
            },
            "send_verification_code": {
                "method": "POST",
                "path": "/auth/send-verify-code",
                "body_map": {"email": "email"},
            },
            "login": {
                "method": "POST",
                "path": "/auth/login",
                "body_map": {"email": "email", "password": "password"},
                "response": {
                    "access_token": "access_token",
                    "refresh_token": "refresh_token",
                    "expires_in": "expires_in",
                    "token_type": "token_type",
                    "user": "user",
                },
            },
            "refresh": {
                "method": "POST",
                "path": "/auth/refresh",
                "body_map": {"refresh_token": "refresh_token"},
                "response": {
                    "access_token": "access_token",
                    "refresh_token": "refresh_token",
                    "expires_in": "expires_in",
                    "token_type": "token_type",
                    "user": "user",
                },
            },
            "groups": {
                "method": "GET",
                "path": "/groups/available",
                "auth": True,
                "response": {"items": "$", "id": "id", "name": "name"},
            },
            "create_key": {
                "method": "POST",
                "path": "/keys",
                "auth": True,
                "body_map": {"key_name": "name", "group_id": "group_id"},
                "response": {"api_key": "key", "id": "id", "name": "name"},
            },
            "balance": {
                "method": "GET",
                "path": "/auth/me",
                "auth": True,
                "response": {"balance": "balance"},
            },
        },
    }


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _upgrade_profile(profile: dict[str, Any]) -> bool:
    changed = False
    if not isinstance(profile.get("verification"), dict):
        profile["verification"] = {"mode": "auto", "code_pattern": r"\b\d{4,8}\b"}
        changed = True
    if not isinstance(profile.get("key_workflow"), dict):
        profile["key_workflow"] = {"create_requires_group": True}
        changed = True
    operations = profile.get("operations")
    profile_id = str(profile.get("id") or "")
    settings_path = str(((operations or {}).get("settings") or {}).get("path") or "") if isinstance(operations, dict) else ""
    register_path = str(((operations or {}).get("register") or {}).get("path") or "") if isinstance(operations, dict) else ""
    xhhai_compatible = (
        profile_id == DEFAULT_SITE_ID
        or profile_id.startswith("discovered-")
        or (settings_path.endswith("/settings/public") and register_path.endswith("/auth/register"))
    )
    if isinstance(operations, dict) and xhhai_compatible:
        settings = operations.get("settings")
        if isinstance(settings, dict) and not isinstance(settings.get("response"), dict):
            settings["response"] = {
                "registration_enabled": "registration_enabled",
                "email_verify_enabled": "email_verify_enabled",
                "turnstile_enabled": "turnstile_enabled",
                "invitation_code_enabled": "invitation_code_enabled",
                "requires_manual": "requires_manual",
            }
            changed = True
        register = operations.get("register")
        if isinstance(register, dict):
            body_map = register.setdefault("body_map", {})
            if isinstance(body_map, dict):
                current_verification_field = str(body_map.get("verification_code") or "")
                if not current_verification_field or current_verification_field == "email_code":
                    body_map["verification_code"] = "verify_code"
                    changed = True
        if "send_verification_code" not in operations:
            operations["send_verification_code"] = {
                "method": "POST",
                "path": "/auth/send-verify-code",
                "body_map": {"email": "email"},
            }
            changed = True
    return changed


def load_site_store(path: Path, *, create: bool = True) -> dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and isinstance(data.get("sites"), list):
                changed = False
                for item in data["sites"]:
                    if isinstance(item, dict) and _upgrade_profile(item):
                        changed = True
                if changed and create:
                    _write_json_atomic(path, data)
                return data
        except Exception as exc:
            raise SiteError(f"站点配置读取失败: {exc}") from exc
    data = {"version": 1, "sites": [default_xhhai_profile()]}
    if create:
        _write_json_atomic(path, data)
    return data


def save_site_store(path: Path, store: dict[str, Any]) -> None:
    sites = store.get("sites") if isinstance(store, dict) else None
    if not isinstance(sites, list) or not sites:
        raise SiteError("至少保留一个号池网站。")
    if not any(site.get("enabled", True) for site in sites if isinstance(site, dict)):
        raise SiteError("至少启用一个号池网站。")
    default_seen = False
    for site in sites:
        validate_profile(site)
        if site.get("is_default") and site.get("enabled", True) is False:
            raise SiteError("默认网站必须保持启用。")
        if site.get("is_default") and not default_seen:
            default_seen = True
        elif site.get("is_default"):
            site["is_default"] = False
    if not default_seen:
        next(site for site in sites if site.get("enabled", True))["is_default"] = True
    store["version"] = 1
    _write_json_atomic(path, store)


def validate_profile(profile: dict[str, Any]) -> None:
    for field in ("id", "name", "home_url", "api_base"):
        if not str(profile.get(field) or "").strip():
            raise SiteError(f"站点字段 {field} 不能为空。")
    for field in ("home_url", "api_base"):
        parsed = urllib.parse.urlparse(str(profile[field]))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SiteError(f"{field} 必须是完整的 HTTP/HTTPS 地址。")
    operations = profile.get("operations")
    if not isinstance(operations, dict) or "register" not in operations:
        raise SiteError("站点协议至少需要 register 操作。")
    verification = profile.get("verification") or {}
    if str(verification.get("mode") or "auto") not in {"auto", "direct", "receiver"}:
        raise SiteError("verification.mode 只支持 auto、direct 或 receiver。")
    workflow = profile.get("key_workflow") or {}
    if workflow.get("create_requires_group") is False and "update_key_group" not in operations:
        raise SiteError("先建 Key 的协议必须配置 update_key_group 操作。")


def site_by_id(store: dict[str, Any], site_id: str) -> dict[str, Any]:
    for profile in store.get("sites", []):
        if str(profile.get("id")) == site_id:
            return profile
    raise SiteError(f"号池网站不存在: {site_id}")


def default_site(store: dict[str, Any]) -> dict[str, Any]:
    sites = store.get("sites", [])
    for profile in sites:
        if profile.get("is_default"):
            return profile
    if sites:
        return sites[0]
    raise SiteError("没有可用的号池网站配置。")


def clone_profile(profile: dict[str, Any], *, name: str | None = None) -> dict[str, Any]:
    cloned = copy.deepcopy(profile)
    cloned["id"] = uuid.uuid4().hex[:12]
    cloned["name"] = name or f"{profile.get('name', '站点')} 副本"
    cloned["is_default"] = False
    return cloned


def json_path_get(value: Any, path: str | None, default: Any = None) -> Any:
    if path is None or str(path).strip() in {"", "$"}:
        return value
    current = value
    normalized = str(path).strip()
    if normalized.startswith("$."):
        normalized = normalized[2:]
    for part in normalized.split("."):
        if not part:
            continue
        if isinstance(current, dict):
            if part not in current:
                return default
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return default
            current = current[index]
        else:
            return default
    return current


def _format_template(value: Any, variables: dict[str, Any]) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return value.format(**{key: "" if item is None else item for key, item in variables.items()})
    except (KeyError, ValueError) as exc:
        raise SiteError(f"模板格式错误: {value}: {exc}") from exc


def operation_url(profile: dict[str, Any], operation: dict[str, Any], variables: dict[str, Any] | None = None) -> str:
    path = str(_format_template(operation.get("path") or "", variables or {})).strip()
    if not path:
        raise SiteError("接口路径为空。")
    if urllib.parse.urlparse(path).scheme in {"http", "https"}:
        return path
    return str(profile["api_base"]).rstrip("/") + "/" + path.lstrip("/")


def resolve_proxy(mode: str, host: str, port: int, timeout: float = 0.6) -> ProxyDecision:
    normalized = mode if mode in PROXY_MODES else "自动"
    if normalized == "直连":
        return ProxyDecision(normalized, False)
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return ProxyDecision(normalized, True, proxy_url=f"http://{host}:{int(port)}")
    except OSError as exc:
        if normalized == "强制代理":
            raise SiteError(f"代理 {host}:{port} 未监听: {exc}") from exc
        return ProxyDecision(normalized, False, f"代理 {host}:{port} 未监听，本次任务已改用直连。")


def _opener(use_proxy: bool, proxy_url: str = "") -> Any:
    if use_proxy and proxy_url:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})).open
    if use_proxy:
        return urllib.request.urlopen
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def request_operation(
    profile: dict[str, Any],
    operation_name: str,
    variables: dict[str, Any] | None = None,
    *,
    use_proxy: bool = False,
    proxy_url: str = "",
    timeout: int = 30,
) -> tuple[Any, dict[str, Any]]:
    if profile.get("enabled", True) is False:
        raise SiteError(f"站点已停用: {profile.get('name') or profile.get('id')}")
    variables = dict(variables or {})
    operations = profile.get("operations") or {}
    operation = operations.get(operation_name)
    if not isinstance(operation, dict):
        raise SiteError(f"站点未配置 {operation_name} 接口。")
    method = str(operation.get("method") or "GET").upper()
    url = operation_url(profile, operation, variables)
    body: dict[str, Any] = {}
    static_body = operation.get("static_body")
    if isinstance(static_body, dict):
        body.update({str(k): _format_template(v, variables) for k, v in static_body.items()})
    body_map = operation.get("body_map")
    if isinstance(body_map, dict):
        for canonical, provider_field in body_map.items():
            value = variables.get(str(canonical))
            if value not in (None, "") and str(provider_field).strip():
                body[str(provider_field)] = value
    query: dict[str, Any] = {}
    static_query = operation.get("static_query")
    if isinstance(static_query, dict):
        query.update({str(k): _format_template(v, variables) for k, v in static_query.items()})
    query_map = operation.get("query_map")
    if isinstance(query_map, dict):
        for canonical, provider_field in query_map.items():
            value = variables.get(str(canonical))
            if value not in (None, "") and str(provider_field).strip():
                query[str(provider_field)] = value
    if query:
        separator = "&" if "?" in url else "?"
        url += separator + urllib.parse.urlencode(query)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body and method not in {"GET", "HEAD"} else None
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Omini-API-Toolbox/2.0"}
    for custom_headers in (profile.get("headers"), operation.get("headers")):
        if isinstance(custom_headers, dict):
            headers.update({str(k): str(_format_template(v, variables)) for k, v in custom_headers.items()})
    if operation.get("auth"):
        token = str(variables.get("access_token") or "")
        if not token:
            raise SiteError(f"{operation_name} 操作缺少 access_token。")
        auth = profile.get("auth") or {}
        header_name = str(auth.get("header") or "Authorization")
        template = str(auth.get("template") or "Bearer {access_token}")
        headers[header_name] = str(_format_template(template, variables))
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _opener(use_proxy, proxy_url)(req, timeout=timeout) as response:
            raw_text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw_text = exc.read().decode("utf-8", errors="replace")
        raise SiteError(f"HTTP {exc.code}: {raw_text[:300]}") from exc
    except urllib.error.URLError as exc:
        raise SiteError(f"网络错误: {exc.reason}") from exc
    try:
        raw = json.loads(raw_text) if raw_text.strip() else {}
    except json.JSONDecodeError as exc:
        raise SiteError(f"接口返回的内容不是 JSON: {raw_text[:300]}") from exc

    envelope = dict(profile.get("envelope") or {})
    if isinstance(operation.get("envelope"), dict):
        envelope.update(operation["envelope"])
    code_path = str(envelope.get("code_path") or "").strip()
    if code_path:
        marker = object()
        code = json_path_get(raw, code_path, marker)
        if code is not marker:
            success_values = envelope.get("success_values", [0])
            if code not in success_values:
                message = json_path_get(raw, str(envelope.get("message_path") or "message"), raw)
                raise SiteError(str(message))
    data_path = operation.get("data_path", envelope.get("data_path", ""))
    result = json_path_get(raw, str(data_path), raw)
    return result, raw


def canonical_response(operation: dict[str, Any], result: Any) -> dict[str, Any]:
    mapping = operation.get("response") or {}
    canonical: dict[str, Any] = {}
    if not isinstance(mapping, dict):
        return canonical
    for name, path in mapping.items():
        if name == "items":
            continue
        value = json_path_get(result, str(path), None)
        if value is not None:
            canonical[str(name)] = value
    return canonical


def normalize_groups(profile: dict[str, Any], result: Any) -> list[dict[str, Any]]:
    operation = (profile.get("operations") or {}).get("groups") or {}
    response = operation.get("response") or {}
    items = json_path_get(result, str(response.get("items") or "$"), [])
    if not isinstance(items, list):
        raise SiteError("分组接口没有返回列表。")
    normalized: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        group_id = json_path_get(raw, str(response.get("id") or "id"), None)
        name = json_path_get(raw, str(response.get("name") or "name"), None)
        if group_id is not None and str(name or "").strip():
            normalized.append({"id": group_id, "name": str(name), "raw": raw})
    return normalized


def test_site(profile: dict[str, Any], *, use_proxy: bool = False, proxy_url: str = "", timeout: int = 15) -> str:
    validate_profile(profile)
    if profile.get("enabled", True) is False:
        raise SiteError(f"站点已停用: {profile.get('name') or profile.get('id')}")
    if "settings" in (profile.get("operations") or {}):
        result, _ = request_operation(profile, "settings", use_proxy=use_proxy, proxy_url=proxy_url, timeout=timeout)
        if isinstance(result, dict):
            operation = (profile.get("operations") or {}).get("settings") or {}
            mapped = canonical_response(operation, result)
            if mapped:
                result = {**result, **mapped}
        if isinstance(result, dict) and (
            result.get("turnstile_enabled")
            or result.get("invitation_code_enabled")
            or result.get("requires_manual")
            or result.get("captcha_enabled")
            or result.get("image_captcha_enabled")
            or result.get("tencent_captcha_enabled")
            or result.get("aliyun_captcha_enabled")
            or result.get("oauth_required")
            or result.get("confirmation_link_required")
        ):
            return "连接成功；站点启用了交互验证，注册任务将标记为需人工处理。"
        if isinstance(result, dict) and result.get("email_verify_enabled"):
            return "连接成功；站点需要邮箱验证码，将使用验证码邮箱服务。"
        return "连接成功，settings 接口返回正常。"
    req = urllib.request.Request(str(profile["home_url"]), method="GET", headers={"User-Agent": "Omini-API-Toolbox/2.0"})
    try:
        with _opener(use_proxy, proxy_url)(req, timeout=timeout) as response:
            return f"连接成功，主页 HTTP {response.status}。"
    except Exception as exc:
        raise SiteError(f"主页连接失败: {exc}") from exc
