"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from endpoint_benchmark.models import (
    BenchmarkConfig,
    EndpointPoolConfig,
    LoadConfig,
    MeasurementConfig,
    OutputConfig,
    PrefixCacheResetConfig,
    PreflightConfig,
    ValidityConfig,
    WorkloadConfig,
)
from endpoint_benchmark.runner import run_benchmark


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.print_help()
        return 2
    try:
        config = _config_from_args(args)
        result = run_benchmark(config)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_summaries(result.summaries)
    print(f"Saved results to {result.output_directory.resolve()}")
    if not result.valid and config.validity.fail_on_request_error:
        print(
            "error: benchmark is invalid because one or more concurrency "
            "levels missed the minimum success rate",
            file=sys.stderr,
        )
        return 3
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="endpoint-benchmark",
        description="Benchmark OpenAI-compatible streaming endpoint pools.",
    )
    subparsers = parser.add_subparsers(dest="command")
    run = subparsers.add_parser("run", help="run a closed-loop benchmark")
    run.add_argument("--endpoint", action="append", required=True)
    run.add_argument("--routing", default="round_robin", choices=("round_robin",))
    run.add_argument("--timeout", type=float, default=720.0)
    run.add_argument("--connection-mode", choices=("reuse", "new"), default="reuse")
    run.add_argument("--api-key-env")
    run.add_argument("--header", action="append", default=[])
    run.add_argument("--dataset", type=Path, required=True)
    run.add_argument("--model")
    run.add_argument("--temperature", type=float)
    run.add_argument("--max-output-tokens", type=int)
    run.add_argument("--max-output-tokens-cap", type=int)
    run.add_argument("--ignore-eos", action=argparse.BooleanOptionalAction)
    run.add_argument("--extra-body-json", type=Path)
    run.add_argument("--concurrency", nargs="+", type=int, required=True)
    run.add_argument("--num-prompts", type=int)
    run.add_argument("--shuffle", action="store_true")
    run.add_argument("--seed", type=int)
    run.add_argument("--warmup-prompts", type=int, default=0)
    metrics = run.add_mutually_exclusive_group()
    metrics.add_argument("--metrics-url")
    metrics.add_argument("--no-server-metrics", action="store_true")
    run.add_argument("--record-chunk-timestamps", action="store_true")
    run.add_argument("--preflight-tokenize", action="store_true")
    run.add_argument("--max-model-len", type=int)
    run.add_argument("--min-success-rate", type=float, default=1.0)
    run.add_argument(
        "--fail-on-request-error",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    run.add_argument(
        "--isolated-miss",
        action="store_true",
        help="give every logical request a unique text/media cache identity",
    )
    run.add_argument(
        "--reset-prefix-cache",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    run.add_argument("--prefix-cache-reset-timeout", type=float)
    run.add_argument("--prefix-cache-reset-interval", type=float)
    run.add_argument("--reset-external-prefix-cache", action="store_true")
    run.add_argument("--output-dir", type=Path, default=Path("benchmark_results"))
    run.add_argument("--label", default="benchmark")
    run.add_argument("--run-name")
    run.add_argument("--overwrite", action="store_true")
    return parser


def _config_from_args(args: argparse.Namespace) -> BenchmarkConfig:
    if args.isolated_miss:
        conflicts = []
        if args.reset_prefix_cache is True:
            conflicts.append("--reset-prefix-cache")
        if args.reset_external_prefix_cache:
            conflicts.append("--reset-external-prefix-cache")
        if args.prefix_cache_reset_timeout is not None:
            conflicts.append("--prefix-cache-reset-timeout")
        if args.prefix_cache_reset_interval is not None:
            conflicts.append("--prefix-cache-reset-interval")
        if conflicts:
            raise ValueError(
                "--isolated-miss cannot be combined with " + ", ".join(conflicts)
            )

    return BenchmarkConfig(
        endpoint_pool=EndpointPoolConfig(
            endpoints=tuple(args.endpoint),
            routing=args.routing,
            timeout_s=args.timeout,
            connection_mode=args.connection_mode,
            api_key_env=args.api_key_env,
            headers=tuple(_parse_header(value) for value in args.header),
        ),
        workload=WorkloadConfig(
            dataset=args.dataset,
            model=args.model,
            temperature=args.temperature,
            max_output_tokens=args.max_output_tokens,
            max_output_tokens_cap=args.max_output_tokens_cap,
            ignore_eos=args.ignore_eos,
            extra_body=_load_extra_body(args.extra_body_json),
        ),
        load=LoadConfig(
            concurrency=tuple(args.concurrency),
            num_prompts=args.num_prompts,
            shuffle=args.shuffle,
            seed=args.seed,
            warmup_prompts=args.warmup_prompts,
        ),
        measurement=MeasurementConfig(
            metrics_url=args.metrics_url,
            server_metrics_enabled=not args.no_server_metrics,
            record_chunk_timestamps=args.record_chunk_timestamps,
        ),
        preflight=PreflightConfig(
            tokenize=args.preflight_tokenize or args.max_model_len is not None,
            max_model_len=args.max_model_len,
        ),
        validity=ValidityConfig(
            min_success_rate=args.min_success_rate,
            fail_on_request_error=args.fail_on_request_error,
        ),
        isolated_miss=args.isolated_miss,
        prefix_cache_reset=PrefixCacheResetConfig(
            enabled=(
                False if args.isolated_miss else args.reset_prefix_cache is not False
            ),
            timeout_s=(
                args.prefix_cache_reset_timeout
                if args.prefix_cache_reset_timeout is not None
                else 60.0
            ),
            retry_interval_s=(
                args.prefix_cache_reset_interval
                if args.prefix_cache_reset_interval is not None
                else 0.5
            ),
            reset_external=args.reset_external_prefix_cache,
        ),
        output=OutputConfig(
            directory=args.output_dir,
            label=args.label,
            run_name=args.run_name,
            overwrite=args.overwrite,
        ),
    )


def _parse_header(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise ValueError(f"header must be KEY=VALUE: {value}")
    name, header_value = value.split("=", 1)
    if not name:
        raise ValueError("header name cannot be empty")
    return name, header_value


def _load_extra_body(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("extra body JSON must contain an object")
    return value


def _print_summaries(summaries: list[dict[str, Any]]) -> None:
    headers = (
        "conc",
        "ok/total",
        "req/s",
        "input_tok/s",
        "output_tok/s",
        "total_tok/s",
        "e2e_p95",
        "ttft_p95",
        "tpot_p95",
        "icl_p95",
        "acc_rate",
    )
    rows: list[tuple[str, ...]] = []
    for summary in summaries:
        throughput = summary["throughput"]
        server_metrics = summary.get("server_metrics") or {}
        rows.append(
            (
                str(summary["concurrency"]),
                f'{summary["successful_requests"]}/{summary["requested_requests"]}',
                _format(throughput["requests_per_second"]),
                _format(throughput["input_tokens_per_second"]),
                _format(throughput["output_tokens_per_second"]),
                _format(throughput["total_tokens_per_second"]),
                _format(summary["e2e_latency_ms"]["p95"]),
                _format(summary["ttft_ms"]["p95"]),
                _format(summary["tpot_ms"]["p95"]),
                _format(summary["inter_chunk_latency_ms"]["p95"]),
                _format(server_metrics.get("acceptance_rate"), 3),
            )
        )
    widths = [len(header) for header in headers]
    for row in rows:
        widths = [
            max(width, len(value))
            for width, value in zip(widths, row, strict=True)
        ]
    print(
        "  ".join(
            value.rjust(width)
            for value, width in zip(headers, widths, strict=True)
        )
    )
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print(
            "  ".join(
                value.rjust(width)
                for value, width in zip(row, widths, strict=True)
            )
        )


def _format(value: Any, digits: int = 2) -> str:
    return "-" if value is None else f"{float(value):.{digits}f}"


if __name__ == "__main__":
    raise SystemExit(main())
