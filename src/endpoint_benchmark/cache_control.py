"""vLLM prefix-cache reset lifecycle support."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from endpoint_benchmark.models import PrefixCacheResetConfig
from endpoint_benchmark.transport import now_ms


class PrefixCacheResetError(RuntimeError):
    """Raised when an engine prefix cache cannot be reset safely."""


def reset_prefix_caches(
    endpoints: tuple[str, ...],
    headers: dict[str, str],
    config: PrefixCacheResetConfig,
    concurrency: int,
    phase: str,
) -> dict[str, Any]:
    """Reset all unique engine origins in parallel and return an audit record."""
    reset_urls = tuple(dict.fromkeys(_reset_url(endpoint) for endpoint in endpoints))
    started_ms = now_ms()
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(reset_urls)) as executor:
        futures = {
            executor.submit(_reset_one, url, headers, config): url
            for url in reset_urls
        }
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                url = futures[future]
                raise PrefixCacheResetError(
                    "prefix cache reset failed: "
                    f"concurrency={concurrency}, phase={phase}, url={url}, error={exc}"
                ) from exc
    results.sort(key=lambda item: item["url"])
    return {
        "concurrency": concurrency,
        "phase": phase,
        "engine_count": len(reset_urls),
        "duration_ms": now_ms() - started_ms,
        "success": True,
        "engines": results,
    }


def _reset_one(
    url: str,
    headers: dict[str, str],
    config: PrefixCacheResetConfig,
) -> dict[str, Any]:
    started_ms = now_ms()
    deadline = time.monotonic() + config.timeout_s
    attempts = 0
    last_response: dict[str, Any] | None = None
    while True:
        attempts += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"reset did not succeed within {config.timeout_s}s; "
                f"attempts={attempts - 1}, last_response={last_response}"
            )
        request = urllib.request.Request(
            _with_query(url, config.reset_external),
            data=b"",
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=min(remaining, 30.0)
            ) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:1000]
            if exc.code == 404:
                raise RuntimeError(
                    "vLLM prefix-cache reset endpoint is unavailable (HTTP 404); "
                    "start every engine with VLLM_SERVER_DEV_MODE=1"
                ) from exc
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid reset response: {body[:1000]}") from exc
        if not isinstance(value, dict) or "success" not in value:
            raise RuntimeError(f"invalid reset response: {value!r}")
        last_response = value
        if value["success"] is True:
            return {
                "url": url,
                "attempts": attempts,
                "duration_ms": now_ms() - started_ms,
                "success": True,
            }
        sleep_s = min(config.retry_interval_s, max(0.0, deadline - time.monotonic()))
        if sleep_s:
            time.sleep(sleep_s)


def _reset_url(endpoint: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "/reset_prefix_cache", "", "")
    )


def _with_query(url: str, reset_external: bool) -> str:
    query = urllib.parse.urlencode(
        {
            "reset_running_requests": "false",
            "reset_external": str(reset_external).lower(),
        }
    )
    return f"{url}?{query}"
