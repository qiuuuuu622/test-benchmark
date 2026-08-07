"""Benchmark orchestration."""

from __future__ import annotations

import os
import platform
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from endpoint_benchmark.aggregation import summarize_endpoints, summarize_requests
from endpoint_benchmark.cache_control import clear_caches
from endpoint_benchmark.dataset import load_dataset
from endpoint_benchmark.models import (
    BenchmarkConfig,
    BenchmarkResult,
    RequestCase,
    RequestResult,
)
from endpoint_benchmark.output import resolve_output_directory, write_result
from endpoint_benchmark.preflight import run_token_preflight
from endpoint_benchmark.routing import RoundRobinRouter
from endpoint_benchmark.server_metrics import (
    MetricsSnapshot,
    calculate_metrics_delta,
    fetch_metrics,
)
from endpoint_benchmark.target_config import (
    DirectCacheSource,
    DynamoCacheSource,
    TargetConfig,
)
from endpoint_benchmark.transport import StreamingHttpClient, now_ms
from endpoint_benchmark.version import __version__


def run_benchmark(config: BenchmarkConfig) -> BenchmarkResult:
    """Run all configured concurrency levels and write their results."""
    started_at = datetime.now(UTC).isoformat()
    target = _effective_target(config)
    output_directory = resolve_output_directory(config.output)
    cases, warnings = load_dataset(config.workload.dataset, config.load)
    preflight = None
    if config.preflight.tokenize:
        preflight = run_token_preflight(config, cases, _request_headers(config))
    startup_cache_reset = None
    if config.prefix_cache_reset.enabled:
        startup_cache_reset = _reset_prefix_cache(
            config,
            target,
            config.load.concurrency[0],
            "startup_check",
        )
    requests_by_concurrency: dict[int, list[RequestResult]] = {}
    metrics_by_concurrency: dict[int, dict[str, Any] | None] = {}
    cache_resets_by_concurrency: dict[int, list[dict[str, Any]]] = {}
    summaries: list[dict[str, Any]] = []
    endpoint_summaries: list[dict[str, Any]] = []

    for concurrency in config.load.concurrency:
        summary, results, metrics, cache_resets, run_warnings = _run_concurrency(
            config, target, concurrency, cases
        )
        summaries.append(summary)
        endpoint_summaries.extend(
            summarize_endpoints(
                concurrency=concurrency,
                endpoints=tuple(
                    _redacted_url(endpoint)
                    for endpoint in config.endpoint_pool.endpoints
                ),
                results=results,
                duration_s=summary["benchmark_duration_s"],
                min_success_rate=config.validity.min_success_rate,
            )
        )
        requests_by_concurrency[concurrency] = results
        metrics_by_concurrency[concurrency] = metrics
        cache_resets_by_concurrency[concurrency] = cache_resets
        if startup_cache_reset is not None:
            cache_resets_by_concurrency[concurrency].insert(0, startup_cache_reset)
            startup_cache_reset = None
        warnings.extend(run_warnings)

    result = BenchmarkResult(
        run=_run_metadata(config, target, cases, started_at),
        summaries=summaries,
        endpoint_summaries=endpoint_summaries,
        requests_by_concurrency=requests_by_concurrency,
        server_metrics_by_concurrency=metrics_by_concurrency,
        cache_resets_by_concurrency=cache_resets_by_concurrency,
        warnings=list(dict.fromkeys(warnings)),
        output_directory=output_directory,
        valid=all(summary["valid"] for summary in summaries),
        preflight=preflight,
    )
    write_result(result)
    return result


