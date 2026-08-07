"""Runtime-neutral cache discovery, clearing, and audit records."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from endpoint_benchmark.target_config import (
    DirectCacheSource,
    DynamoCacheSource,
    TargetConfig,
)
from endpoint_benchmark.transport import now_ms

_MAX_BODY_BYTES = 1024 * 1024
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_URL_USERINFO = re.compile(r"(https?://)[^/@\s]+@")


class CacheControlError(RuntimeError):
    """Raised when every cache instance cannot be cleared safely."""


@dataclass(frozen=True)
class _HttpResult:
    status: int
    body: bytes


@dataclass(frozen=True)
class _DirectInstance:
    source: str
    url: str
    headers: dict[str, str]
    runtime: str | None = None
    version: str | None = None
    hicache: bool = False

    @property
    def address(self) -> str:
        return _safe_address(self.url)


@dataclass
class _RetryProgress:
    next_log_at: float | None = None

    def report(self, address: str, attempts: int, error: str) -> None:
        current = time.monotonic()
        if self.next_log_at is None or current >= self.next_log_at:
            print(
                "cache clear retry: "
                f"instance={address}, attempts={attempts}, error={error}",
                file=sys.stderr,
            )
            self.next_log_at = current + 5.0


def clear_caches(
    target: TargetConfig,
    concurrency: int,
    phase: str = "pre",
    *,
    reset_external: bool = False,
) -> dict[str, Any]:
    """Probe direct instances, clear every source, and return one audit record."""
    started_ms = now_ms()
    deadline = time.monotonic() + target.clear_timeout_s
    cancelled = threading.Event()
    direct = [
        _probe_direct(instance, target, deadline, cancelled)
        for instance in _direct_instances(target)
    ]
    dynamo = [
        (f"dynamo-{index}", source)
        for index, source in enumerate(target.cache_sources)
        if isinstance(source, DynamoCacheSource)
    ]

    futures: dict[Future[Any], str] = {}
    workers = len(direct) + bool(dynamo)
    executor = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        for instance in direct:
            future = executor.submit(
                _clear_direct,
                instance,
                target,
                deadline,
                cancelled,
                reset_external,
            )
            futures[future] = instance.address
        if dynamo:
            future = executor.submit(
                _clear_dynamo_sources, dynamo, target, deadline, cancelled
            )
            futures[future] = "Dynamo"

        instances: list[dict[str, Any]] = []
        for future in as_completed(futures):
            value = future.result()
            instances.extend(value if isinstance(value, list) else [value])
    except Exception as exc:
        cancelled.set()
        for future in futures:
            future.cancel()
        if isinstance(exc, CacheControlError):
            raise
        raise CacheControlError(f"cache clear failed: {exc}") from exc
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    instances.sort(
        key=lambda item: (
            item["source"],
            item["address"],
            -1 if item["instance_id"] is None else item["instance_id"],
        )
    )
    return {
        "phase": phase,
        "concurrency": concurrency,
        "duration_ms": now_ms() - started_ms,
        "success": True,
        "instances": instances,
    }


def _direct_instances(target: TargetConfig) -> list[_DirectInstance]:
    instances: dict[str, _DirectInstance] = {}
    for index, source in enumerate(target.cache_sources):
        if not isinstance(source, DirectCacheSource):
            continue
        endpoints = (
            target.inference_endpoints
            if source.use_inference_endpoints
            else source.endpoints or ()
        )
        headers = source.headers
        if headers is None:
            headers = target.headers if source.use_inference_endpoints else ()
        for endpoint in endpoints:
            origin = _origin(endpoint)
            instances.setdefault(
                _safe_address(origin),
                _DirectInstance(f"direct-{index}", origin, dict(headers)),
            )
    return list(instances.values())


def _probe_direct(
    instance: _DirectInstance,
    target: TargetConfig,
    deadline: float,
    cancelled: threading.Event,
) -> _DirectInstance:
    attempts = 0
    progress = _RetryProgress()
    while True:
        attempts += 1
        try:
            version_response = _http(
                "GET",
                f"{instance.url}/version",
                instance.headers,
                _remaining(deadline),
            )
            if version_response.status in {401, 403}:
                raise CacheControlError(
                    f"runtime probe rejected for {instance.address}: "
                    f"HTTP {version_response.status}"
                )
            if version_response.status >= 500:
                raise OSError(f"HTTP {version_response.status} from /version")
            version = _json(version_response.body)
            if (
                version_response.status == 200
                and isinstance(version, dict)
                and set(version) == {"version"}
                and isinstance(version["version"], str)
            ):
                return _DirectInstance(
                    instance.source,
                    instance.url,
                    instance.headers,
                    runtime="vllm",
                    version=version["version"],
                )

            info_response = _http(
                "GET",
                f"{instance.url}/server_info",
                instance.headers,
                _remaining(deadline),
            )
            if info_response.status in {401, 403}:
                raise CacheControlError(
                    f"runtime probe rejected for {instance.address}: "
                    f"HTTP {info_response.status}"
                )
            if info_response.status >= 500:
                raise OSError(f"HTTP {info_response.status} from /server_info")
            info = _json(info_response.body)
            if (
                info_response.status == 200
                and isinstance(info, dict)
                and isinstance(info.get("version"), str)
                and isinstance(info.get("model_path"), str)
                and isinstance(info.get("internal_states"), list)
            ):
                return _DirectInstance(
                    instance.source,
                    instance.url,
                    instance.headers,
                    runtime="sglang",
                    version=info["version"],
                    hicache=info.get("enable_hierarchical_cache") is True,
                )
            raise CacheControlError(
                f"unknown runtime at {instance.address}; "
                f"/version={version_response.status} "
                f"{_summary(version_response.body, instance.headers.values())!r}, "
                f"/server_info={info_response.status} "
                f"{_summary(info_response.body, instance.headers.values())!r}"
            )
        except CacheControlError:
            raise
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            _retry_wait(
                target,
                deadline,
                cancelled,
                progress,
                instance.address,
                attempts,
                _error_summary(exc, instance.headers.values()),
            )


def _clear_direct(
    instance: _DirectInstance,
    target: TargetConfig,
    deadline: float,
    cancelled: threading.Event,
    reset_external: bool,
) -> dict[str, Any]:
    progress = _RetryProgress()
    if instance.runtime == "vllm":
        query = urllib.parse.urlencode(
            {
                "reset_running_requests": "false",
                "reset_external": str(reset_external).lower(),
            }
        )
        result, attempts = _retry_http(
            instance,
            "reset_prefix_cache",
            f"{instance.url}/reset_prefix_cache?{query}",
            target,
            deadline,
            cancelled,
            progress,
            response_succeeded=_vllm_reset_succeeded,
        )
        operation = "reset_prefix_cache"
    elif instance.runtime == "sglang":
        result, attempts = _retry_http(
            instance,
            "flush_cache",
            f"{instance.url}/flush_cache?timeout=0",
            target,
            deadline,
            cancelled,
            progress,
            retry_statuses={400},
        )
        operation = "flush_cache"
        if instance.hicache:
            storage, storage_attempts = _retry_http(
                instance,
                "clear_hicache_storage",
                f"{instance.url}/hicache/storage-backend/clear",
                target,
                deadline,
                cancelled,
                progress,
            )
            result = storage
            attempts += storage_attempts
            operation += "+clear_hicache_storage"
    else:  # pragma: no cover - only constructed by the probe above
        raise CacheControlError(f"unknown runtime at {instance.address}")

    print(
        "cache clear success: "
        f"instance={instance.address}, runtime={instance.runtime}, attempts={attempts}",
        file=sys.stderr,
    )
    return {
        "source": instance.source,
        "address": instance.address,
        "instance_id": None,
        "runtime": instance.runtime,
        "version": instance.version,
        "operation": operation,
        "attempts": attempts,
        "status_code": result.status,
        "response": _summary(result.body, instance.headers.values()),
        "success": True,
    }


def _retry_http(
    instance: _DirectInstance,
    operation: str,
    url: str,
    target: TargetConfig,
    deadline: float,
    cancelled: threading.Event,
    progress: _RetryProgress,
    retry_statuses: set[int] | None = None,
    response_succeeded: Callable[[bytes], bool] | None = None,
) -> tuple[_HttpResult, int]:
    retry_statuses = retry_statuses or set()
    attempts = 0
    while True:
        attempts += 1
        try:
            result = _http(
                "POST", url, instance.headers, _remaining(deadline), data=b""
            )
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            error = _error_summary(exc, instance.headers.values())
        else:
            if 200 <= result.status < 300:
                if response_succeeded is None or response_succeeded(result.body):
                    return result, attempts
                error = (
                    f"HTTP {result.status}: "
                    f"{_summary(result.body, instance.headers.values())}"
                )
            elif result.status >= 500 or result.status in retry_statuses:
                error = (
                    f"HTTP {result.status}: "
                    f"{_summary(result.body, instance.headers.values())}"
                )
            else:
                if instance.runtime == "vllm" and result.status in {404, 405}:
                    raise CacheControlError(
                        f"{operation} unavailable at {instance.address} "
                        f"(HTTP {result.status}); check VLLM_SERVER_DEV_MODE=1"
                    )
                raise CacheControlError(
                    f"{operation} failed at {instance.address}: "
                    f"HTTP {result.status}: "
                    f"{_summary(result.body, instance.headers.values())}"
                )
        _retry_wait(
            target,
            deadline,
            cancelled,
            progress,
            instance.address,
            attempts,
            error,
        )


def _vllm_reset_succeeded(body: bytes) -> bool:
    if not body.strip():
        return True
    value = _json(body)
    return isinstance(value, dict) and value.get("success") is True


def _http(
    method: str,
    url: str,
    headers: dict[str, str],
    timeout_s: float,
    data: bytes | None = None,
) -> _HttpResult:
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=min(timeout_s, 30.0)) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            body = response.read(_MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read(_MAX_BODY_BYTES + 1)
    if len(body) > _MAX_BODY_BYTES:
        raise CacheControlError(
            f"HTTP response exceeded {_MAX_BODY_BYTES} bytes at {_safe_address(url)}"
        )
    return _HttpResult(int(status), body)


def _retry_wait(
    target: TargetConfig,
    deadline: float,
    cancelled: threading.Event,
    progress: _RetryProgress,
    address: str,
    attempts: int,
    error: str,
) -> None:
    if cancelled.is_set():
        raise CacheControlError("cache clear cancelled after another instance failed")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CacheControlError(
            f"cache clear timed out after {target.clear_timeout_s}s at {address}; "
            f"attempts={attempts}, last_error={error}"
        )
    progress.report(address, attempts, error)
    sleep_s = min(target.clear_retry_interval_s, remaining)
    if sleep_s and cancelled.wait(sleep_s):
        raise CacheControlError("cache clear cancelled after another instance failed")


def _clear_dynamo_sources(
    sources: list[tuple[str, DynamoCacheSource]],
    target: TargetConfig,
    deadline: float,
    cancelled: threading.Event,
) -> list[dict[str, Any]]:
    return asyncio.run(
        _clear_dynamo_sources_async(sources, target, deadline, cancelled)
    )


async def _clear_dynamo_sources_async(
    sources: list[tuple[str, DynamoCacheSource]],
    target: TargetConfig,
    deadline: float,
    cancelled: threading.Event,
) -> list[dict[str, Any]]:
    runtime_class = _dynamo_runtime_class()
    loop = asyncio.get_running_loop()
    try:
        runtime = runtime_class(
            loop,
            os.environ.get("DYN_DISCOVERY_BACKEND", "etcd"),
            os.environ.get("DYN_REQUEST_PLANE", "tcp"),
        )
    except Exception as exc:
        raise CacheControlError(
            f"Dynamo runtime initialization failed: {_error_summary(exc)}"
        ) from exc

    discovered: list[tuple[str, str, Any, int]] = []
    try:
        for source_name, source in sources:
            for component in source.components:
                address = f"dyn://{source.namespace}.{component}.clear_kv_blocks"
                try:
                    endpoint = runtime.endpoint(address)
                    client = await asyncio.wait_for(
                        endpoint.client(), timeout=_remaining(deadline)
                    )
                    instance_ids = list(
                        dict.fromkeys(
                            await asyncio.wait_for(
                                client.wait_for_instances(),
                                timeout=_remaining(deadline),
                            )
                        )
                    )
                except Exception as exc:
                    raise CacheControlError(
                        f"Dynamo discovery failed for {address}: "
                        f"{_error_summary(exc)}"
                    ) from exc
                if not instance_ids:
                    raise CacheControlError(
                        f"Dynamo discovery found no instances: {address}"
                    )
                discovered.extend(
                    (source_name, address, client, instance_id)
                    for instance_id in instance_ids
                )

        return list(
            await asyncio.gather(
                *(
                    _clear_dynamo_instance(
                        source_name,
                        address,
                        client,
                        instance_id,
                        target,
                        deadline,
                        cancelled,
                    )
                    for source_name, address, client, instance_id in discovered
                )
            )
        )
    finally:
        runtime.shutdown()


async def _clear_dynamo_instance(
    source: str,
    address: str,
    client: Any,
    instance_id: int,
    target: TargetConfig,
    deadline: float,
    cancelled: threading.Event,
) -> dict[str, Any]:
    attempts = 0
    progress = _RetryProgress()
    label = f"{address}#{instance_id}"
    while True:
        attempts += 1
        try:
            stream = await asyncio.wait_for(
                client.direct({}, instance_id, annotated=False),
                timeout=_remaining(deadline),
            )
            responses: list[Any] = []
            async with asyncio.timeout(_remaining(deadline)):
                async for response in stream:
                    responses.append(response)
        except Exception as exc:
            error = _error_summary(exc)
        else:
            statuses = [
                item.get("status") if isinstance(item, dict) else None
                for item in responses
            ]
            if responses and all(status == "success" for status in statuses):
                print(
                    "cache clear success: "
                    f"instance={label}, runtime=dynamo, attempts={attempts}",
                    file=sys.stderr,
                )
                return {
                    "source": source,
                    "address": address,
                    "instance_id": instance_id,
                    "runtime": "dynamo",
                    "version": _dynamo_version(),
                    "operation": "clear_kv_blocks",
                    "attempts": attempts,
                    "status_code": None,
                    "response": _value_summary(responses[-1]),
                    "success": True,
                }
            if "error" in statuses:
                error = f"status=error: {_value_summary(responses)}"
            else:
                raise CacheControlError(
                    f"invalid Dynamo clear response from {label}: "
                    f"{_value_summary(responses)}"
                )
        if cancelled.is_set():
            raise CacheControlError(
                "cache clear cancelled after another instance failed"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CacheControlError(
                f"cache clear timed out after {target.clear_timeout_s}s at {label}; "
                f"attempts={attempts}, last_error={error}"
            )
        progress.report(label, attempts, error)
        await asyncio.sleep(min(target.clear_retry_interval_s, remaining))


def _dynamo_runtime_class() -> Any:
    try:
        from dynamo.runtime import DistributedRuntime
    except ImportError as exc:
        raise CacheControlError(
            "Dynamo cache sources require endpoint-benchmark[dynamo] on Linux"
        ) from exc
    return DistributedRuntime


def _dynamo_version() -> str | None:
    try:
        return importlib.metadata.version("ai-dynamo-runtime")
    except importlib.metadata.PackageNotFoundError:
        return None


def _origin(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _safe_address(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or "unknown"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    return urllib.parse.urlunsplit((parsed.scheme, netloc, "", "", ""))


def _json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _summary(body: bytes, secrets: Any = ()) -> str:
    return _redact(_clean(body[:1024].decode("utf-8", errors="replace")), secrets)


def _value_summary(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = repr(value)
    return _clean(encoded)[:1024]


def _error_summary(exc: BaseException, secrets: Any = ()) -> str:
    return _redact(_clean(f"{type(exc).__name__}: {exc}"), secrets)[:1024]


def _clean(value: str) -> str:
    return _URL_USERINFO.sub(r"\1", _CONTROL_CHARACTERS.sub(" ", value))


def _redact(value: str, secrets: Any) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(str(secret), "[REDACTED]")
    return value


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("shared cache-clear deadline expired")
    return remaining
