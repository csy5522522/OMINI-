"""API-key members shared by the desktop pool and account synchronization."""
from __future__ import annotations

import copy
import re
import time
from urllib.parse import quote, urlsplit, urlunsplit


# Protocol names are persisted; provider names follow CLIProxyAPI v8's schema.
API_PROTOCOLS = {
    "openai": ("OpenAI Chat / 兼容接口", "openai-compatibility", "https://api.openai.com/v1"),
    "responses": ("OpenAI Responses / Codex", "codex", "https://api.openai.com/v1"),
    "anthropic": ("Anthropic Messages / Claude", "claude", "https://api.anthropic.com"),
    "gemini": ("Gemini GenerateContent", "gemini", "https://generativelanguage.googleapis.com"),
    "interactions": ("Gemini Interactions", "interactions", "https://generativelanguage.googleapis.com"),
    "vertex": ("Vertex AI（API Key）", "vertex", "https://aiplatform.googleapis.com"),
    "xai": ("xAI Responses", "xai", "https://api.x.ai/v1"),
    "meta": ("Meta Responses", "meta", "https://api.meta.ai/v1"),
}


def normalize_base_url(value: str, protocol: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("API 地址格式有误。")
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(c.isspace() or ord(c) < 32 for c in value)):
            raise ValueError()
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError()
    except ValueError:
        raise ValueError("请填写 HTTP/HTTPS API 基础地址，密钥请填在 Key 栏。") from None
    path = parsed.path.rstrip("/")
    # Accept a pasted endpoint, but store the base expected by the engine.
    suffixes = ("/chat/completions", "/responses", "/messages", "/models", "/interactions")
    for suffix in suffixes:
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    if protocol in ("openai", "responses", "xai", "meta"):
        if not path:
            path = "/v1"
    elif protocol in ("anthropic", "gemini", "interactions", "vertex"):
        path = re.sub(r"/v1(?:beta1?|alpha)?$", "", path)
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def parse_models(text: str) -> list[dict]:
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"\s*(?:=>|=)\s*", line)
        if len(parts) > 2:
            raise ValueError("每行填写一个模型 ID，或 上游ID => 对外模型ID。")
        name, alias = parts[0], parts[-1]
        result.append({"name": name, "alias": alias})
    return result


def models_text(models: list[dict]) -> str:
    return "\n".join(m["name"] if m["name"] == m["alias"] else m["name"] + " => " + m["alias"] for m in models)


