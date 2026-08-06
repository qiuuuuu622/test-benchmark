"""Measurement-pool summary calculations."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from typing import Any

from endpoint_benchmark.models import RequestResult


def summarize_requests(
    concurrency: int,
    requested: int,
    results: list[RequestResult],
    duration_s: float,
    dispatch_count: dict[str, int],
    server_metrics: dict[str, Any] | None,
    min_success_rate: float = 1.0,
) -> dict[str, Any]:
    """Calculate one summary from the unified request measurement pool."""
    successful = [result for result in results if result.success]
    failed = [result for result in results if not result.success]
    input_tokens = [
        result.input_tokens
        for result in successful
        if result.input_tokens is not None
    ]
    output_tokens = [
        result.output_tokens
        for result in successful
        if result.output_tokens is not None
    ]
    token_counts_complete = (
        len(input_tokens) == len(successful) == len(output_tokens)
    )
    input_total = sum(input_tokens) if token_counts_complete else None
    output_total = sum(output_tokens) if token_counts_complete else None

    success_rate = len(successful) / len(results) if results else None
    valid = success_rate is not None and success_rate >= min_success_rate
    return {
        "concurrency": concurrency,
        "requested_requests": requested,
        "completed_requests": len(results),
        "successful_requests": len(successful),
        "failed_requests": len(failed),
        "timed_out_requests": sum(
            result.error is not None and result.error.get("type") == "timeout"
            for result in failed
        ),
        "success_rate": success_rate,
        "min_success_rate": min_success_rate,
        "valid": valid,
        "benchmark_duration_s": duration_s,
        "throughput": {
            "requests_per_second": len(successful) / duration_s if duration_s else None,
            "input_tokens_per_second": (
                input_total / duration_s
                if duration_s and input_total is not None
                else None
            ),
            "output_tokens_per_second": (
                output_total / duration_s
                if duration_s and output_total is not None
                else None
            ),
            "total_tokens_per_second": (
                (input_total + output_total) / duration_s
                if duration_s and input_total is not None and output_total is not None
                else None
            ),
            "token_counts_complete": token_counts_complete,
        },
        "input_tokens": distribution(input_tokens),
        "output_tokens": distribution(output_tokens),
        "e2e_latency_ms": distribution(
            result.e2e_latency_ms for result in successful
        ),
        "ttft_ms": distribution(
            result.ttft_ms for result in successful if result.ttft_ms is not None
        ),
        "tpot_ms": distribution(
            result.tpot_ms for result in successful if result.tpot_ms is not None
        ),
        "inter_chunk_latency_ms": distribution(
            latency
            for result in successful
            for latency in result.inter_chunk_latencies_ms
        ),
        "transport_retries": sum(result.transport_retries for result in results),
        "routing": {
            "policy": "round_robin",
            "dispatch_count": dispatch_count,
        },
        "errors": _error_counts(failed),
        "server_metrics": server_metrics,
    }


def summarize_endpoints(
    concurrency: int,
    endpoints: tuple[str, ...],
    results: list[RequestResult],
    duration_s: float,
    min_success_rate: float,
) -> list[dict[str, Any]]:
    """Return diagnostics per endpoint without changing pool-level semantics."""
    summaries: list[dict[str, Any]] = []
    for endpoint_index, endpoint in enumerate(endpoints):
        endpoint_results = [
            result for result in results if result.endpoint_index == endpoint_index
        ]
        summary = summarize_requests(
            concurrency=concurrency,
            requested=len(endpoint_results),
            results=endpoint_results,
            duration_s=duration_s,
            dispatch_count={f"endpoint-{endpoint_index}": len(endpoint_results)},
            server_metrics=None,
            min_success_rate=min_success_rate,
        )
        summary["endpoint_index"] = endpoint_index
        summary["endpoint"] = endpoint
        summaries.append(summary)
    return summaries


def distribution(values: Iterable[float | int]) -> dict[str, float | int | None]:
    """Return the standard distribution contract."""
    samples = sorted(float(value) for value in values)
    if not samples:
        return {
            "samples": 0,
            "mean": None,
            "min": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "max": None,
        }
    return {
        "samples": len(samples),
        "mean": statistics.fmean(samples),
        "min": samples[0],
        "p50": percentile(samples, 50),
        "p90": percentile(samples, 90),
        "p95": percentile(samples, 95),
        "p99": percentile(samples, 99),
        "max": samples[-1],
    }


def percentile(sorted_values: list[float], p: float) -> float:
    """Calculate a linearly interpolated percentile."""
    index = (len(sorted_values) - 1) * p / 100.0
    low = math.floor(index)
    high = math.ceil(index)
    if low == high:
        return sorted_values[low]
    return sorted_values[low] + (
        sorted_values[high] - sorted_values[low]
    ) * (index - low)


def _error_counts(results: list[RequestResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        error_type = result.error.get("type", "unknown") if result.error else "unknown"
        counts[error_type] = counts.get(error_type, 0) + 1
    return counts