def _run_concurrency(
    config: BenchmarkConfig,
    target: TargetConfig,
    concurrency: int,
    cases: list[RequestCase],
) -> tuple[
    dict[str, Any],
    list[RequestResult],
    dict[str, Any] | None,
    list[dict[str, Any]],
    list[str],
]:
    warnings: list[str] = []
    if config.load.warmup_prompts:
        warmup_client = StreamingHttpClient(config.endpoint_pool.connection_mode)
        try:
            _run_warmup(config, concurrency, cases, warmup_client)
        finally:
            warmup_client.close()

    cache_resets: list[dict[str, Any]] = []
    if config.prefix_cache_reset.enabled:
        cache_resets.append(_reset_prefix_cache(config, target, concurrency, "pre"))

    metrics_url = _effective_metrics_url(config)
    metrics_url_source = _metrics_url_source(config)
    metrics_before = _fetch_metrics(metrics_url, warnings, "before")
    router = RoundRobinRouter(config.endpoint_pool.endpoints)
    client = StreamingHttpClient(config.endpoint_pool.connection_mode)
    benchmark_start_ms = now_ms()
    results: list[RequestResult] = []
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [
                executor.submit(
                    _execute_case,
                    config,
                    client,
                    router,
                    case,
                    index,
                    benchmark_start_ms,
                )
                for index, case in enumerate(cases)
            ]
            for future in as_completed(futures):
                results.append(future.result())
    except BaseException:
        client.close()
        if config.prefix_cache_reset.enabled:
            _reset_prefix_cache(config, target, concurrency, "post_failure")
        raise
    benchmark_end_ms = now_ms()
    client.close()
    results.sort(key=lambda result: result.index)

    metrics_after = _fetch_metrics(metrics_url, warnings, "after")
    server_metrics = _server_metrics_result(
        metrics_url, metrics_url_source, metrics_before, metrics_after, warnings
    )
    if config.prefix_cache_reset.enabled:
        cache_resets.append(_reset_prefix_cache(config, target, concurrency, "post"))
    duration_s = (benchmark_end_ms - benchmark_start_ms) / 1000.0
    summary = summarize_requests(
        concurrency=concurrency,
        requested=len(cases),
        results=results,
        duration_s=duration_s,
        dispatch_count=router.dispatch_count,
        server_metrics=server_metrics,
        min_success_rate=config.validity.min_success_rate,
    )
    return summary, results, server_metrics, cache_resets, warnings


def _run_warmup(
    config: BenchmarkConfig,
    concurrency: int,
    cases: list[RequestCase],
    client: StreamingHttpClient,
) -> None:
    warmup_cases = [
        cases[index % len(cases)] for index in range(config.load.warmup_prompts)
    ]
    router = RoundRobinRouter(config.endpoint_pool.endpoints)
    start_ms = now_ms()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(
                _execute_case,
                config,
                client,
                router,
                case,
                index,
                start_ms,
            )
            for index, case in enumerate(warmup_cases)
        ]
        for future in as_completed(futures):
            result = future.result()
            if not result.success:
                raise RuntimeError(f"warmup request failed: {result.error}")


def _execute_case(
    config: BenchmarkConfig,
    client: StreamingHttpClient,
    router: RoundRobinRouter,
    case: RequestCase,
    index: int,
    benchmark_start_ms: float,
) -> RequestResult:
    endpoint_index, endpoint = router.next()
    payload = _build_payload(config, case)
    transport = client.post(
        endpoint=endpoint,
        payload=payload,
        headers=_request_headers(config),
        timeout_s=config.endpoint_pool.timeout_s,
        record_chunk_timestamps=config.measurement.record_chunk_timestamps,
    )
    usage = transport.usage or {}
    input_tokens = _optional_int(usage.get("prompt_tokens"))
    output_tokens = _optional_int(usage.get("completion_tokens"))
    prompt_token_details = usage.get("prompt_tokens_details")
    cached_input_tokens = (
        _optional_int(prompt_token_details.get("cached_tokens"))
        if isinstance(prompt_token_details, dict)
        else None
    )
    ttft_ms = (
        transport.first_output_ms - transport.start_ms
        if transport.first_output_ms is not None
        else None
    )
    tpot_ms = None
    if (
        transport.error is None
        and transport.first_output_ms is not None
        and output_tokens is not None
        and output_tokens > 1
    ):
        tpot_ms = (transport.end_ms - transport.first_output_ms) / (
            output_tokens - 1
        )
    arrivals = None
    if config.measurement.record_chunk_timestamps:
        arrivals = [
            timestamp - transport.start_ms for timestamp in transport.chunk_arrival_ms
        ]
    inter_chunk_latencies = [
        current - previous
        for previous, current in zip(
            transport.chunk_arrival_ms,
            transport.chunk_arrival_ms[1:],
            strict=False,
        )
    ]
    return RequestResult(
        request_id=case.request_id,
        index=index,
        endpoint_index=endpoint_index,
        success=transport.error is None,
        status_code=transport.status_code,
        error=transport.error,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        e2e_latency_ms=transport.end_ms - transport.start_ms,
        ttft_ms=ttft_ms,
        tpot_ms=tpot_ms,
        started_at_offset_ms=transport.start_ms - benchmark_start_ms,
        finished_at_offset_ms=transport.end_ms - benchmark_start_ms,
        finish_reason=transport.finish_reason,
        cached_input_tokens=cached_input_tokens,
        transport_retries=transport.retry_count,
        inter_chunk_latencies_ms=inter_chunk_latencies,
        chunk_arrival_offsets_ms=arrivals,
    )