def validate_api_member(value: dict, *, require_models=True) -> dict:
    if not isinstance(value, dict) or set(value) - {"label", "protocol", "base_url", "api_key", "models", "proxy", "headers", "disabled"}:
        raise ValueError("API 成员字段有误。")
    protocol = value.get("protocol")
    if not isinstance(protocol, str) or protocol not in API_PROTOCOLS:
        raise ValueError("请选择 API 协议。")
    for key, limit in (("label", 120), ("api_key", 8192), ("proxy", 2048)):
        text = value.get(key, "")
        if not isinstance(text, str) or len(text) > limit or any(ord(c) < 32 for c in text):
            raise ValueError("API 名称、Key 或代理格式有误。")
    if not value.get("label", "").strip() or not value.get("api_key", "").strip():
        raise ValueError("请填写线路名称和 API Key。")
    if any(c.isspace() or ord(c) > 126 for c in value["api_key"].strip()):
        raise ValueError("API Key 请只填写密钥文本，去掉空格和请求头前缀。")
    if type(value.get("disabled", False)) is not bool:
        raise ValueError("API 成员状态有误。")
    proxy = value.get("proxy", "").strip()
    if proxy and proxy != "direct":
        try:
            p = urlsplit(proxy)
            valid = p.scheme in ("http", "https", "socks5", "socks5h") and p.hostname and p.port and p.path in ("", "/") and not p.query and not p.fragment
        except ValueError:
            valid = False
        if not valid or any(c.isspace() for c in proxy):
            raise ValueError("线路代理请填完整代理地址、direct（直连），或留空继承账号池。")
    models = value.get("models", [])
    if not isinstance(models, list) or (require_models and not models) or len(models) > 500:
        raise ValueError("请填写至少一个模型 ID，每条线路最多 500 个模型。")
    normalized, aliases = [], set()
    for model in models:
        if not isinstance(model, dict) or set(model) - {"name", "alias"}:
            raise ValueError("模型 ID 格式有误。")
        name, alias = model.get("name", ""), model.get("alias", model.get("name", ""))
        if any(not isinstance(s, str) or not s.strip() or len(s) > 256 or any(c.isspace() or ord(c) < 32 for c in s) for s in (name, alias)):
            raise ValueError("模型 ID 与别名需为不含空格的文本。")
        if alias in aliases:
            raise ValueError("同一线路的模型别名请保持唯一。")
        aliases.add(alias)
        normalized.append({"name": name, "alias": alias})
    headers = value.get("headers", {})
    if not isinstance(headers, dict) or len(headers) > 30:
        raise ValueError("附加请求头请填写 JSON 对象。")
    for key, text in headers.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}", key)
                or not isinstance(text, str) or len(text) > 8192 or any(ord(c) < 32 or ord(c) > 255 for c in text)
                or key.lower() in {"host", "content-length", "transfer-encoding", "connection"}):
            raise ValueError("附加请求头名称或内容有误。")
    return {"label": value["label"].strip(), "protocol": protocol,
            "base_url": normalize_base_url(value.get("base_url", ""), protocol),
            "api_key": value["api_key"].strip(), "models": normalized,
            "proxy": proxy, "headers": copy.deepcopy(headers), "disabled": value.get("disabled", False)}


def validate_api_members(members: dict) -> dict:
    if not isinstance(members, dict) or len(members) > 500:
        raise ValueError("最多保存 500 条 API 线路。")
    result = {}
    for name, member in members.items():
        if not isinstance(name, str) or not re.fullmatch(r"api-[a-zA-Z0-9_-]{1,80}", name):
            raise ValueError("API 成员标识有误。")
        result[name] = validate_api_member(member)
    return result


def api_member_rows(pool: dict) -> list[dict]:
    return [{"name": name, "id": name, "label": member["label"], "api_member": True,
             "protocol": member["protocol"], "base_url": member["base_url"],
             "models": member["models"], "disabled": member.get("disabled", False),
             "status": "已保存 · 待应用"} for name, member in pool.get("api_members", {}).items()]


def api_providers(pool: dict) -> dict:
    """Emit every native API-key protocol; OAuth members remain in auth-dir."""
    providers = {spec[1]: [] for spec in API_PROTOCOLS.values()}
    for name, member in validate_api_members(pool.get("api_members", {})).items():
        if member["disabled"]:
            continue
        rule = pool.get("member_rules", {}).get(name, {})
        fields = {"name": name, "base-url": member["base_url"],
                  "priority": rule.get("priority", 0) if pool.get("strategy") == "fill-first" else 0,
                  "keys": [{"api-key": member["api_key"], "weight": rule.get("weight", 1)}],
                  "models": member["models"]}
        if member["proxy"]:
            (fields["keys"][0] if member["protocol"] == "openai" else fields)["proxy-url"] = member["proxy"]
        if member["headers"]:
            fields["headers"] = member["headers"]
        providers[API_PROTOCOLS[member["protocol"]][1]].append(fields)
    return providers


