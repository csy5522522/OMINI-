from __future__ import annotations

import copy
import hashlib
import json
import random
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from omini_api import omini_account_service as account_service
from omini_api import omini_email_receiver as email_receiver
from omini_api import omini_sites as site_profiles
from omini_api.xhhai_file_utils import write_json_atomic


DISCOVERY_WORDS = (
    "ai", "api", "openai", "gpt", "codex", "claude", "llm", "model", "proxy", "gateway",
    "oneapi", "newapi", "chat", "cloud", "hub", "plus", "pro", "fast", "free", "global",
    "token", "key", "relay", "router", "agent", "omni", "omini",
)
DISCOVERY_TLDS = ("com", "top", "xyz", "net", "io", "ai", "cn", "cc", "vip", "site")
HOST_RE = re.compile(r"^api\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(?:com|top|xyz|net|io|ai|cn|cc|vip|site)$")


@dataclass
class ProbeResult:
    host: str
    url: str
    compatible: bool
    stage: str
    reason: str = ""
    settings: dict[str, Any] | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def generate_candidates(count: int = 1000, *, seed: int | None = None) -> list[str]:
    count = max(1, int(count))
    rng: random.Random = random.Random(seed)
    word_target = round(count * 0.60)
    candidates: list[str] = []
    seen: set[str] = set()
    attempts = 0
    while len(candidates) < count:
        attempts += 1
        if attempts > count * 100:
            raise site_profiles.SiteError("候选域名生成失败，唯一组合不足。")
        if len(candidates) < word_target:
            label = rng.choice(DISCOVERY_WORDS)
            if rng.random() < 0.75:
                digits = rng.randint(10, 9999)
                label = f"{label}{digits}"
        else:
            length = rng.randint(5, 12)
            alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
            label = "".join(rng.choice(alphabet) for _ in range(length))
            if label[0].isdigit():
                label = rng.choice("abcdefghijklmnopqrstuvwxyz") + label[1:]
        host = f"api.{label}.{rng.choice(DISCOVERY_TLDS)}"
        if host not in seen:
            seen.add(host)
            candidates.append(host)
    return candidates


def normalize_candidate_host(value: str) -> str:
    text = str(value or "").strip().lower()
    parsed = urllib.parse.urlparse(text if "://" in text else f"https://{text}")
    host = str(parsed.hostname or "").rstrip(".")
    if not HOST_RE.fullmatch(host):
        raise site_profiles.SiteError(f"候选地址不符合 api.主体.TLD 格式: {value}")
    return host