def _build_payload(config: BenchmarkConfig, case: RequestCase) -> dict[str, Any]:
    row = dict(case.request)
    payload = dict(config.workload.extra_body)
    payload["messages"] = case.messages
    payload["stream"] = True
    payload["stream_options"] = {"include_usage": True}

    row_model = row.pop("model", None)
    model = config.workload.model if config.workload.model is not None else row_model
    if model is not None:
        payload["model"] = model

    row_temperature = row.pop("temperature", None)
    temperature = (
        config.workload.temperature
        if config.workload.temperature is not None
        else row_temperature
    )
    if temperature is not None:
        payload["temperature"] = temperature

    row_max_tokens = row.pop("max_output_tokens", None)
    max_tokens = (
        config.workload.max_output_tokens
        if config.workload.max_output_tokens is not None
        else row_max_tokens
    )
    cap = config.workload.max_output_tokens_cap
    if max_tokens is not None:
        max_tokens = int(max_tokens)
        payload["max_tokens"] = min(max_tokens, cap) if cap is not None else max_tokens

    row_ignore_eos = row.pop("ignore_eos", None)
    ignore_eos = (
        config.workload.ignore_eos
        if config.workload.ignore_eos is not None
        else row_ignore_eos
    )
    if ignore_eos is not None:
        payload["ignore_eos"] = ignore_eos

    reserved = {"messages", "model", "stream", "stream_options"}
    conflicts = reserved.intersection(row)
    if conflicts:
        names = ", ".join(sorted(conflicts))
        raise ValueError(f"request row cannot override: {names}")
    payload.update(row)
    return payload


def _request_headers(config: BenchmarkConfig) -> dict[str, str]:
    headers = dict(config.endpoint_pool.headers)
    env_name = config.endpoint_pool.api_key_env
    if env_name and (api_key := os.getenv(env_name)):
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _effective_target(config: BenchmarkConfig) -> TargetConfig:
    if config.target is not None:
        return config.target
    return TargetConfig(
        inference_endpoints=config.endpoint_pool.endpoints,
        headers=tuple(_request_headers(config).items()),
        cache_sources=(DirectCacheSource(use_inference_endpoints=True),),
        clear_timeout_s=config.prefix_cache_reset.timeout_s,
        clear_retry_interval_s=config.prefix_cache_reset.retry_interval_s,
    )


def _reset_prefix_cache(
    config: BenchmarkConfig,
    target: TargetConfig,
    concurrency: int,
    phase: str,
) -> dict[str, Any]:
    return clear_caches(
        target,
        concurrency,
        phase,
        reset_external=config.prefix_cache_reset.reset_external,
    )


def _fetch_metrics(
    url: str | None,
    warnings: list[str],
    phase: str,
) -> MetricsSnapshot | None:
    if url is None:
        return None
    try:
        return fetch_metrics(url)
    except Exception as exc:  # noqa: BLE001
        warnings.append(
            f"metrics {phase} fetch failed for {_redacted_url(url)}: "
            f"{type(exc).__name__}"
        )
        return None


def _server_metrics_result(
    url: str | None,
    url_source: str,
    before: MetricsSnapshot | None,
    after: MetricsSnapshot | None,
    warnings: list[str],
) -> dict[str, Any] | None:
    if url is None:
        return None
    if before is None or after is None:
        return {
            "available": False,
            "url": _redacted_url(url),
            "url_source": url_source,
            "accepted_tokens": None,
            "draft_tokens": None,
            "acceptance_rate": None,
            "accepted_tokens_per_position": {},
        }
    result = calculate_metrics_delta(_redacted_url(url), before, after)
    result["url_source"] = url_source
    if not result["available"]:
        warnings.append("configured metrics URL lacks supported counters")
    return result


