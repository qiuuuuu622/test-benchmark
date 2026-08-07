"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
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
from endpoint_benchmark.target_config import default_targets_path, load_target


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
    destination = run.add_mutually_exclusive_group(required=True)
    destination.add_argument("--endpoint", action="append")
    destination.add_argument("--target")
    run.add_argument("--targets-file", type=Path)
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
        "--reset-prefix-cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="run startup/pre/post unified cache clears (default: enabled)",
    )
    run.add_argument(
        "--prefix-cache-reset-timeout",
        type=float,
        metavar="SEC",
        help="shared clear deadline (default: Target value or 60)",
    )
    run.add_argument(
        "--prefix-cache-reset-interval",
        type=float,
        metavar="SEC",
        help="clear retry interval (default: Target value or 0.5)",
    )
    run.add_argument(
        "--reset-external-prefix-cache",
        action="store_true",
        help="also clear vLLM external prefix cache (default: false)",
    )
    run.add_argument("--output-dir", type=Path, default=Path("benchmark_results"))
    run.add_argument("--label", default="benchmark")
    run.add_argument("--run-name")
    run.add_argument("--overwrite", action="store_true")
    return parser


def _config_from_args(args: argparse.Namespace) -> BenchmarkConfig:
    target = None
    if args.target is not None:
        if args.header or args.api_key_env:
            raise ValueError(
                "--target cannot be combined with --header or --api-key-env"
            )
        target = load_target(args.targets_file or default_targets_path(), args.target)
        target = replace(
            target,
            clear_timeout_s=(
                args.prefix_cache_reset_timeout
                if args.prefix_cache_reset_timeout is not None
                else target.clear_timeout_s
            ),
            clear_retry_interval_s=(
                args.prefix_cache_reset_interval
                if args.prefix_cache_reset_interval is not None
                else target.clear_retry_interval_s
            ),
        )
        endpoints = target.inference_endpoints
        headers = target.headers
        api_key_env = None
    else:
        if args.targets_file is not None:
            raise ValueError("--targets-file requires --target")
        endpoints = tuple(args.endpoint)
        headers = tuple(_parse_header(value) for value in args.header)
        api_key_env = args.api_key_env
    return BenchmarkConfig(
        endpoint_pool=EndpointPoolConfig(
            endpoints=endpoints,
            routing=args.routing,
            timeout_s=args.timeout,
            connection_mode=args.connection_mode,
            api_key_env=api_key_env,
            headers=headers,
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
        prefix_cache_reset=PrefixCacheResetConfig(
            enabled=args.reset_prefix_cache,
            timeout_s=(
                target.clear_timeout_s
                if target is not None
                else (
                    args.prefix_cache_reset_timeout
                    if args.prefix_cache_reset_timeout is not None
                    else 60.0
                )
            ),
            retry_interval_s=(
                target.clear_retry_interval_s
                if target is not None
                else (
                    args.prefix_cache_reset_interval
                    if args.prefix_cache_reset_interval is not None
                    else 0.5
                )
            ),
            reset_external=args.reset_external_prefix_cache,
        ),
        target=target,
        output=OutputConfig(
            directory=args.output_dir,
            label=args.label,
            run_name=args.run_name,
            overwrite=args.overwrite,
        ),
    )


def _parse_header(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise ValueError("header must use KEY=VALUE")
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
