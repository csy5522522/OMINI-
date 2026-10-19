from __future__ import annotations

import csv
import json
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from omini_api import omini_email_receiver as email_receiver
from omini_api.omini_sites import SiteError, canonical_response, normalize_groups, request_operation
from omini_api.xhhai_file_utils import write_json_atomic


DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")
SECRET_FIELD_RE = re.compile(
    r'(?i)("?(?:password|access_token|refresh_token|api_key|token|key)"?\s*[:=]\s*["\']?)([^"\'\s,}]+)'
)
BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+\-/=]+")
FIXED_ACCOUNT_PASSWORD = "1234578Cao"


@dataclass
class ProvisionResult:
    status: str
    path: Path
    email: str
    balance: float | None = None
    balance_status: str = "pending"
    key_status: str = "pending"
    group_status: str = "pending"
    errors: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class VerificationTestResult:
    site_id: str
    email: str
    subject: str
    elapsed_seconds: float
    poll_count: int
    email_count: int
    code_length: int


def redact_log_text(value: Any) -> str:
    text = str(value)
    text = SECRET_FIELD_RE.sub(r"\1***", text)
    return BEARER_RE.sub(r"\1***", text)


def normalize_email_domain(value: str) -> str:
    domain = str(value or "").strip().lower().lstrip("@")
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise SiteError(f"邮箱后缀格式错误: {value}") from exc
    if not DOMAIN_RE.fullmatch(ascii_domain):
        raise SiteError(f"邮箱后缀格式错误: {value}")
    return ascii_domain


def normalize_email(value: str) -> str:
    email = str(value or "").strip()
    if email.count("@") != 1:
        raise SiteError(f"邮箱格式错误: {value}")
    local, domain = email.rsplit("@", 1)
    if not local or len(local) > 64 or any(ch.isspace() for ch in local):
        raise SiteError(f"邮箱格式错误: {value}")
    return f"{local}@{normalize_email_domain(domain)}"


def random_email(domain: str) -> str:
    normalized = normalize_email_domain(domain)
    length = secrets.randbelow(5) + 8
    first = str(secrets.randbelow(9) + 1)
    rest = "".join(str(secrets.randbelow(10)) for _ in range(length - 1))
    return f"{first}{rest}@{normalized}"