def _effective_metrics_url(config: BenchmarkConfig) -> str | None:
    measurement = config.measurement
    if not measurement.server_metrics_enabled:
        return None
    if measurement.metrics_url is not None:
        return measurement.metrics_url
    endpoint = urlsplit(config.endpoint_pool.endpoints[0])
    return urlunsplit((endpoint.scheme, endpoint.netloc, "/metrics", "", ""))


def _metrics_url_source(config: BenchmarkConfig) -> str:
    if not config.measurement.server_metrics_enabled:
        return "disabled"
    if config.measurement.metrics_url is not None:
        return "explicit"
    return "derived_from_first_endpoint"


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _run_metadata(
    config: BenchmarkConfig,
    target: TargetConfig,
    cases: list[RequestCase],
    started_at: str,
) -> dict[str, Any]:
    return {
        "label": config.output.label,
        "started_at": started_at,
        "package_version": __version__,
        "schema_version": "1.0",
        "metric_semantics_version": "1.0",
        "dataset": {
            "path": str(config.workload.dataset.resolve()),
            "request_count": len(cases),
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "configuration": _redacted_configuration(config, target),
    }


def _redacted_configuration(
    config: BenchmarkConfig, target: TargetConfig
) -> dict[str, Any]:
    return {
        "endpoints": [
            _redacted_url(endpoint) for endpoint in config.endpoint_pool.endpoints
        ],
        "routing": config.endpoint_pool.routing,
        "timeout_s": config.endpoint_pool.timeout_s,
        "connection_mode": config.endpoint_pool.connection_mode,
        "api_key_env": config.endpoint_pool.api_key_env,
        "headers": [name for name, _ in config.endpoint_pool.headers],
        "model": config.workload.model,
        "concurrency": list(config.load.concurrency),
        "num_prompts": config.load.num_prompts,
        "shuffle": config.load.shuffle,
        "seed": config.load.seed,
        "warmup_prompts": config.load.warmup_prompts,
        "metrics_url": (
            _redacted_url(config.measurement.metrics_url)
            if config.measurement.metrics_url is not None
            else None
        ),
        "effective_metrics_url": (
            _redacted_url(url)
            if (url := _effective_metrics_url(config)) is not None
            else None
        ),
        "metrics_url_source": _metrics_url_source(config),
        "server_metrics_enabled": config.measurement.server_metrics_enabled,
        "record_chunk_timestamps": config.measurement.record_chunk_timestamps,
        "preflight_tokenize": config.preflight.tokenize,
        "preflight_max_model_len": config.preflight.max_model_len,
        "min_success_rate": config.validity.min_success_rate,
        "fail_on_request_error": config.validity.fail_on_request_error,
        "prefix_cache_reset_enabled": config.prefix_cache_reset.enabled,
        "prefix_cache_reset_timeout_s": target.clear_timeout_s,
        "prefix_cache_reset_retry_interval_s": target.clear_retry_interval_s,
        "reset_external_prefix_cache": config.prefix_cache_reset.reset_external,
        "cache_clear_timeout_s": target.clear_timeout_s,
        "cache_clear_retry_interval_s": target.clear_retry_interval_s,
        "cache_sources": [_redacted_source(source) for source in target.cache_sources],
        "output_root": str(config.output.directory),
        "run_name": config.output.run_name,
    }


def _redacted_source(source: DirectCacheSource | DynamoCacheSource) -> dict[str, Any]:
    if isinstance(source, DirectCacheSource):
        return {
            "kind": "direct",
            "endpoints": (
                [_redacted_url(endpoint) for endpoint in source.endpoints]
                if source.endpoints is not None
                else None
            ),
            "use_inference_endpoints": source.use_inference_endpoints,
            "headers": (
                [name for name, _ in source.headers]
                if source.headers is not None
                else None
            ),
        }
    return {
        "kind": "dynamo",
        "namespace": source.namespace,
        "components": list(source.components),
    }


def _redacted_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or "unknown"
        port = parsed.port
    except ValueError:
        return "<invalid-url>"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
