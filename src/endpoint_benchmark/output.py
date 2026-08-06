"""Benchmark result serialization."""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from endpoint_benchmark.models import BenchmarkResult, OutputConfig


def write_result(result: BenchmarkResult) -> None:
    """Write the contracted run result files."""
    directory = result.output_directory
    directory.mkdir(parents=True, exist_ok=True)

    _write_json(
        directory / "run.json",
        {
            "run": result.run,
            "valid": result.valid,
            "preflight": result.preflight,
            "cache_resets": result.cache_resets_by_concurrency,
            "warnings": result.warnings,
        },
    )
    _write_json(directory / "summary.json", result.summaries)
    _write_summary_csv(directory / "summary.csv", result.summaries)
    _write_json(directory / "endpoint_summary.json", result.endpoint_summaries)
    _write_endpoint_summary_csv(
        directory / "endpoint_summary.csv", result.endpoint_summaries
    )
    if result.preflight is not None:
        _write_json(directory / "preflight.json", result.preflight)

    with (directory / "requests.jsonl").open("w", encoding="utf-8") as handle:
        for concurrency, requests in result.requests_by_concurrency.items():
            for request in requests:
                record = {"concurrency": concurrency, **request.to_dict()}
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    _write_json(
        directory / "server_metrics.json",
        result.server_metrics_by_concurrency,
    )
    _write_json(
        directory / "cache_resets.json",
        result.cache_resets_by_concurrency,
    )


def resolve_output_directory(config: OutputConfig) -> Path:
    """Resolve a unique timestamped run directory or an explicit run name."""
    root = config.directory
    if config.run_name is not None:
        run_name = _safe_name(config.run_name)
        directory = root / run_name
        if directory.exists() and any(directory.iterdir()) and not config.overwrite:
            raise FileExistsError(
                f"run directory is not empty: {directory}; "
                "use overwrite to replace managed result files"
            )
        return directory

    label = _safe_name(config.label)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    candidate = root / f"{label}_{timestamp}"
    suffix = 2
    while candidate.exists():
        candidate = root / f"{label}_{timestamp}_{suffix}"
        suffix += 1
    return candidate


def _safe_name(value: str) -> str:
    name = re.sub(r"[^\w.-]+", "_", value.strip(), flags=re.UNICODE).strip("._")
    if not name:
        raise ValueError(
            "label or run name must contain at least one filename-safe character"
        )
    return name


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_summary_csv(path: Path, summaries: list[dict[str, Any]]) -> None:
    fields = [
        "concurrency",
        "successful_requests",
        "requested_requests",
        "requests_per_second",
        "input_tokens_per_second",
        "output_tokens_per_second",
        "total_tokens_per_second",
        "e2e_latency_ms_mean",
        "e2e_latency_ms_p95",
        "e2e_latency_ms_p99",
        "ttft_ms_mean",
        "ttft_ms_p95",
        "ttft_ms_p99",
        "tpot_ms_mean",
        "tpot_ms_p95",
        "tpot_ms_p99",
        "inter_chunk_latency_ms_mean",
        "inter_chunk_latency_ms_p95",
        "inter_chunk_latency_ms_p99",
        "transport_retries",
        "success_rate",
        "valid",
        "acceptance_rate",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for summary in summaries:
            throughput = summary["throughput"]
            server_metrics = summary.get("server_metrics") or {}
            writer.writerow(
                {
                    "concurrency": summary["concurrency"],
                    "successful_requests": summary["successful_requests"],
                    "requested_requests": summary["requested_requests"],
                    "requests_per_second": throughput["requests_per_second"],
                    "input_tokens_per_second": throughput[
                        "input_tokens_per_second"
                    ],
                    "output_tokens_per_second": throughput[
                        "output_tokens_per_second"
                    ],
                    "total_tokens_per_second": throughput[
                        "total_tokens_per_second"
                    ],
                    "e2e_latency_ms_mean": summary["e2e_latency_ms"]["mean"],
                    "e2e_latency_ms_p95": summary["e2e_latency_ms"]["p95"],
                    "e2e_latency_ms_p99": summary["e2e_latency_ms"]["p99"],
                    "ttft_ms_mean": summary["ttft_ms"]["mean"],
                    "ttft_ms_p95": summary["ttft_ms"]["p95"],
                    "ttft_ms_p99": summary["ttft_ms"]["p99"],
                    "tpot_ms_mean": summary["tpot_ms"]["mean"],
                    "tpot_ms_p95": summary["tpot_ms"]["p95"],
                    "tpot_ms_p99": summary["tpot_ms"]["p99"],
                    "inter_chunk_latency_ms_mean": summary[
                        "inter_chunk_latency_ms"
                    ]["mean"],
                    "inter_chunk_latency_ms_p95": summary[
                        "inter_chunk_latency_ms"
                    ]["p95"],
                    "inter_chunk_latency_ms_p99": summary[
                        "inter_chunk_latency_ms"
                    ]["p99"],
                    "transport_retries": summary["transport_retries"],
                    "success_rate": summary["success_rate"],
                    "valid": summary["valid"],
                    "acceptance_rate": server_metrics.get("acceptance_rate"),
                }
            )


def _write_endpoint_summary_csv(
    path: Path, summaries: list[dict[str, Any]]
) -> None:
    fields = [
        "concurrency",
        "endpoint_index",
        "endpoint",
        "successful_requests",
        "requested_requests",
        "success_rate",
        "requests_per_second",
        "output_tokens_per_second",
        "e2e_latency_ms_p95",
        "ttft_ms_p95",
        "tpot_ms_p95",
        "inter_chunk_latency_ms_p95",
        "transport_retries",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for summary in summaries:
            writer.writerow(
                {
                    "concurrency": summary["concurrency"],
                    "endpoint_index": summary["endpoint_index"],
                    "endpoint": summary["endpoint"],
                    "successful_requests": summary["successful_requests"],
                    "requested_requests": summary["requested_requests"],
                    "success_rate": summary["success_rate"],
                    "requests_per_second": summary["throughput"][
                        "requests_per_second"
                    ],
                    "output_tokens_per_second": summary["throughput"][
                        "output_tokens_per_second"
                    ],
                    "e2e_latency_ms_p95": summary["e2e_latency_ms"]["p95"],
                    "ttft_ms_p95": summary["ttft_ms"]["p95"],
                    "tpot_ms_p95": summary["tpot_ms"]["p95"],
                    "inter_chunk_latency_ms_p95": summary[
                        "inter_chunk_latency_ms"
                    ]["p95"],
                    "transport_retries": summary["transport_retries"],
                }
            )
