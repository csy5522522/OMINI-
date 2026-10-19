#!/usr/bin/env python3
"""
Summarize xhhai registration workflow metrics from JSONL logs.

This script is offline-only: it reads metrics already recorded by
xhhai_create_codex_key.py and prints latency/error summaries by workflow step
and HTTP operation.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from omini_api.xhhai_concurrency_policy import (
    DEFAULT_CONCURRENCY_BAND,
    LOW_CONCURRENCY_BAND,
    PRESSURE_STATUS_CODES,
    decide_concurrency,
)


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


_configure_utf8_stdio()


DEFAULT_METRICS_LOG = Path("xhhai_register_metrics.jsonl")


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * pct)
    return ordered[index]


def fmt_ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            print(f"Skip invalid JSON line {line_no}")
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def group_key(event: dict[str, Any]) -> str:
    if event.get("event") == "http_request":
        return str(event.get("op") or event.get("path") or "unknown_http")
    return str(event.get("step") or event.get("event") or "unknown")


def summarize(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("event") == event_type:
            buckets[group_key(event)].append(event)

    rows: list[dict[str, Any]] = []
    for name, items in sorted(buckets.items()):
        latencies = [
            float(item["elapsed_ms"])
            for item in items
            if isinstance(item.get("elapsed_ms"), (int, float))
        ]
        ok_count = sum(1 for item in items if item.get("ok") is True)
        pressure = Counter(str(item.get("pressure_hint") or "unknown") for item in items)
        errors = Counter(str(item.get("error_type") or "") for item in items if item.get("ok") is not True)
        rows.append(
            {
                "name": name,
                "count": len(items),
                "ok_count": ok_count,
                "success_rate": ok_count / len(items) if items else 0.0,
                "p50": percentile(latencies, 0.50),
                "p95": percentile(latencies, 0.95),
                "p99": percentile(latencies, 0.99),
                "pressure": pressure,
                "errors": errors,
            }
        )
    return rows


def print_rows(title: str, rows: list[dict[str, Any]]) -> None:
    print(title)
    print("-" * len(title))
    if not rows:
        print("No data.")
        print()
        return
    print(f"{'name':24} {'count':>5} {'ok%':>7} {'p50ms':>10} {'p95ms':>10} {'p99ms':>10} pressure")
    for row in rows:
        pressure = ",".join(f"{key}:{value}" for key, value in sorted(row["pressure"].items()))
        if row["errors"]:
            pressure += " errors=" + ",".join(
                f"{key or 'unknown'}:{value}" for key, value in sorted(row["errors"].items())
            )
        print(
            f"{row['name'][:24]:24} "
            f"{row['count']:5d} "
            f"{row['success_rate'] * 100:6.1f}% "
            f"{fmt_ms(row['p50']):>10} "
            f"{fmt_ms(row['p95']):>10} "
            f"{fmt_ms(row['p99']):>10} "
            f"{pressure}"
        )
    print()


def is_timeout_event(event: dict[str, Any]) -> bool:
    error_type = str(event.get("error_type") or "").lower()
    error_message = str(event.get("error_message") or "").lower()
    return "timeout" in error_type or "timeout" in error_message or "timed out" in error_message


def print_concurrency_advice(
    events: list[dict[str, Any]],
    *,
    default_band: str,
    low_band: str,
) -> None:
    http_events = [event for event in events if event.get("event") == "http_request"]
    latencies = [
        float(event["elapsed_ms"])
        for event in http_events
        if isinstance(event.get("elapsed_ms"), (int, float))
    ]
    p95 = percentile(latencies, 0.95)
    timeout_count = sum(1 for event in http_events if is_timeout_event(event))
    pressure_status_count = sum(
        1 for event in http_events if event.get("status_code") in PRESSURE_STATUS_CODES
    )
    decision = decide_concurrency(
        p95_ms=p95,
        timeout_count=timeout_count,
        pressure_status_count=pressure_status_count,
        total_count=len(http_events),
        default_band=default_band,
        low_band=low_band,
    )

    print("Concurrency advice")
    print("------------------")
    print(f"Default band: {default_band}")
    print(f"Pressure band: {low_band}")
    print(f"HTTP requests: {len(http_events)}")
    print(f"p95ms: {fmt_ms(p95)}")
    print(f"429/503/504 count: {pressure_status_count}")
    print(f"Timeout count: {timeout_count}")
    print(f"Recommended band: {decision.band} ({decision.pressure})")
    if decision.should_backoff:
        print(f"Backoff: wait at least {decision.backoff_seconds}s")
    else:
        print("Backoff: none")
    print(f"Reason: {decision.reason}")
    print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="汇总 xhhai 注册/建 key 的 metrics JSONL 日志，并给出并发建议。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python xhhai_metrics_summary.py\n"
            "  python xhhai_metrics_summary.py --metrics-log xhhai_register_metrics.jsonl\n\n"
            "这个脚本只读取本地日志，不发起网络请求。"
        ),
    )
    parser.add_argument("--metrics-log", type=Path, default=DEFAULT_METRICS_LOG, help="metrics JSONL 日志路径。默认: %(default)s。")
    parser.add_argument("--default-concurrency-band", default=DEFAULT_CONCURRENCY_BAND, help="正常并发区间。默认: %(default)s。")
    parser.add_argument("--low-concurrency-band", default=LOW_CONCURRENCY_BAND, help="高压/限流时的低并发区间。默认: %(default)s。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    events = load_events(args.metrics_log)
    print(f"Metrics file: {args.metrics_log}")
    print(f"Events: {len(events)}")
    print()
    print_concurrency_advice(
        events,
        default_band=args.default_concurrency_band,
        low_band=args.low_concurrency_band,
    )
    print_rows("Workflow steps", summarize(events, "workflow_step"))
    print_rows("HTTP requests", summarize(events, "http_request"))
    print_rows("Workflow runs", summarize(events, "workflow_run"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