def _probe_opener(use_proxy: bool, proxy_url: str):
    handlers: list[Any] = [_NoRedirect()]
    if use_proxy and proxy_url:
        handlers.insert(0, urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    elif not use_proxy:
        handlers.insert(0, urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers).open


def probe_candidate(
    host: str,
    *,
    use_proxy: bool = False,
    proxy_url: str = "",
    timeout: float = 5.0,
) -> ProbeResult:
    try:
        host = normalize_candidate_host(host)
    except Exception as exc:
        return ProbeResult(str(host), "", False, "format", str(exc))
    url = f"https://{host}/api/v1/settings/public"
    if not use_proxy:
        try:
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            return ProbeResult(host, url, False, "dns", str(exc))
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": "Omini-API-Toolbox/2.0"},
    )
    try:
        with _probe_opener(use_proxy, proxy_url)(request, timeout=timeout) as response:
            if response.status != 200:
                return ProbeResult(host, url, False, "https", f"HTTP {response.status}")
            raw_text = response.read(1_048_577)
            if len(raw_text) > 1_048_576:
                return ProbeResult(host, url, False, "json", "settings 响应超过 1 MiB")
    except urllib.error.HTTPError as exc:
        stage = "redirect" if 300 <= exc.code < 400 else "https"
        return ProbeResult(host, url, False, stage, f"HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
        return ProbeResult(host, url, False, "tls", str(exc))
    try:
        raw = json.loads(raw_text.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        return ProbeResult(host, url, False, "json", str(exc))
    data = raw.get("data") if isinstance(raw, dict) and raw.get("code") == 0 else None
    if not isinstance(data, dict):
        return ProbeResult(host, url, False, "fingerprint", "缺少 code=0 或 data 对象")
    if not isinstance(data.get("registration_enabled"), bool) or not isinstance(data.get("email_verify_enabled"), bool):
        return ProbeResult(host, url, False, "fingerprint", "缺少注册/邮箱验证布尔字段")
    if data["registration_enabled"] is not True:
        return ProbeResult(host, url, False, "registration_closed", "站点已关闭注册", data)
    return ProbeResult(host, url, True, "compatible", "", data)


def discovered_profile(host: str, settings: dict[str, Any]) -> dict[str, Any]:
    host = normalize_candidate_host(host)
    profile = copy.deepcopy(site_profiles.default_xhhai_profile())
    digest = hashlib.sha256(host.encode("ascii")).hexdigest()[:12]
    profile.update(
        id=f"discovered-{digest}",
        name=str(settings.get("site_name") or host),
        home_url=f"https://{host}",
        api_base=f"https://{host}/api/v1",
        enabled=True,
        is_default=False,
        discovery={
            "source": "random-api-host-search",
            "host": host,
            "discovered_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
    )
    return profile


def append_discovery_log(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = {
        key: value for key, value in event.items()
        if key not in {"password", "access_token", "refresh_token", "api_key", "token", "verification_code"}
    }
    with path.open("a", encoding="utf-8", newline="") as stream:
        stream.write(json.dumps(safe, ensure_ascii=False, separators=(",", ":")) + "\n")


def promote_profile(store: dict[str, Any], profile: dict[str, Any]) -> str:
    api_base = str(profile["api_base"]).rstrip("/").casefold()
    for current in store.setdefault("sites", []):
        if str(current.get("api_base") or "").rstrip("/").casefold() == api_base or str(current.get("id")) == str(profile["id"]):
            existing_id = str(current["id"])
            was_default = bool(current.get("is_default"))
            updated = copy.deepcopy(profile)
            updated["id"] = existing_id
            updated["is_default"] = was_default
            updated["enabled"] = True
            current.clear()
            current.update(updated)
            return existing_id
    store["sites"].append(copy.deepcopy(profile))
    return str(profile["id"])


def move_promoted_account(path: Path, accounts_root: Path) -> Path:
    source = path.resolve()
    root = accounts_root.resolve()
    source.relative_to(root)
    target_dir = (root / "codex").resolve()
    target_dir.relative_to(root)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    if target.exists() and target.resolve() != source:
        target = target_dir / f"{source.stem}_{hashlib.sha256(str(source).encode()).hexdigest()[:8]}{source.suffix}"
    source.replace(target)
    data = account_service.read_account(target)
    discovery = data.setdefault("discovery", {})
    discovery["promoted"] = True
    discovery["promoted_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    write_json_atomic(target, data, trailing_newline=True)
    return target


def discover_sites(
    *,
    count: int,
    probe_concurrency: int,
    provision_concurrency: int,
    email_domain: str,
    email_mode: str,
    key_name: str,
    receiver_config: dict[str, Any],
    accounts_root: Path,
    site_store_path: Path,
    log_path: Path,
    use_proxy: bool,
    proxy_url: str = "",
    seed: int | None = None,
    candidates: Iterable[str] | None = None,
    probe_func: Callable[..., ProbeResult] = probe_candidate,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, int]:
    store = site_profiles.load_site_store(site_store_path)
    known = {str(item.get("api_base") or "").rstrip("/").casefold() for item in store.get("sites", [])}
    hosts = list(candidates) if candidates is not None else generate_candidates(count, seed=seed)
    hosts = [normalize_candidate_host(host) for host in hosts]
    hosts = [host for host in dict.fromkeys(hosts) if f"https://{host}/api/v1".casefold() not in known]
    generated_event = {"event": "generated", "total": len(hosts)}
    print(json.dumps(generated_event, ensure_ascii=False), flush=True)
    append_discovery_log(log_path, generated_event)
    compatible: list[ProbeResult] = []
    probed = 0
    with ThreadPoolExecutor(max_workers=max(1, min(int(probe_concurrency), 100))) as executor:
        futures = {
            executor.submit(probe_func, host, use_proxy=use_proxy, proxy_url=proxy_url): host
            for host in hosts
        }
        for future in as_completed(futures):
            if stop_requested and stop_requested():
                for pending in futures:
                    pending.cancel()
                break
            host = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = ProbeResult(host, f"https://{host}/api/v1/settings/public", False, "probe", str(exc))
            probed += 1
            probe_event = {
                "event": "probe", "url": f"https://{result.host}",
                "fingerprint": "XHHAI" if result.compatible else result.stage,
                "registration": "开放" if result.compatible else "跳过",
                "reason": result.reason,
            }
            print(json.dumps(probe_event, ensure_ascii=False), flush=True)
            append_discovery_log(log_path, probe_event)
            if result.compatible:
                compatible.append(result)
                event = {"event": "compatible", "url": f"https://{result.host}", "stage": result.stage}
                print(json.dumps(event, ensure_ascii=False), flush=True)
                append_discovery_log(log_path, event)
            if probed % 25 == 0 or probed == len(hosts):
                progress_event = {"event": "progress", "probed": probed, "compatible": len(compatible)}
                print(json.dumps(progress_event, ensure_ascii=False), flush=True)
                append_discovery_log(log_path, progress_event)

    quarantine = accounts_root / "discovery"
    quarantine.mkdir(parents=True, exist_ok=True)

    def provision(probe: ProbeResult) -> tuple[ProbeResult, dict[str, Any], account_service.ProvisionResult]:
        profile = discovered_profile(probe.host, probe.settings or {})
        mode = account_service.resolve_email_mode(profile, probe.settings or {}, email_mode)
        mailbox = None
        if mode == "receiver":
            mailbox = email_receiver.create_mailbox(receiver_config, use_proxy=use_proxy, proxy_url=proxy_url)
            email = str(mailbox["email"])
        else:
            email = account_service.random_email(email_domain)
        snapshot = {"site_profile_snapshot": profile, "probe_url": probe.url, "promoted": False}
        result = account_service.provision_account(
            profile,
            email,
            output_dir=quarantine,
            account_kind="codex",
            route="默认",
            email_mode=mode,
            key_name=key_name,
            use_proxy=use_proxy,
            proxy_url=proxy_url,
            receiver_config=receiver_config,
            mailbox=mailbox,
            discovery=snapshot,
        )
        return probe, profile, result

    promoted = zero_balance = balance_failed = provision_failed = 0
    with ThreadPoolExecutor(max_workers=max(1, min(int(provision_concurrency), 2))) as executor:
        futures = {executor.submit(provision, item): item for item in compatible}
        for future in as_completed(futures):
            if stop_requested and stop_requested():
                for pending in futures:
                    pending.cancel()
                break
            try:
                probe, profile, result = future.result()
            except Exception as exc:
                failed_probe = futures[future]
                provision_failed += 1
                event = {"event": "result", "url": f"https://{failed_probe.host}", "status": "failed", "error": account_service.redact_log_text(exc)[:300]}
                print(json.dumps(event, ensure_ascii=False), flush=True)
                append_discovery_log(log_path, event)
                continue
            decision = "quarantined"
            final_path = result.path
            if result.balance is not None and result.balance > 0:
                profile.setdefault("discovery", {})["verified_balance"] = result.balance
                profile["discovery"]["provisioning_status"] = result.status
                promoted_id = promote_profile(store, profile)
                site_profiles.save_site_store(site_store_path, store)
                if result.path.exists():
                    if promoted_id != str(profile["id"]):
                        rebound = copy.deepcopy(profile)
                        rebound["id"] = promoted_id
                        account_service.rebind_account_site(rebound, result.path)
                    final_path = move_promoted_account(result.path, accounts_root)
                promoted += 1
                decision = "promoted" if result.status == "complete" else "promoted_needs_repair"
            elif result.balance == 0:
                zero_balance += 1
            elif result.status == "failed":
                provision_failed += 1
            else:
                balance_failed += 1
            event = {
                "event": "result", "url": f"https://{probe.host}", "status": result.status,
                "balance": result.balance, "key_status": result.key_status, "group_status": result.group_status,
                "decision": decision, "path": str(final_path),
            }
            print(json.dumps(event, ensure_ascii=False), flush=True)
            append_discovery_log(log_path, event)
    summary = {
        "generated": len(hosts), "probed": probed, "compatible": len(compatible), "promoted": promoted,
        "zero_balance": zero_balance, "balance_failed": balance_failed, "provision_failed": provision_failed,
        "stopped": bool(stop_requested and stop_requested()),
    }
    summary_event = {"event": "summary", **summary}
    print(json.dumps(summary_event, ensure_ascii=False), flush=True)
    append_discovery_log(log_path, summary_event)
    return summary
