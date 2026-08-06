"""Server-side dataset token preflight."""

from __future__ import annotations

import json
import urllib.request
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from endpoint_benchmark.aggregation import distribution
from endpoint_benchmark.models import BenchmarkConfig, RequestCase


def run_token_preflight(
    config: BenchmarkConfig,
    cases: list[RequestCase],
    headers: dict[str, str],
) -> dict[str, Any]:
    """Tokenize every case through vLLM and reject context overflow."""
    tokenize_url = _tokenize_url(config.endpoint_pool.endpoints[0])
    input_tokens: list[int] = []
    total_tokens: list[int] = []
    violations: list[dict[str, Any]] = []
    observed_model_limits: list[int] = []

    for index, case in enumerate(cases):
        payload: dict[str, Any] = {
            "messages": case.messages,
            "add_generation_prompt": True,
        }
        model = config.workload.model or case.request.get("model")
        if model is not None:
            payload["model"] = model
        if "tools" in case.request:
            payload["tools"] = case.request["tools"]
        response = _post_json(
            tokenize_url,
            payload,
            headers,
            config.endpoint_pool.timeout_s,
        )
        count = _required_positive_int(response.get("count"), "count")
        server_limit = _optional_positive_int(response.get("max_model_len"))
        if server_limit is not None:
            observed_model_limits.append(server_limit)
        configured_limit = config.preflight.max_model_len
        effective_limit = configured_limit or server_limit
        output_tokens = _output_tokens(config, case)
        requested_total = count + output_tokens
        input_tokens.append(count)
        total_tokens.append(requested_total)
        if effective_limit is not None and requested_total > effective_limit:
            violations.append(
                {
                    "index": index,
                    "request_id": case.request_id,
                    "input_tokens": count,
                    "output_tokens": output_tokens,
                    "requested_total_tokens": requested_total,
                    "max_model_len": effective_limit,
                }
            )

    result = {
        "enabled": True,
        "tokenize_url": tokenize_url,
        "request_count": len(cases),
        "input_tokens": distribution(input_tokens),
        "requested_total_tokens": distribution(total_tokens),
        "configured_max_model_len": config.preflight.max_model_len,
        "server_max_model_len": (
            min(observed_model_limits) if observed_model_limits else None
        ),
        "context_violations": violations,
    }
    if violations:
        first = violations[0]
        raise ValueError(
            f"preflight found {len(violations)} context-length violations; "
            f"first request {first['request_id']} asks for "
            f"{first['requested_total_tokens']} tokens with max_model_len="
            f"{first['max_model_len']}"
        )
    return result


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_s: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"tokenize endpoint returned non-object JSON: {url}")
    return value


def _tokenize_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    return urlunsplit((parsed.scheme, parsed.netloc, "/tokenize", "", ""))


def _output_tokens(config: BenchmarkConfig, case: RequestCase) -> int:
    value = config.workload.max_output_tokens
    if value is None:
        value = case.request.get("max_output_tokens", 0)
    output_tokens = int(value or 0)
    cap = config.workload.max_output_tokens_cap
    return min(output_tokens, cap) if cap is not None else output_tokens


def _required_positive_int(value: Any, name: str) -> int:
    parsed = _optional_positive_int(value)
    if parsed is None:
        raise ValueError(f"tokenize response requires positive integer {name}")
    return parsed


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed > 0 else None