def probe_api_member(member: dict, pool_proxy="", *, generate=False) -> dict:
    """Probe the configured upstream directly, never another pool member."""
    import requests

    member = validate_api_member(member, require_models=generate)
    protocol, base, key = member["protocol"], member["base_url"], member["api_key"]
    headers = {"Accept": "application/json"}
    if protocol == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    elif protocol in ("gemini", "interactions", "vertex"):
        headers["x-goog-api-key"] = key
    else:
        headers["Authorization"] = "Bearer " + key
    # Replace case-insensitively, as HTTP request headers are case-insensitive.
    headers = requests.structures.CaseInsensitiveDict(headers)
    headers.update(member["headers"])
    model = member["models"][0]["name"] if member["models"] else ""
    prefix = "/v1/publishers/google" if protocol == "vertex" else "/v1beta" if protocol in ("gemini", "interactions") else "/v1" if protocol == "anthropic" else ""
    path, payload = prefix + "/models", None
    if generate:
        if protocol == "openai":
            path, payload = "/chat/completions", {"model": model, "messages": [{"role": "user", "content": "Reply OK"}], "max_tokens": 16, "stream": False}
        elif protocol == "anthropic":
            path, payload = "/v1/messages", {"model": model, "messages": [{"role": "user", "content": "Reply OK"}], "max_tokens": 16}
        elif protocol in ("gemini", "vertex"):
            path = prefix + "/models/" + quote(model.removeprefix("models/"), safe="") + ":generateContent"
            payload = {"contents": [{"role": "user", "parts": [{"text": "Reply OK"}]}], "generationConfig": {"maxOutputTokens": 16}}
        elif protocol == "interactions":
            path, payload = "/v1beta/interactions", {"model": model, "input": "Reply OK", "generation_config": {"max_output_tokens": 16}, "store": False}
        else:
            path, payload = prefix + "/responses", {"model": model, "input": [{"role": "user", "content": [{"type": "input_text", "text": "Reply OK"}]}], "max_output_tokens": 16, "stream": False, "store": False}
    start = time.monotonic()
    result = {"ok": False, "models": [], "status": None}
    try:
        with requests.Session() as session:
            session.trust_env = False
            proxy = member["proxy"] or pool_proxy
            if proxy and proxy != "direct":
                session.proxies.update(http=proxy, https=proxy)
            with session.request("POST" if generate else "GET", base + path, headers=headers,
                                 json=payload, timeout=(8, 35), allow_redirects=False) as response:
                result["status"] = response.status_code
                if not 200 <= response.status_code < 300:
                    message = {401: "Key 鉴权失败", 403: "访问被拒绝，请检查权限或出口线路", 404: "接口或模型不存在；模型列表接口未开放时可手填 ID 并验证模型", 429: "请求受限或额度不足"}.get(response.status_code, "上游服务返回异常状态")
                    result["message"] = f"HTTP {response.status_code} · {message}"
                else:
                    data = response.json()
                    if not isinstance(data, dict) or data.get("error") or data.get("status") in ("failed", "cancelled"):
                        raise ValueError("上游返回错误或响应结构有误")
                    if generate:
                        field = "choices" if protocol == "openai" else "content" if protocol == "anthropic" else "candidates" if protocol in ("gemini", "vertex") else "outputs" if protocol == "interactions" else "output"
                        if not isinstance(data.get(field), list) or (not data[field] and not data.get("id")):
                            raise ValueError("上游响应与所选协议不匹配")
                        result.update(ok=True, message=f"模型调用通过 · {model}")
                    else:
                        entries = data.get("models") if protocol in ("gemini", "interactions", "vertex") else data.get("data")
                        if not isinstance(entries, list):
                            raise ValueError("模型列表响应与所选协议不匹配")
                        models = sorted({str(m.get("id") or m.get("name", "")).removeprefix("models/") for m in entries if isinstance(m, dict) and (m.get("id") or m.get("name"))})
                        result.update(ok=True, models=models, message=f"鉴权与模型列表通过 · 本页 {len(models)} 个模型")
    except requests.Timeout:
        result["message"] = "连接或响应超时，请检查线路代理和地址"
    except requests.RequestException:
        result["message"] = "线路连接失败，请检查地址、证书和出口代理"
    except (ValueError, TypeError):
        result["message"] = "响应格式与所选协议不匹配，请检查 API 基础地址和协议"
    result["latency_ms"] = round((time.monotonic() - start) * 1000)
    result["message"] += f" · {result['latency_ms']} ms"
    return result
