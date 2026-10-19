#!/usr/bin/env python3
"""Shared concurrency pressure rules for xhhai registration tooling."""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_CONCURRENCY_BAND = "5-20"
LOW_CONCURRENCY_BAND = "1-5"
PRESSURE_STATUS_CODES = {429, 503, 504}


@dataclass(frozen=True)
class ConcurrencyDecision:
    band: str
    minimum: int
    maximum: int
    pressure: str
    should_backoff: bool
    backoff_seconds: int
    reason: str


def parse_concurrency_band(value: str) -> tuple[int, int]:
    parts = value.strip().split("-", 1)
    if len(parts) != 2:
        raise ValueError(f"Concurrency band must look like min-max: {value}")
    minimum = int(parts[0])
    maximum = int(parts[1])
    if minimum < 1 or maximum < minimum:
        raise ValueError(f"Invalid concurrency band: {value}")
    return minimum, maximum


def _status_pressure_is_large(count: int, total: int) -> bool:
    if count <= 0:
        return False
    if count >= 3:
        return True
    return total > 0 and (count / total) >= 0.10


def decide_concurrency(
    *,
    p95_ms: float | None,
    timeout_count: int,
    pressure_status_count: int,
    total_count: int,
    default_band: str = DEFAULT_CONCURRENCY_BAND,
    low_band: str = LOW_CONCURRENCY_BAND,
) -> ConcurrencyDecision:
    if _status_pressure_is_large(pressure_status_count, total_count):
        band = low_band
        minimum, maximum = parse_concurrency_band(band)
        return ConcurrencyDecision(
            band=band,
            minimum=minimum,
            maximum=maximum,
            pressure="high",
            should_backoff=True,
            backoff_seconds=300,
            reason="429/503/504 responses are frequent; lower registration frequency and wait before retrying.",
        )

    if timeout_count >= 3:
        band = low_band
        minimum, maximum = parse_concurrency_band(band)
        return ConcurrencyDecision(
            band=band,
            minimum=minimum,
            maximum=maximum,
            pressure="high",
            should_backoff=True,
            backoff_seconds=180,
            reason="timeouts are increasing; use the low concurrency band and back off.",
        )

    if p95_ms is not None and p95_ms > 5000:
        band = low_band
        minimum, maximum = parse_concurrency_band(band)
        return ConcurrencyDecision(
            band=band,
            minimum=minimum,
            maximum=maximum,
            pressure="high",
            should_backoff=True,
            backoff_seconds=120,
            reason="p95 latency is above 5s; pressure is clear.",
        )

    band = default_band
    minimum, maximum = parse_concurrency_band(band)
    if p95_ms is not None and p95_ms >= 2000:
        return ConcurrencyDecision(
            band=band,
            minimum=minimum,
            maximum=maximum,
            pressure="elevated",
            should_backoff=False,
            backoff_seconds=0,
            reason="p95 latency is 2s-5s; keep the default band but watch the next samples.",
        )

    return ConcurrencyDecision(
        band=band,
        minimum=minimum,
        maximum=maximum,
        pressure="normal",
        should_backoff=False,
        backoff_seconds=0,
        reason="p95 latency is below 2s and pressure errors are not frequent.",
    )
