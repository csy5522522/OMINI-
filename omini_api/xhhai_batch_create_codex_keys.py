#!/usr/bin/env python3
"""
Batch-register xhhai accounts and create codex keys with adaptive concurrency.

Concurrency policy:
- normal/elevated pressure: run in the default band, usually 5-20
- high pressure: move into the low band, usually 1-5, and back off
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from omini_api.xhhai_codexpp_sync import (
    DEFAULT_CODEXPP_SETTINGS_PATH,
    DEFAULT_PROFILE_NAME as DEFAULT_CODEXPP_PROFILE_NAME,
    DEFAULT_WEBUI_CUSTOM_PROVIDERS_PATH,
    provider_base_url_from_api_base,
    sync_codexpp_if_present,
)
from omini_api.xhhai_concurrency_policy import (
    DEFAULT_CONCURRENCY_BAND,
    LOW_CONCURRENCY_BAND,
    PRESSURE_STATUS_CODES,
    ConcurrencyDecision,
    decide_concurrency,
    parse_concurrency_band,
)
from omini_api.xhhai_file_utils import write_json_atomic
from omini_api.xhhai_create_codex_key import (
    DEFAULT_GROUP_NAME,
    DEFAULT_KEY_NAME,
    DEFAULT_METRICS_LOG,
    classify_workflow_error,
    create_key,
    extract_api_key,
    get_available_groups,
    pick_group,
    pressure_hint,
    register_account,
    workflow_step,
)
from omini_api.xhhai_register import (
    ALLOWED_EMAIL_DOMAINS,
    DEFAULT_API_BASE,
    DEFAULT_EMAIL_DOMAIN,
    ApiError,
    get_public_settings,
    make_email,
    make_password,
    metric_timestamp,
    validate_email_rule,
    validate_password_rule,
    validate_settings,
)


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


_configure_utf8_stdio()


DEFAULT_OUTPUT_PATTERN = "xhhai_codex_account_{email_local}.json"
DEFAULT_BATCH_MODE = "adaptive"
DEFAULT_PACED_BATCH_SIZE = 2
DEFAULT_PACED_INTERVAL_SECONDS = 2
DEFAULT_PACED_FAILURE_STEP_SECONDS = 1
DEFAULT_PACED_RECOVERY_SECONDS = 10


class ThreadSafeMetricSink:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, event: dict[str, Any]) -> None:
        row = dict(event)
        with self._lock:
            self._events.append(row)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def mark(self) -> int:
        with self._lock:
            return len(self._events)

    def events_since(self, mark: int) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events[mark:])


@dataclass(frozen=True)
class AccountJob:
    index: int
    email: str
    password: str | None
    output: Path


@dataclass(frozen=True)
class JobResult:
    job: AccountJob
    ok: bool
    elapsed_ms: float
    output: Path | None = None
    error_type: str | None = None
    error_message: str | None = None


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * pct)
    return ordered[index]


def is_timeout_event(event: dict[str, Any]) -> bool:
    error_type = str(event.get("error_type") or "").lower()
    error_message = str(event.get("error_message") or "").lower()
    return "timeout" in error_type or "timeout" in error_message or "timed out" in error_message


def decide_from_events(
    events: list[dict[str, Any]],
    *,
    default_band: str,
    low_band: str,
) -> ConcurrencyDecision:
    http_events = [event for event in events if event.get("event") == "http_request"]
    latencies = [
        float(event["elapsed_ms"])
        for event in http_events
        if isinstance(event.get("elapsed_ms"), (int, float))
    ]
    return decide_concurrency(
        p95_ms=percentile(latencies, 0.95),
        timeout_count=sum(1 for event in http_events if is_timeout_event(event)),
        pressure_status_count=sum(
            1 for event in http_events if event.get("status_code") in PRESSURE_STATUS_CODES
        ),
        total_count=len(http_events),
        default_band=default_band,
        low_band=low_band,
    )


def next_concurrency(
    current: int,
    decision: ConcurrencyDecision,
    *,
    default_band: str,
    low_band: str,
    ramp_step: int,
) -> int:
    default_min, default_max = parse_concurrency_band(default_band)
    low_min, low_max = parse_concurrency_band(low_band)
    step = max(ramp_step, 1)

    if decision.band == low_band:
        capped = min(current, low_max)
        if decision.pressure == "high":
            return max(low_min, capped - step)
        return max(low_min, min(capped, low_max))

    if current < default_min:
        return default_min
    if decision.pressure == "normal":
        return min(current + step, default_max)
    return max(default_min, min(current, default_max))


def sanitize_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    slug = slug.strip("._-")
    return slug or secrets.token_hex(4)


def render_output_path(pattern: str, output_dir: Path, index: int, email: str) -> Path:
    email_local = email.split("@", 1)[0]
    values = {
        "index": index,
        "email": email,
        "email_slug": sanitize_slug(email),
        "email_local": sanitize_slug(email_local),
    }
    return output_dir / pattern.format(**values)


def read_email_rows(path: Path) -> list[tuple[str, str | None]]:
    rows: list[tuple[str, str | None]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(line for line in fh if line.strip() and not line.lstrip().startswith("#"))
        for row in reader:
            if not row:
                continue
            email = row[0].strip()
            password = row[1].strip() if len(row) > 1 and row[1].strip() else None
            if email:
                rows.append((email, password))
    return rows


def build_jobs(args: argparse.Namespace) -> list[AccountJob]:
    email_rows: list[tuple[str, str | None]] = []
    for email in args.email:
        email_rows.append((email, None))
    if args.emails_file:
        email_rows.extend(read_email_rows(args.emails_file))
    for _ in range(args.count):
        email_rows.append((make_email(args.email_domain), None))

    jobs: list[AccountJob] = []
    seen: set[str] = set()
    for email, row_password in email_rows:
        normalized = validate_email_rule(email)
        if not normalized:
            continue
        dedupe_key = normalized.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        index = len(jobs) + 1
        output = render_output_path(args.output_pattern, args.output_dir, index, normalized)
        password = row_password or args.password
        if password:
            password = validate_password_rule(password)
        jobs.append(AccountJob(index=index, email=normalized, password=password, output=output))
    return jobs


def create_one_account(
    job: AccountJob,
    *,
    api_base: str,
    key_name: str,
    group_name: str,
    overwrite: bool,
    use_system_proxy: bool,
    metric_sink: ThreadSafeMetricSink,
    metric_context: dict[str, Any],
) -> JobResult:
    started = time.perf_counter()
    workflow_ok = False
    workflow_error_type: str | None = None
    workflow_error_message: str | None = None
    password = job.password or make_password()
    context = dict(metric_context)
    context.update(
        {
            "job_index": job.index,
            "email": job.email,
            "output": str(job.output),
        }
    )

    try:
        if job.output.exists() and not overwrite:
            raise ApiError(f"Output already exists: {job.output}. Pass --overwrite to replace it.")

        with workflow_step("register_account", metric_sink, context):
            registration = register_account(
                api_base,
                job.email,
                password,
                metric_sink=metric_sink,
                metric_context=context,
                use_system_proxy=use_system_proxy,
            )
        token = registration["access_token"]

        with workflow_step("get_available_groups", metric_sink, context):
            groups = get_available_groups(
                api_base,
                token,
                metric_sink=metric_sink,
                metric_context=context,
                use_system_proxy=use_system_proxy,
            )

        with workflow_step("pick_group", metric_sink, context):
            group = pick_group(groups, group_name)
            group_id = group.get("id")
            if group_id is None:
                raise ApiError(f"Matched group has no id: {group}")

        with workflow_step("create_key", metric_sink, context):
            key = create_key(
                api_base,
                token,
                key_name,
                group_id,
                metric_sink=metric_sink,
                metric_context=context,
                use_system_proxy=use_system_proxy,
            )
            api_key = extract_api_key(key)

        with workflow_step("save_output", metric_sink, context):
            output = {
                "site": "https://api.xhhai.top",
                "email": job.email,
                "password": password,
                "api_key": api_key,
                "balance_initial": 5.0,
                "concurrency_policy": {
                    "band": metric_context["concurrency_band"],
                    "minimum": metric_context["concurrency_min"],
                    "maximum": metric_context["concurrency_max"],
                    "current": metric_context["concurrency_current"],
                },
                "batch": {
                    "batch_id": metric_context["batch_id"],
                    "job_index": job.index,
                    "wave": metric_context["wave"],
                },
                "group": group,
                "key": key,
                "registration": registration,
            }
            job.output.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(job.output, output)

        workflow_ok = True
        return JobResult(
            job=job,
            ok=True,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            output=job.output,
        )
    except Exception as exc:
        workflow_error_type = classify_workflow_error(exc)
        workflow_error_message = str(exc)
        return JobResult(
            job=job,
            ok=False,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            error_type=workflow_error_type,
            error_message=workflow_error_message,
        )
    finally:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        event: dict[str, Any] = {
            "event": "workflow_run",
            "ts": metric_timestamp(),
            "ok": workflow_ok,
            "elapsed_ms": elapsed_ms,
            "error_type": workflow_error_type,
            "error_message": workflow_error_message,
            "pressure_hint": pressure_hint(None, elapsed_ms, workflow_error_type),
        }
        event.update(context)
        metric_sink.record(event)


def print_result(result: JobResult) -> None:
    if result.ok:
        print(f"[OK] {result.job.email} -> {result.output} ({result.elapsed_ms:.2f}ms)", flush=True)
        return
    print(
        f"[FAIL] {result.job.email}: {result.error_type}: {result.error_message} "
        f"({result.elapsed_ms:.2f}ms)",
        flush=True,
    )


def load_api_key_from_output(path: Path) -> str | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    value = data.get("api_key")
    return value if isinstance(value, str) and value else None


def sleep_with_message(seconds: int) -> None:
    if seconds <= 0:
        return
    print(f"Backoff: sleeping {seconds}s before the next wave.", flush=True)
    time.sleep(seconds)


def sleep_between_paced_waves(seconds: int) -> None:
    if seconds <= 0:
        return
    print(f"Paced wait: sleeping {seconds}s before the next wave.", flush=True)
    time.sleep(seconds)


def paced_wait_seconds(
    base_interval: int,
    failure_streak: int,
    failure_step: int,
    max_backoff: int,
) -> int:
    wait = base_interval + max(failure_streak - 1, 0) * max(failure_step, 1)
    return min(wait, max(max_backoff, 1))


def run_batch(args: argparse.Namespace) -> int:
    # Compatibility API: this historical entry point now follows the v2
    # registration-only contract.  The former key-creation implementation is
    # intentionally left below for source compatibility, but is not reached.
    from omini_api.omini_account_service import register_batch
    from omini_api.omini_sites import load_site_store, resolve_proxy, site_by_id

    from omini_api.omini_api_toolbox import app_dir
    base = app_dir()
    store = load_site_store(base / "omini_sites.json")
    profile = site_by_id(store, str(getattr(args, "site_id", "xhhai")))
    mode = str(getattr(args, "proxy_mode", "") or ("自动" if getattr(args, "use_system_proxy", False) else "直连"))
    decision = resolve_proxy(mode, str(getattr(args, "proxy_host", "127.0.0.1")), int(getattr(args, "proxy_port", 7897)))
    if decision.note:
        print(f"[NETWORK] {decision.note}", flush=True)
    print("[DEPRECATED] 旧批量入口已切换为只注册账号；分组和 Key 参数本次忽略。", flush=True)
    minimum, _maximum = parse_concurrency_band(str(getattr(args, "default_band", DEFAULT_CONCURRENCY_BAND)))
    batch_mode = str(getattr(args, "batch_mode", DEFAULT_BATCH_MODE))
    concurrency = getattr(args, "initial_concurrency", None) or (
        int(getattr(args, "paced_batch_size", DEFAULT_PACED_BATCH_SIZE)) if batch_mode == "paced" else minimum
    )
    ok_count, fail_count = register_batch(
        profile,
        count=int(getattr(args, "count", 0)),
        email_domain=str(getattr(args, "email_domain", DEFAULT_EMAIL_DOMAIN)),
        output_dir=Path(getattr(args, "output_dir", Path("."))),
        account_kind="codex",
        route="",
        use_proxy=decision.use_proxy,
        proxy_url=decision.proxy_url,
        overwrite=bool(getattr(args, "overwrite", False)),
        concurrency=concurrency,
        emails_file=Path(args.emails_file) if getattr(args, "emails_file", None) else None,
        email_rows=[(email, getattr(args, "password", None)) for email in getattr(args, "email", [])],
        password=getattr(args, "password", None),
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    return 0 if fail_count == 0 else 1

    api_base = args.api_base.rstrip("/")
    if args.count < 0:
        raise ApiError("--count must be 0 or greater.")
    args.paced_batch_size = max(int(getattr(args, "paced_batch_size", DEFAULT_PACED_BATCH_SIZE)), 1)
    args.paced_interval_seconds = max(int(getattr(args, "paced_interval_seconds", DEFAULT_PACED_INTERVAL_SECONDS)), 1)
    args.paced_failure_step_seconds = max(int(getattr(args, "paced_failure_step_seconds", DEFAULT_PACED_FAILURE_STEP_SECONDS)), 1)
    args.paced_recovery_seconds = max(int(getattr(args, "paced_recovery_seconds", DEFAULT_PACED_RECOVERY_SECONDS)), 1)
    default_min, default_max = parse_concurrency_band(args.default_band)
    low_min, low_max = parse_concurrency_band(args.low_band)
    active_band = args.default_band
    current_concurrency = args.initial_concurrency or default_min
    current_concurrency = max(low_min, min(current_concurrency, default_max))

    jobs = build_jobs(args)
    if not jobs:
        raise ApiError("No emails provided. Use --email, --emails-file, or --count.")

    if args.dry_run:
        print(f"Dry run: {len(jobs)} jobs.")
        if getattr(args, "batch_mode", DEFAULT_BATCH_MODE) == "paced":
            print(
                f"Paced mode: batch_size={args.paced_batch_size}; "
                f"interval={args.paced_interval_seconds}s; "
                f"failure_step={args.paced_failure_step_seconds}s; "
                f"recovery={args.paced_recovery_seconds}s"
            )
        else:
            print(f"Default band: {args.default_band}; low band: {args.low_band}")
            print(f"Initial concurrency: {current_concurrency}")
        for job in jobs:
            print(f"{job.index}: {job.email} -> {job.output}")
        return 0

    metric_path = None if args.no_metrics else args.metrics_log
    metric_sink = ThreadSafeMetricSink(metric_path)
    batch_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
    batch_started = time.perf_counter()

    settings_context = {
        "batch_id": batch_id,
        "wave": 0,
        "concurrency_band": args.default_band,
        "concurrency_min": default_min,
        "concurrency_max": default_max,
        "concurrency_current": current_concurrency,
    }
    with workflow_step("settings_check", metric_sink, settings_context):
        settings = get_public_settings(
            api_base,
            metric_sink=metric_sink,
            metric_context=settings_context,
            use_system_proxy=args.use_system_proxy,
        )
        validate_settings(settings, invitation_code=None)

    remaining = list(jobs)
    results: list[JobResult] = []
    wave = 0
    batch_mode = getattr(args, "batch_mode", DEFAULT_BATCH_MODE)
    if batch_mode == "paced":
        current_concurrency = args.paced_batch_size
        print(
            f"Batch {batch_id}: {len(jobs)} jobs; paced mode, "
            f"batch_size={current_concurrency}, interval={args.paced_interval_seconds}s",
            flush=True,
        )
    else:
        print(
            f"Batch {batch_id}: {len(jobs)} jobs; default={args.default_band}, "
            f"low={args.low_band}, initial={current_concurrency}",
            flush=True,
        )

    failure_streak = 0
    last_failure_at: float | None = None
    while remaining:
        wave += 1
        concurrency_min, concurrency_max = parse_concurrency_band(active_band)
        wave_jobs = remaining[:current_concurrency]
        remaining = remaining[current_concurrency:]
        wave_context = {
            "batch_id": batch_id,
            "wave": wave,
            "concurrency_band": "paced" if batch_mode == "paced" else active_band,
            "concurrency_min": current_concurrency if batch_mode == "paced" else concurrency_min,
            "concurrency_max": current_concurrency if batch_mode == "paced" else concurrency_max,
            "concurrency_current": current_concurrency,
        }

        print(
            f"Wave {wave}: running {len(wave_jobs)} jobs at concurrency={current_concurrency}",
            flush=True,
        )
        mark = metric_sink.mark()
        with ThreadPoolExecutor(max_workers=current_concurrency) as executor:
            futures = [
                executor.submit(
                    create_one_account,
                    job,
                    api_base=api_base,
                    key_name=args.key_name,
                    group_name=args.group_name,
                    overwrite=args.overwrite,
                    use_system_proxy=args.use_system_proxy,
                    metric_sink=metric_sink,
                    metric_context=wave_context,
                )
                for job in wave_jobs
            ]
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                print_result(result)

        wave_results = results[-len(wave_jobs) :]
        wave_events = metric_sink.events_since(mark)
        if batch_mode == "paced":
            wave_ok = all(result.ok for result in wave_results)
            previous = current_concurrency
            next_wait = 0
            if wave_ok:
                if last_failure_at is not None and (time.perf_counter() - last_failure_at) >= args.paced_recovery_seconds:
                    failure_streak = 0
                next_wait = args.paced_interval_seconds
            else:
                failure_streak += 1
                last_failure_at = time.perf_counter()
                next_wait = paced_wait_seconds(
                    args.paced_interval_seconds,
                    failure_streak,
                    args.paced_failure_step_seconds,
                    args.max_backoff_seconds,
                )
            metric_sink.record(
                {
                    "event": "batch_wave",
                    "ts": metric_timestamp(),
                    "batch_id": batch_id,
                    "wave": wave,
                    "jobs": len(wave_jobs),
                    "ok": wave_ok,
                    "previous_concurrency": previous,
                    "next_concurrency": current_concurrency,
                    "decision_band": "paced",
                    "decision_pressure": "failure" if not wave_ok else "normal",
                    "decision_backoff_seconds": next_wait,
                    "decision_reason": (
                        f"paced mode failure streak={failure_streak}; wait {next_wait}s"
                        if not wave_ok
                        else f"paced mode steady interval {next_wait}s"
                    ),
                }
            )
            print(
                f"Wave {wave} decision: paced; next batch_size={current_concurrency}; "
                f"wait={next_wait}s; failure_streak={failure_streak}",
                flush=True,
            )
            if remaining:
                sleep_between_paced_waves(next_wait)
            continue

        decision = decide_from_events(
            wave_events,
            default_band=args.default_band,
            low_band=args.low_band,
        )
        previous = current_concurrency
        active_band = decision.band
        current_concurrency = next_concurrency(
            current_concurrency,
            decision,
            default_band=args.default_band,
            low_band=args.low_band,
            ramp_step=args.ramp_step,
        )
        metric_sink.record(
            {
                "event": "batch_wave",
                "ts": metric_timestamp(),
                "batch_id": batch_id,
                "wave": wave,
                "jobs": len(wave_jobs),
                "ok": all(result.ok for result in wave_results),
                "previous_concurrency": previous,
                "next_concurrency": current_concurrency,
                "decision_band": decision.band,
                "decision_pressure": decision.pressure,
                "decision_backoff_seconds": decision.backoff_seconds,
                "decision_reason": decision.reason,
            }
        )
        print(
            f"Wave {wave} decision: {decision.band} / {decision.pressure}; "
            f"next concurrency={current_concurrency}. {decision.reason}",
            flush=True,
        )
        if remaining and decision.should_backoff:
            sleep_with_message(min(decision.backoff_seconds, args.max_backoff_seconds))

    ok_count = sum(1 for result in results if result.ok)
    fail_count = len(results) - ok_count
    sync_result = None
    if getattr(args, "sync_codexpp", True):
        first_success = next((result for result in results if result.ok and result.output), None)
        if first_success and first_success.output:
            api_key = load_api_key_from_output(first_success.output)
            if api_key:
                sync_result = sync_codexpp_if_present(
                    api_key=api_key,
                    base_url=provider_base_url_from_api_base(api_base),
                    settings_path=Path(getattr(args, "codexpp_settings_path", DEFAULT_CODEXPP_SETTINGS_PATH)).expanduser(),
                    webui_path=Path(getattr(args, "codexpp_webui_path", DEFAULT_WEBUI_CUSTOM_PROVIDERS_PATH)).expanduser(),
                    profile_name=str(getattr(args, "codexpp_profile_name", DEFAULT_CODEXPP_PROFILE_NAME)),
                )
            else:
                print("Codex++ sync skipped: first successful output has no api_key.", flush=True)
        else:
            print("Codex++ sync skipped: no successful account output was produced.", flush=True)
    metric_sink.record(
        {
            "event": "batch_run",
            "ts": metric_timestamp(),
            "batch_id": batch_id,
            "ok": fail_count == 0,
            "jobs": len(results),
            "ok_count": ok_count,
            "fail_count": fail_count,
            "elapsed_ms": round((time.perf_counter() - batch_started) * 1000, 2),
        }
    )
    if sync_result is not None:
        print(f"Codex++ sync: {sync_result.summary()}", flush=True)
    print(f"Batch complete: ok={ok_count}, failed={fail_count}", flush=True)
    return 0 if fail_count == 0 else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="兼容入口：批量注册账号并保存登录令牌，不创建 API Key。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python xhhai_batch_create_codex_keys.py --count 20 --output-dir accounts\n"
            "  python xhhai_batch_create_codex_keys.py --count 10 --email-domain example.com --site-id xhhai\n"
            "  python xhhai_batch_create_codex_keys.py --emails-file emails.csv --overwrite\n"
            "  python xhhai_batch_create_codex_keys.py --count 20 --use-system-proxy\n\n"
            "并发规则:\n"
            "  默认区间是 5-20；压力高、超时或 429/502/503/504 时降到 1-5。\n"
            "  每一波结束后根据请求耗时、失败、限流自动调整下一波并发。\n"
            "  --batch-mode paced 可改用节拍模式：每轮注册固定数量，隔固定秒数继续；失败后等待逐步增加。\n"
            "账号规则:\n"
            "  自动账号为 8-12 位数字 + 自定义邮箱后缀；密码为 6-10 位。"
        ),
    )
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="自动生成并注册多少个账号。默认: %(default)s。",
    )
    parser.add_argument("--email", action="append", default=[], help="手动指定邮箱；可重复传多次。")
    parser.add_argument(
        "--email-domain",
        default=DEFAULT_EMAIL_DOMAIN,
        help="自动生成邮箱时使用的域名。默认: qq.com。",
    )
    parser.add_argument(
        "--emails-file",
        type=Path,
        help="邮箱列表文件；每行 email 或 email,password，支持 CSV/普通文本。",
    )
    parser.add_argument("--password", help="统一指定密码，长度必须是 6-10 位；不传则每个账号自动生成。")
    parser.add_argument("--key-name", default=DEFAULT_KEY_NAME, help="兼容旧命令；注册阶段忽略。")
    parser.add_argument("--group-name", default=DEFAULT_GROUP_NAME, help="兼容旧命令；注册阶段忽略。")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="接口基础地址。默认: %(default)s。")
    parser.add_argument("--use-system-proxy", action="store_true", help="使用 Windows/环境变量里的系统代理或 VPN HTTP 代理路线。")
    parser.add_argument("--site-id", default="xhhai", help="omini_sites.json 中的网站 ID。默认: %(default)s。")
    parser.add_argument("--proxy-mode", choices=("自动", "强制代理", "直连"), default="", help="代理模式；旧 --use-system-proxy 会迁移为自动。")
    parser.add_argument("--proxy-host", default="127.0.0.1")
    parser.add_argument("--proxy-port", type=int, default=7897)
    parser.add_argument("--output-dir", type=Path, default=Path("."), help="账号 JSON 输出目录。默认: 当前目录。")
    parser.add_argument("--output-pattern", default=DEFAULT_OUTPUT_PATTERN, help="账号 JSON 文件名模板。默认: %(default)s。")
    parser.add_argument("--overwrite", action="store_true", help="如果输出 JSON 已存在，允许覆盖。")
    parser.add_argument("--metrics-log", type=Path, default=DEFAULT_METRICS_LOG, help="metrics JSONL 日志路径。默认: %(default)s。")
    parser.add_argument("--no-metrics", action="store_true", help="不写入 metrics 日志。")
    parser.add_argument("--default-band", default=DEFAULT_CONCURRENCY_BAND, help="正常并发区间。默认: %(default)s。")
    parser.add_argument("--low-band", default=LOW_CONCURRENCY_BAND, help="高压/限流时使用的低并发区间。默认: %(default)s。")
    parser.add_argument("--batch-mode", choices=("adaptive", "paced"), default=DEFAULT_BATCH_MODE, help="注册并发模式：adaptive=按压力自动调整；paced=每轮固定数量并按秒等待。默认: %(default)s。")
    parser.add_argument("--paced-batch-size", type=int, default=DEFAULT_PACED_BATCH_SIZE, help="--batch-mode paced 时每轮注册几个号。默认: %(default)s。")
    parser.add_argument("--paced-interval-seconds", type=int, default=DEFAULT_PACED_INTERVAL_SECONDS, help="--batch-mode paced 时无失败的基础等待秒数。默认: %(default)s。")
    parser.add_argument("--paced-failure-step-seconds", type=int, default=DEFAULT_PACED_FAILURE_STEP_SECONDS, help="--batch-mode paced 时每次失败后等待时间增加多少秒。默认: %(default)s。")
    parser.add_argument("--paced-recovery-seconds", type=int, default=DEFAULT_PACED_RECOVERY_SECONDS, help="--batch-mode paced 时连续多少秒没有失败后清零失败递增计数。默认: %(default)s。")
    parser.add_argument("--initial-concurrency", type=int, help="起始并发；不传则使用 --default-band 的最小值。")
    parser.add_argument("--ramp-step", type=int, default=1, help="每一波并发增减步长。默认: %(default)s。")
    parser.add_argument(
        "--max-backoff-seconds",
        type=int,
        default=300,
        help="自动退避等待时间上限，单位秒。默认: %(default)s。",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划任务，不发起网络请求。")
    parser.add_argument("--no-sync-codexpp", dest="sync_codexpp", action="store_false", default=True, help="不要同步 Codex++ 的批量注册 API key。同步时只改 key，不改 URL。")
    parser.add_argument("--codexpp-settings-path", type=Path, default=DEFAULT_CODEXPP_SETTINGS_PATH, help="Codex++ settings.json 路径；存在才会修改。默认: %(default)s。")
    parser.add_argument("--codexpp-webui-path", type=Path, default=DEFAULT_WEBUI_CUSTOM_PROVIDERS_PATH, help="Codex++ webui-custom-providers.json 路径；存在才会修改。默认: %(default)s。")
    parser.add_argument("--codexpp-profile-name", default=DEFAULT_CODEXPP_PROFILE_NAME, help="要同步的 Codex++ relay profile 名称。默认: %(default)s。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        from omini_api.omini_account_service import register_batch
        from omini_api.omini_sites import load_site_store, resolve_proxy, site_by_id

        from omini_api.omini_api_toolbox import app_dir
        base = app_dir()
        store = load_site_store(base / "omini_sites.json")
        profile = site_by_id(store, args.site_id)
        mode = args.proxy_mode or ("自动" if args.use_system_proxy else "直连")
        decision = resolve_proxy(mode, args.proxy_host, args.proxy_port)
        if decision.note:
            print(f"[NETWORK] {decision.note}", flush=True)
        print("[DEPRECATED] 旧批量脚本已切换为只注册账号；分组和 Key 参数本次忽略。", flush=True)
        minimum, _maximum = parse_concurrency_band(args.default_band)
        concurrency = args.initial_concurrency or (args.paced_batch_size if args.batch_mode == "paced" else minimum)
        ok_count, fail_count = register_batch(
            profile,
            count=args.count,
            email_domain=args.email_domain,
            output_dir=args.output_dir,
            account_kind="codex",
            route="",
            use_proxy=decision.use_proxy,
            proxy_url=decision.proxy_url,
            overwrite=args.overwrite,
            concurrency=concurrency,
            emails_file=args.emails_file,
            email_rows=[(email, args.password) for email in args.email],
            password=args.password,
            dry_run=args.dry_run,
        )
        return 0 if fail_count == 0 else 1
    except ApiError as exc:
        print(f"Batch failed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Invalid arguments: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCanceled.", file=sys.stderr)
        return 130
    except Exception as exc:
        from omini_api.omini_account_service import redact_log_text

        print(f"Batch failed: {redact_log_text(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