def read_email_rows(path: Path | None) -> list[tuple[str, str | None]]:
    if path is None:
        return []
    rows: list[tuple[str, str | None]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(line for line in stream if line.strip() and not line.lstrip().startswith("#"))
        for row in reader:
            if row and row[0].strip():
                rows.append((normalize_email(row[0]), row[1].strip() if len(row) > 1 and row[1].strip() else None))
    return rows


def registration_settings(profile: dict[str, Any], *, use_proxy: bool, proxy_url: str = "") -> dict[str, Any]:
    if "settings" not in (profile.get("operations") or {}):
        return {}
    settings, _ = request_operation(profile, "settings", use_proxy=use_proxy, proxy_url=proxy_url)
    if not isinstance(settings, dict):
        return {}
    operation = (profile.get("operations") or {}).get("settings") or {}
    mapped = canonical_response(operation, settings)
    if mapped:
        settings = {**settings, **mapped}
    if settings.get("registration_enabled") is False:
        raise SiteError("站点已关闭注册。")
    if (
        settings.get("turnstile_enabled")
        or settings.get("invitation_code_enabled")
        or settings.get("requires_manual")
        or settings.get("captcha_enabled")
        or settings.get("image_captcha_enabled")
        or settings.get("tencent_captcha_enabled")
        or settings.get("aliyun_captcha_enabled")
        or settings.get("oauth_required")
        or settings.get("confirmation_link_required")
        or settings.get("email_confirmation_link_enabled")
    ):
        raise SiteError("站点启用了 Turnstile、图形验证码、OAuth 或确认链接，需人工处理。")
    return settings


def resolve_email_mode(profile: dict[str, Any], settings: dict[str, Any], requested: str) -> str:
    requested = str(requested or "auto").lower()
    marker = settings.get("email_verify_enabled")
    if isinstance(marker, str):
        lowered = marker.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            marker = True
        elif lowered in {"false", "0", "no", "off"}:
            marker = False
    if requested == "direct":
        if marker is True:
            raise SiteError("该网站要求邮箱验证码，请选择方案二。")
        return requested
    if requested == "receiver":
        if marker is False:
            raise SiteError("该网站无需邮箱验证码，请选择方案一。")
        return requested
    if marker is True:
        return "receiver"
    if marker is False:
        return "direct"
    fallback = str((profile.get("verification") or {}).get("mode") or "auto").lower()
    if fallback in {"direct", "receiver"}:
        return fallback
    raise SiteError("站点设置未返回 email_verify_enabled，请在网站协议中设置 verification.mode 为 direct 或 receiver。")


def _new_account_record(
    profile: dict[str, Any],
    email: str,
    *,
    account_kind: str,
    route: str,
    email_mode: str,
    mailbox: dict[str, Any] | None = None,
) -> dict[str, Any]:
    receiver_data: dict[str, Any] = {}
    if mailbox:
        receiver_data = {
            key: mailbox[key]
            for key in ("id", "email", "token", "page_url", "api_url")
            if mailbox.get(key) not in (None, "")
        }
    return {
        "schema_version": 2,
        "site": {
            "id": profile["id"],
            "name": profile["name"],
            "home_url": profile["home_url"],
            "api_base": profile["api_base"],
        },
        "account_kind": account_kind,
        "route": route,
        "email": email,
        "password": FIXED_ACCOUNT_PASSWORD,
        "registration": {},
        "api_key": "",
        "group": {},
        "key": {},
        "key_history": [],
        "email_provisioning": {"mode": email_mode, "receiver": receiver_data},
        "provisioning": {
            "status": "in_progress",
            "stage": "prepare_email",
            "error": "",
            "stages": {"prepare_email": "in_progress"},
            "errors": [],
        },
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def test_receiver_delivery(
    profile: dict[str, Any], receiver_config: dict[str, Any], *, use_proxy: bool, proxy_url: str = ""
) -> VerificationTestResult:
    settings = registration_settings(profile, use_proxy=use_proxy, proxy_url=proxy_url)
    resolve_email_mode(profile, settings, "receiver")
    if not isinstance((profile.get("operations") or {}).get("send_verification_code"), dict):
        raise SiteError("站点协议缺少 send_verification_code 接口。")
    try:
        mailbox = email_receiver.create_mailbox(receiver_config, use_proxy=use_proxy, proxy_url=proxy_url)
    except Exception as exc:
        raise SiteError(f"create_mailbox: {exc}") from exc
    token = str(mailbox.get("token") or "")
    try:
        baseline = email_receiver.mailbox_snapshot(
            receiver_config, token, use_proxy=use_proxy, proxy_url=proxy_url
        )
    except Exception:
        baseline = set()
    try:
        request_operation(
            profile, "send_verification_code", {"email": mailbox["email"]},
            use_proxy=use_proxy, proxy_url=proxy_url,
        )
    except Exception as exc:
        raise SiteError(f"send_verification_code: 目标网站发送验证码失败: {exc}") from exc
    try:
        receipt = email_receiver.wait_for_code_details(
            receiver_config, token,
            code_pattern=str((profile.get("verification") or {}).get("code_pattern") or r"\b\d{4,8}\b"),
            use_proxy=use_proxy, proxy_url=proxy_url, ignore_signatures=baseline,
        )
    except Exception as exc:
        raise SiteError(f"wait_verification_code: {exc}") from exc
    return VerificationTestResult(
        site_id=str(profile["id"]), email=str(mailbox["email"]), subject=receipt.subject,
        elapsed_seconds=receipt.elapsed_seconds, poll_count=receipt.poll_count,
        email_count=receipt.email_count, code_length=len(receipt.code),
    )


def _set_provisioning(data: dict[str, Any], status: str, stage: str, error: str = "") -> None:
    provisioning = data.get("provisioning") if isinstance(data.get("provisioning"), dict) else {}
    stages = provisioning.get("stages") if isinstance(provisioning.get("stages"), dict) else {}
    errors = provisioning.get("errors") if isinstance(provisioning.get("errors"), list) else []
    redacted = redact_log_text(error)[:500]
    stage_state = "failed" if redacted else ("complete" if status == "complete" else "in_progress")
    stages[str(stage)] = stage_state
    if redacted:
        entry = {"stage": str(stage), "message": redacted}
        if entry not in errors:
            errors.append(entry)
    provisioning.update(
        status=status,
        stage=stage,
        error=redacted,
        stages=stages,
        errors=errors,
        updated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    data["provisioning"] = provisioning


def _complete_stage(data: dict[str, Any], stage: str) -> None:
    provisioning = data.setdefault("provisioning", {})
    stages = provisioning.setdefault("stages", {})
    stages[str(stage)] = "complete"


def _account_output_path(output_dir: Path, profile: dict[str, Any], email: str, account_kind: str) -> Path:
    local = re.sub(r"[^A-Za-z0-9_.-]+", "_", email.split("@", 1)[0])
    site_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(profile["id"]))
    kind_slug = "claude" if account_kind == "claude" else "codex"
    return output_dir / f"xhhai_{kind_slug}_account_{site_slug}_{local}.json"


def _register_into_record(
    profile: dict[str, Any],
    data: dict[str, Any],
    output: Path,
    *,
    verification_code: str = "",
    use_proxy: bool,
    proxy_url: str,
) -> None:
    result, raw = request_operation(
        profile,
        "register",
        {
            "email": data["email"],
            "password": FIXED_ACCOUNT_PASSWORD,
            "verification_code": verification_code,
        },
        use_proxy=use_proxy,
        proxy_url=proxy_url,
    )
    operation = (profile.get("operations") or {}).get("register") or {}
    registration = canonical_response(operation, result)
    if not str(registration.get("access_token") or ""):
        if "login" not in (profile.get("operations") or {}):
            raise SiteError("注册响应中没有映射到 access_token，且站点未配置 login 接口。")
        login_result, login_raw = request_operation(
            profile,
            "login",
            {"email": data["email"], "password": FIXED_ACCOUNT_PASSWORD},
            use_proxy=use_proxy,
            proxy_url=proxy_url,
        )
        login_operation = (profile.get("operations") or {}).get("login") or {}
        login_registration = canonical_response(login_operation, login_result)
        if not str(login_registration.get("access_token") or ""):
            raise SiteError("注册后的登录响应中没有映射到 access_token。")
        if registration.get("user") and not login_registration.get("user"):
            login_registration["user"] = registration["user"]
        registration = login_registration
        registration["provider_response"] = {"register": raw, "login": login_raw}
    else:
        registration["provider_response"] = raw
    data["registration"] = registration
    _complete_stage(data, "prepare_email")
    _complete_stage(data, "register")
    _set_provisioning(data, "in_progress", "registered")
    _complete_stage(data, "registered")
    output.parent.mkdir(parents=True, exist_ok=True)
    _save_account(output, data)


def _created_key(profile: dict[str, Any], result: Any, raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    operation = (profile.get("operations") or {}).get("create_key") or {}
    canonical = canonical_response(operation, result)
    api_key = str(canonical.get("api_key") or "")
    if not api_key:
        raise SiteError("创建 Key 的响应没有映射到 api_key。")
    key_record = dict(canonical)
    key_record["key"] = api_key
    key_record["provider_response"] = raw
    return api_key, key_record


def _provision_key_and_group(
    profile: dict[str, Any],
    output: Path,
    data: dict[str, Any],
    *,
    key_name: str,
    use_proxy: bool,
    proxy_url: str,
) -> None:
    token = str((data.get("registration") or {}).get("access_token") or "")
    if not token:
        token = ensure_access_token(profile, output, data, use_proxy=use_proxy, proxy_url=proxy_url)
    create_requires_group = bool((profile.get("key_workflow") or {}).get("create_requires_group", True))
    variables: dict[str, Any] = {"access_token": token, "key_name": key_name}
    if create_requires_group:
        _set_provisioning(data, "in_progress", "detect_group")
        _save_account(output, data)
        groups_result, _ = request_operation(
            profile, "groups", {"access_token": token}, use_proxy=use_proxy, proxy_url=proxy_url
        )
        groups = normalize_groups(profile, groups_result)
        if not groups:
            raise SiteError("站点没有返回可用分组。")
        group = groups[0]
        _complete_stage(data, "detect_group")
        variables["group_id"] = group["id"]
        _set_provisioning(data, "in_progress", "create_key")
        _save_account(output, data)
        result, raw = request_operation(profile, "create_key", variables, use_proxy=use_proxy, proxy_url=proxy_url)
        api_key, key_record = _created_key(profile, result, raw)
        data["api_key"] = api_key
        data["key"] = key_record
        data["group"] = {"id": group["id"], "name": group["name"]}
        _complete_stage(data, "create_key")
    else:
        api_key = str(data.get("api_key") or (data.get("key") or {}).get("key") or "")
        key_record = data.get("key") if isinstance(data.get("key"), dict) else {}
        if not api_key:
            _set_provisioning(data, "in_progress", "create_key")
            _save_account(output, data)
            result, raw = request_operation(profile, "create_key", variables, use_proxy=use_proxy, proxy_url=proxy_url)
            api_key, key_record = _created_key(profile, result, raw)
            data["api_key"] = api_key
            data["key"] = key_record
            data["key_created_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            _complete_stage(data, "create_key")
            _save_account(output, data)
        _set_provisioning(data, "in_progress", "detect_group")
        _save_account(output, data)
        groups_result, _ = request_operation(
            profile, "groups", {"access_token": token}, use_proxy=use_proxy, proxy_url=proxy_url
        )
        groups = normalize_groups(profile, groups_result)
        if not groups:
            raise SiteError("站点没有返回可用分组。")
        group = groups[0]
        _complete_stage(data, "detect_group")
        _set_provisioning(data, "in_progress", "update_key_group")
        _save_account(output, data)
        request_operation(
            profile,
            "update_key_group",
            {
                "access_token": token,
                "api_key": api_key,
                "key_id": key_record.get("id"),
                "group_id": group["id"],
            },
            use_proxy=use_proxy,
            proxy_url=proxy_url,
        )
        data["group"] = {"id": group["id"], "name": group["name"]}
        _complete_stage(data, "update_key_group")
    data["key_created_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _complete_stage(data, "key")
    _complete_stage(data, "group")
    _set_provisioning(data, "complete", "complete")
    _save_account(output, data)


def provision_account(
    profile: dict[str, Any],
    email: str,
    *,
    output_dir: Path,
    account_kind: str,
    route: str,
    email_mode: str,
    key_name: str,
    use_proxy: bool,
    proxy_url: str = "",
    overwrite: bool = False,
    receiver_config: dict[str, Any] | None = None,
    mailbox: dict[str, Any] | None = None,
    discovery: dict[str, Any] | None = None,
) -> ProvisionResult:
    email = normalize_email(email)
    output = _account_output_path(output_dir, profile, email, account_kind)
    data = _new_account_record(
        profile,
        email,
        account_kind=account_kind,
        route=route or "默认",
        email_mode=email_mode,
        mailbox=mailbox,
    )
    if discovery:
        data["discovery"] = discovery
    balance_value: float | None = None
    balance_error = ""
    try:
        if output.exists() and not overwrite:
            raise SiteError(f"账号文件已存在: {output}")
        verification_code = ""
        if email_mode == "receiver":
            _complete_stage(data, "create_mailbox")
            output.parent.mkdir(parents=True, exist_ok=True)
            _save_account(output, data)
            _set_provisioning(data, "in_progress", "send_verification_code")
            _save_account(output, data)
            request_operation(
                profile,
                "send_verification_code",
                {"email": email},
                use_proxy=use_proxy,
                proxy_url=proxy_url,
            )
            _complete_stage(data, "send_verification_code")
            _set_provisioning(data, "in_progress", "wait_verification_code")
            _save_account(output, data)
            verification_code = email_receiver.wait_for_code(
                receiver_config or {},
                str((mailbox or {}).get("token") or ""),
                code_pattern=str((profile.get("verification") or {}).get("code_pattern") or r"\b\d{4,8}\b"),
                use_proxy=use_proxy,
                proxy_url=proxy_url,
            )
            _complete_stage(data, "wait_verification_code")
        _set_provisioning(data, "in_progress", "register")
        if output.exists():
            _save_account(output, data)
        _register_into_record(
            profile,
            data,
            output,
            verification_code=verification_code,
            use_proxy=use_proxy,
            proxy_url=proxy_url,
        )

        _set_provisioning(data, "in_progress", "balance")
        _save_account(output, data)
        try:
            balance_value, _ = query_balance(profile, output, use_proxy=use_proxy, proxy_url=proxy_url)
            data = read_account(output)
        except Exception as exc:
            balance_error = redact_log_text(exc)[:500]
            data = read_account(output)
            data["balance_check"] = {
                "status": "failed",
                "balance": None,
                "error": balance_error,
                "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            _set_provisioning(data, "in_progress", "balance", balance_error)
            _save_account(output, data)

        try:
            _provision_key_and_group(
                profile,
                output,
                data,
                key_name=key_name,
                use_proxy=use_proxy,
                proxy_url=proxy_url,
            )
            data = read_account(output)
        except Exception as exc:
            data = read_account(output)
            stage = str((data.get("provisioning") or {}).get("stage") or "key_workflow")
            _set_provisioning(data, "partial", stage, str(exc))
            _save_account(output, data)
            errors = list((data.get("provisioning") or {}).get("errors") or [])
            return ProvisionResult(
                "partial", output, email, balance_value,
                "failed" if balance_error else "success",
                "success" if account_has_key(data) else "failed",
                "success" if bool(data.get("group")) else "failed",
                errors,
            )

        if balance_error:
            _set_provisioning(data, "partial", "balance", balance_error)
            _save_account(output, data)
            status = "partial"
        else:
            _set_provisioning(data, "complete", "complete")
            _save_account(output, data)
            status = "complete"
        errors = list((data.get("provisioning") or {}).get("errors") or [])
        return ProvisionResult(status, output, email, balance_value, "failed" if balance_error else "success", "success", "success", errors)
    except Exception as exc:
        if output.exists():
            data = read_account(output)
            stage = str((data.get("provisioning") or {}).get("stage") or "register")
            _set_provisioning(data, "failed", stage, str(exc))
            _save_account(output, data)
        return ProvisionResult(
            "failed", output, email, balance_value, "failed" if balance_error else "pending",
            "pending", "pending", [{
                "stage": str((data.get("provisioning") or {}).get("stage") or "register"),
                "message": redact_log_text(exc)[:500],
            }],
        )


def retry_provision(
    profile: dict[str, Any],
    path: Path,
    *,
    key_name: str,
    receiver_config: dict[str, Any] | None,
    use_proxy: bool,
    proxy_url: str = "",
) -> str:
    data = read_account(path)
    if account_site_id(data) != str(profile["id"]):
        raise SiteError("账号与所选网站不一致。")
    mode = str((data.get("email_provisioning") or {}).get("mode") or "direct")
    try:
        if not str((data.get("registration") or {}).get("access_token") or ""):
            code = ""
            if mode == "receiver":
                mailbox = (data.get("email_provisioning") or {}).get("receiver") or {}
                token = str(mailbox.get("token") or "")
                if not token:
                    raise SiteError("账号缺少临时邮箱 token，请重新发起注册。")
                try:
                    baseline = email_receiver.mailbox_snapshot(
                        receiver_config or {}, token, use_proxy=use_proxy, proxy_url=proxy_url
                    )
                except Exception:
                    baseline = set()
                _set_provisioning(data, "in_progress", "send_verification_code")
                _save_account(path, data)
                request_operation(
                    profile, "send_verification_code", {"email": data["email"]}, use_proxy=use_proxy, proxy_url=proxy_url
                )
                _complete_stage(data, "send_verification_code")
                _set_provisioning(data, "in_progress", "wait_verification_code")
                _save_account(path, data)
                code = email_receiver.wait_for_code(
                    receiver_config or {}, token,
                    code_pattern=str((profile.get("verification") or {}).get("code_pattern") or r"\b\d{4,8}\b"),
                    use_proxy=use_proxy, proxy_url=proxy_url, ignore_signatures=baseline,
                )
                _complete_stage(data, "wait_verification_code")
            _register_into_record(profile, data, path, verification_code=code, use_proxy=use_proxy, proxy_url=proxy_url)
        balance_error = ""
        if str((data.get("balance_check") or {}).get("status") or "") != "success":
            try:
                query_balance(profile, path, use_proxy=use_proxy, proxy_url=proxy_url)
                data = read_account(path)
            except Exception as exc:
                balance_error = redact_log_text(exc)[:500]
                data = read_account(path)
                data["balance_check"] = {
                    "status": "failed", "balance": None, "error": balance_error,
                    "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
                _set_provisioning(data, "in_progress", "balance", balance_error)
                _save_account(path, data)
        if account_has_key(data) and data.get("group"):
            _set_provisioning(data, "partial" if balance_error else "complete", "balance" if balance_error else "complete", balance_error)
            _save_account(path, data)
            return "partial" if balance_error else "complete"
        _provision_key_and_group(profile, path, data, key_name=key_name, use_proxy=use_proxy, proxy_url=proxy_url)
        data = read_account(path)
        if balance_error:
            _set_provisioning(data, "partial", "balance", balance_error)
            _save_account(path, data)
            return "partial"
        return "complete"
    except Exception as exc:
        latest = read_account(path)
        registered = bool(str((latest.get("registration") or {}).get("access_token") or ""))
        stage = str((latest.get("provisioning") or {}).get("stage") or "retry")
        _set_provisioning(latest, "partial" if registered else "failed", stage, str(exc))
        _save_account(path, latest)
        raise


def register_batch(
    profile: dict[str, Any],
    *,
    count: int,
    email_domain: str,
    output_dir: Path,
    account_kind: str,
    route: str,
    use_proxy: bool,
    proxy_url: str = "",
    overwrite: bool = False,
    concurrency: int = 5,
    emails_file: Path | None = None,
    email_rows: Iterable[tuple[str, str | None]] | None = None,
    password: str | None = None,
    dry_run: bool = False,
    email_mode: str = "auto",
    key_name: str = "codex",
    receiver_config: dict[str, Any] | None = None,
) -> tuple[int, int, int]:
    total = max(0, int(count))
    supplied = [*(email_rows or []), *read_email_rows(emails_file)]
    total += len(supplied)
    if total <= 0:
        raise SiteError("没有待注册邮箱。")
    settings = registration_settings(profile, use_proxy=use_proxy, proxy_url=proxy_url)
    selected_mode = resolve_email_mode(profile, settings, email_mode)
    operations = profile.get("operations") or {}
    required = ["register", "groups", "create_key"]
    if selected_mode == "receiver":
        required.append("send_verification_code")
    if (profile.get("key_workflow") or {}).get("create_requires_group") is False:
        required.append("update_key_group")
    missing = [name for name in required if not isinstance(operations.get(name), dict)]
    if missing:
        raise SiteError(f"站点协议缺少完整开户接口: {', '.join(missing)}")
    print(f"[PROVISION] site={profile['id']} email_mode={selected_mode} total={total}", flush=True)
    if dry_run:
        print(f"[PROVISION] dry_run=true fixed_password=true key_name={key_name}", flush=True)
        return total, 0, 0

    jobs: list[tuple[str, dict[str, Any] | None]] = []
    if selected_mode == "receiver":
        jobs = [("", None) for _ in range(total)]
    else:
        domain = normalize_email_domain(email_domain)
        direct_emails = [normalize_email(row[0]) for row in supplied]
        direct_emails.extend(random_email(domain) for _ in range(max(0, int(count))))
        seen: set[str] = set()
        jobs = []
        for email in direct_emails:
            if email.casefold() not in seen:
                jobs.append((email, None))
                seen.add(email.casefold())

    def work(index: int, email: str, mailbox: dict[str, Any] | None) -> tuple[str, str]:
        if selected_mode == "receiver" and mailbox is None:
            try:
                mailbox = email_receiver.create_mailbox(
                    receiver_config or {}, use_proxy=use_proxy, proxy_url=proxy_url
                )
                email = normalize_email(str(mailbox["email"]))
            except Exception as exc:
                return "failed", (
                    f"[PROVISION] status=failed stage=create_mailbox index={index} "
                    f"error={redact_log_text(exc)[:300]}"
                )
        result = provision_account(
            profile,
            email,
            output_dir=output_dir,
            account_kind=account_kind,
            route=route or "默认",
            email_mode=selected_mode,
            key_name=key_name,
            use_proxy=use_proxy,
            proxy_url=proxy_url,
            overwrite=overwrite,
            receiver_config=receiver_config,
            mailbox=mailbox,
        )
        error = result.errors[-1]["message"] if result.errors else ""
        message = (
            f"[PROVISION] status={result.status} index={index} email={email} "
            f"balance={result.balance if result.balance is not None else 'unknown'} path={result.path}"
        )
        if error:
            message += f" error={redact_log_text(error)[:300]}"
        return result.status, message

    complete = partial = failed = 0
    with ThreadPoolExecutor(max_workers=max(1, min(int(concurrency), 50))) as executor:
        futures = [executor.submit(work, index, email, mailbox) for index, (email, mailbox) in enumerate(jobs, 1)]
        for future in as_completed(futures):
            status, message = future.result()
            print(message, flush=True)
            complete += int(status == "complete")
            partial += int(status == "partial")
            failed += int(status == "failed")
    print(
        f"[SUMMARY] operation=provision total={len(jobs)} complete={complete} partial={partial} failed={failed}",
        flush=True,
    )
    return complete, partial, failed


def read_account(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise SiteError(f"账号文件不是 JSON 对象: {path}")
    needs_migration = data.get("schema_version") != 2 or not isinstance(data.get("site"), dict)
    if not isinstance(data.get("site"), dict):
        data["site"] = {
            "id": "xhhai",
            "name": "XHHAI",
            "home_url": str(data.get("site") or "https://api.xhhai.top"),
            "api_base": "https://api.xhhai.top/api/v1",
        }
    data.setdefault("account_kind", "codex")
    data.setdefault("route", "")
    data.setdefault("key_history", [])
    provisioning = data.get("provisioning")
    if not isinstance(provisioning, dict):
        provisioning = {"status": "complete" if account_has_key(data) else "partial", "stage": "legacy", "error": ""}
        data["provisioning"] = provisioning
        needs_migration = True
    if not isinstance(provisioning.get("stages"), dict):
        provisioning["stages"] = {}
        needs_migration = True
    if not isinstance(provisioning.get("errors"), list):
        provisioning["errors"] = []
        if str(provisioning.get("error") or ""):
            provisioning["errors"].append({"stage": str(provisioning.get("stage") or "legacy"), "message": str(provisioning["error"])})
        needs_migration = True
    registration = data.get("registration")
    if not isinstance(registration, dict):
        registration = {}
        data["registration"] = registration
    for field in ("access_token", "refresh_token", "expires_in", "token_type", "user"):
        if field not in registration and field in data:
            registration[field] = data[field]
            needs_migration = True
    if needs_migration:
        _save_account(path, data)
    return data


def account_site_id(data: dict[str, Any]) -> str:
    site = data.get("site")
    if isinstance(site, dict):
        return str(site.get("id") or "xhhai")
    return "xhhai"


def rebind_account_site(profile: dict[str, Any], path: Path) -> None:
    data = read_account(path)
    data["site"] = {
        "id": profile["id"],
        "name": profile["name"],
        "home_url": profile["home_url"],
        "api_base": profile["api_base"],
    }
    data["site_rebound_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _save_account(path, data)


def _save_account(path: Path, data: dict[str, Any]) -> None:
    data["schema_version"] = 2
    write_json_atomic(path, data, trailing_newline=True)


def _normalized_token_response(profile: dict[str, Any], operation_name: str, result: Any) -> dict[str, Any]:
    operation = (profile.get("operations") or {}).get(operation_name) or {}
    return canonical_response(operation, result)


def ensure_access_token(profile: dict[str, Any], path: Path, data: dict[str, Any], *, use_proxy: bool, proxy_url: str = "") -> str:
    registration = data.setdefault("registration", {})
    access_token = str(registration.get("access_token") or "")
    refresh_token = str(registration.get("refresh_token") or "")
    if access_token:
        return access_token
    if refresh_token and "refresh" in (profile.get("operations") or {}):
        try:
            result, _ = request_operation(profile, "refresh", {"refresh_token": refresh_token}, use_proxy=use_proxy, proxy_url=proxy_url)
            updated = _normalized_token_response(profile, "refresh", result)
            if updated.get("access_token"):
                registration.update(updated)
                _save_account(path, data)
                return str(updated["access_token"])
        except Exception:
            pass
    email = str(data.get("email") or "")
    password = str(data.get("password") or "")
    if email and password and "login" in (profile.get("operations") or {}):
        result, _ = request_operation(profile, "login", {"email": email, "password": password}, use_proxy=use_proxy, proxy_url=proxy_url)
        updated = _normalized_token_response(profile, "login", result)
        if updated.get("access_token"):
            registration.update(updated)
            _save_account(path, data)
            return str(updated["access_token"])
    raise SiteError(f"账号缺少可用登录令牌: {data.get('email') or path.name}")


def refresh_account_token(profile: dict[str, Any], path: Path, *, use_proxy: bool, proxy_url: str = "") -> str:
    """Refresh one account, falling back to login, and persist normalized tokens."""
    data = read_account(path)
    if account_site_id(data) != str(profile["id"]):
        raise SiteError(f"账号与网站配置不一致: {path}")
    registration = data.setdefault("registration", {})
    refresh_token = str(registration.get("refresh_token") or "")
    refresh_error: Exception | None = None
    if refresh_token and "refresh" in (profile.get("operations") or {}):
        try:
            result, _ = request_operation(profile, "refresh", {"refresh_token": refresh_token}, use_proxy=use_proxy, proxy_url=proxy_url)
            updated = _normalized_token_response(profile, "refresh", result)
            if not updated.get("access_token"):
                raise SiteError("刷新响应没有映射到 access_token。")
            registration.update(updated)
            _save_account(path, data)
            return "refresh_token"
        except Exception as exc:
            refresh_error = exc
    email = str(data.get("email") or "")
    password = str(data.get("password") or "")
    if email and password and "login" in (profile.get("operations") or {}):
        result, _ = request_operation(profile, "login", {"email": email, "password": password}, use_proxy=use_proxy, proxy_url=proxy_url)
        updated = _normalized_token_response(profile, "login", result)
        if not updated.get("access_token"):
            raise SiteError("登录响应没有映射到 access_token。")
        registration.update(updated)
        _save_account(path, data)
        return "login"
    if refresh_error is not None:
        raise SiteError(f"刷新令牌失败: {refresh_error}") from refresh_error
    if registration.get("access_token"):
        return "access_token"
    raise SiteError(f"账号缺少可刷新或登录的凭据: {data.get('email') or path.name}")


def account_has_key(data: dict[str, Any]) -> bool:
    key = data.get("key") if isinstance(data.get("key"), dict) else {}
    return bool(str(data.get("api_key") or key.get("key") or key.get("api_key") or "").strip())


def needs_registration_retry(data: dict[str, Any]) -> bool:
    """Return true only for an account that never reached a logged-in state.

    A legacy account may legitimately need to log in again before a balance
    request, so it is not rejected merely because its token is absent.  This
    guard is specifically for a saved provisioning failure before registration
    completed (for example an email-verification failure).
    """
    registration = data.get("registration") if isinstance(data.get("registration"), dict) else {}
    if str(registration.get("access_token") or registration.get("refresh_token") or "").strip():
        return False
    provisioning = data.get("provisioning") if isinstance(data.get("provisioning"), dict) else {}
    status = str(provisioning.get("status") or "").casefold()
    stage = str(provisioning.get("stage") or "").casefold()
    before_registration = {
        "create_mailbox",
        "send_verification_code",
        "wait_verification_code",
        "register",
    }
    return status == "failed" and stage in before_registration


def _looks_like_auth_error(exc: Exception) -> bool:
    text = str(exc).casefold()
    return any(marker in text for marker in ("http 401", "http 403", "unauthorized", "expired", "jwt", "token", "未登录", "认证"))


def query_balance(profile: dict[str, Any], path: Path, *, use_proxy: bool, proxy_url: str = "") -> tuple[float, bool]:
    """Query and cache balance. Returns (balance, token_was_refreshed)."""
    data = read_account(path)
    if account_site_id(data) != str(profile["id"]):
        raise SiteError(f"账号与网站配置不一致: {path}")
    before = str((data.get("registration") or {}).get("access_token") or "")
    token = ensure_access_token(profile, path, data, use_proxy=use_proxy, proxy_url=proxy_url)
    try:
        result, _ = request_operation(profile, "balance", {"access_token": token}, use_proxy=use_proxy, proxy_url=proxy_url)
    except Exception as exc:
        if not _looks_like_auth_error(exc):
            raise
        refresh_account_token(profile, path, use_proxy=use_proxy, proxy_url=proxy_url)
        data = read_account(path)
        token = str((data.get("registration") or {}).get("access_token") or "")
        result, _ = request_operation(profile, "balance", {"access_token": token}, use_proxy=use_proxy, proxy_url=proxy_url)
    operation = (profile.get("operations") or {}).get("balance") or {}
    canonical = canonical_response(operation, result)
    value = canonical.get("balance")
    try:
        balance = float(value)
    except (TypeError, ValueError) as exc:
        raise SiteError("余额响应没有映射到可用的 balance 数值。") from exc
    latest = read_account(path)
    initial_raw = latest.get("balance_initial")
    try:
        initial = float(initial_raw) if initial_raw is not None else balance
    except (TypeError, ValueError):
        initial = balance
    latest["balance_initial"] = initial
    latest["balance_cache"] = {
        "balance": balance,
        "initial": initial,
        "consumed": max(initial - balance, 0.0),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    latest["balance_check"] = {
        "status": "success",
        "balance": balance,
        "error": "",
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _complete_stage(latest, "balance")
    _save_account(path, latest)
    after = str((latest.get("registration") or {}).get("access_token") or "")
    return balance, bool(before and after and before != after)


def groups_for_account(profile: dict[str, Any], path: Path, *, use_proxy: bool, proxy_url: str = "") -> list[dict[str, Any]]:
    data = read_account(path)
    if account_site_id(data) != str(profile["id"]):
        raise SiteError(f"账号与所选网站不一致: {path}")
    token = ensure_access_token(profile, path, data, use_proxy=use_proxy, proxy_url=proxy_url)
    result, _ = request_operation(profile, "groups", {"access_token": token}, use_proxy=use_proxy, proxy_url=proxy_url)
    return normalize_groups(profile, result)


def common_groups(profile: dict[str, Any], paths: Iterable[Path], *, use_proxy: bool, proxy_url: str = "") -> list[dict[str, Any]]:
    common: dict[str, dict[str, Any]] | None = None
    for path in paths:
        groups = groups_for_account(profile, path, use_proxy=use_proxy, proxy_url=proxy_url)
        current = {str(item["id"]): item for item in groups}
        common = current if common is None else {group_id: common[group_id] for group_id in common.keys() & current.keys()}
    return sorted((common or {}).values(), key=lambda item: str(item.get("name") or "").casefold())


def add_key(
    profile: dict[str, Any],
    path: Path,
    *,
    group_id: Any,
    group_name: str,
    key_name: str,
    include_existing: bool,
    use_proxy: bool,
    proxy_url: str = "",
) -> str:
    data = read_account(path)
    if account_site_id(data) != str(profile["id"]):
        raise SiteError("账号与所选网站不一致。")
    current_key = str(data.get("api_key") or (data.get("key") or {}).get("key") or "")
    if current_key and not include_existing:
        return "skipped"
    token = ensure_access_token(profile, path, data, use_proxy=use_proxy, proxy_url=proxy_url)
    result, raw = request_operation(
        profile,
        "create_key",
        {"access_token": token, "group_id": group_id, "key_name": key_name},
        use_proxy=use_proxy,
        proxy_url=proxy_url,
    )
    operation = (profile.get("operations") or {}).get("create_key") or {}
    canonical = canonical_response(operation, result)
    api_key = str(canonical.get("api_key") or "")
    if not api_key:
        raise SiteError("创建 Key 的响应没有映射到 api_key。")
    if current_key:
        history = data.setdefault("key_history", [])
        history.append({
            "api_key": current_key,
            "group": data.get("group") or {},
            "key": data.get("key") or {},
            "replaced_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
    key_record = dict(canonical)
    key_record["key"] = api_key
    key_record["provider_response"] = raw
    data["api_key"] = api_key
    data["group"] = {"id": group_id, "name": group_name}
    data["key"] = key_record
    data["key_created_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _save_account(path, data)
    return "created"


def add_keys(
    profile: dict[str, Any],
    paths: Iterable[Path],
    *,
    group_id: Any,
    group_name: str,
    key_name: str,
    include_existing: bool,
    use_proxy: bool,
    proxy_url: str = "",
    delay_seconds: float = 0.0,
) -> tuple[int, int, int]:
    ok_count = fail_count = skipped = 0
    selected = list(paths)
    for index, path in enumerate(selected, 1):
        try:
            state = add_key(
                profile,
                path,
                group_id=group_id,
                group_name=group_name,
                key_name=key_name,
                include_existing=include_existing,
                use_proxy=use_proxy,
                proxy_url=proxy_url,
            )
            if state == "skipped":
                skipped += 1
                print(f"[KEY] ok=true skipped=true path={path}", flush=True)
            else:
                ok_count += 1
                print(f"[KEY] ok=true skipped=false path={path}", flush=True)
        except Exception as exc:
            fail_count += 1
            print(f"[KEY] ok=false path={path} error={redact_log_text(exc)[:400]}", flush=True)
        if delay_seconds > 0 and index < len(selected):
            time.sleep(delay_seconds)
    print(f"[SUMMARY] operation=add-keys total={len(selected)} ok={ok_count} skipped={skipped} failed={fail_count}", flush=True)
    return ok_count, skipped, fail_count
