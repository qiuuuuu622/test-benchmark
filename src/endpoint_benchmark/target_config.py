"""Strict logical-target configuration."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

Headers = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class DirectCacheSource:
    """Explicit HTTP cache instances, or the target's inference instances."""

    endpoints: tuple[str, ...] | None = None
    use_inference_endpoints: bool = False
    headers: Headers | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.use_inference_endpoints, bool):
            raise ValueError("use_inference_endpoints must be a boolean")
        if (self.endpoints is not None) == self.use_inference_endpoints:
            raise ValueError(
                "direct cache source must choose endpoints or "
                "use_inference_endpoints=true"
            )
        if self.endpoints is not None:
            _validate_urls(self.endpoints, "direct cache endpoints")
        if self.headers is not None:
            _validate_headers(self.headers)


@dataclass(frozen=True)
class DynamoCacheSource:
    """Dynamo 1.3.0 legacy clear_kv_blocks capabilities."""

    namespace: str
    components: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.namespace, str)
            or not self.namespace
            or "." in self.namespace
        ):
            raise ValueError("Dynamo namespace must be a non-empty endpoint segment")
        if not self.components or any(
            not isinstance(item, str) or not item or "." in item
            for item in self.components
        ):
            raise ValueError("Dynamo components must be non-empty endpoint segments")


CacheSource = DirectCacheSource | DynamoCacheSource


@dataclass(frozen=True)
class TargetConfig:
    """Inference entrypoints and every cache instance source for one target."""

    inference_endpoints: tuple[str, ...]
    cache_sources: tuple[CacheSource, ...]
    headers: Headers = ()
    clear_timeout_s: float = 60.0
    clear_retry_interval_s: float = 0.5

    def __post_init__(self) -> None:
        _validate_urls(self.inference_endpoints, "inference endpoints")
        if not self.cache_sources:
            raise ValueError("target must contain at least one cache source")
        if any(
            not isinstance(source, (DirectCacheSource, DynamoCacheSource))
            for source in self.cache_sources
        ):
            raise ValueError("target contains an unsupported cache source")
        _validate_headers(self.headers)
        if isinstance(self.clear_timeout_s, bool) or not isinstance(
            self.clear_timeout_s, (int, float)
        ):
            raise ValueError("clear_timeout_s must be a number")
        if self.clear_timeout_s <= 0:
            raise ValueError("clear_timeout_s must be positive")
        if isinstance(self.clear_retry_interval_s, bool) or not isinstance(
            self.clear_retry_interval_s, (int, float)
        ):
            raise ValueError("clear_retry_interval_s must be a number")
        if self.clear_retry_interval_s < 0:
            raise ValueError("clear_retry_interval_s cannot be negative")


def default_targets_path() -> Path:
    """Return the XDG-compatible default target file path."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "endpoint-benchmark" / "targets.toml"


def load_target(path: Path, name: str) -> TargetConfig:
    """Load and strictly validate one named target from TOML."""
    with path.expanduser().open("rb") as handle:
        value = tomllib.load(handle)
    _check_keys(value, {"targets"}, "root")
    targets = _table(value.get("targets"), "targets")
    if name not in targets:
        raise ValueError(f"target is not defined: {name}")
    target = _table(targets[name], f"targets.{name}")
    _check_keys(
        target,
        {
            "inference_endpoints",
            "headers",
            "header_env",
            "clear_timeout_s",
            "clear_retry_interval_s",
            "cache_sources",
        },
        f"targets.{name}",
    )
    context = f"targets.{name}"
    sources = _list(target.get("cache_sources"), f"{context}.cache_sources")
    return TargetConfig(
        inference_endpoints=_strings(
            target.get("inference_endpoints"), f"{context}.inference_endpoints"
        ),
        cache_sources=tuple(
            _cache_source(item, f"{context}.cache_sources[{index}]")
            for index, item in enumerate(sources)
        ),
        headers=_headers(target, context) or (),
        clear_timeout_s=_number(target.get("clear_timeout_s", 60.0), context),
        clear_retry_interval_s=_number(
            target.get("clear_retry_interval_s", 0.5), context
        ),
    )


def _cache_source(value: Any, context: str) -> CacheSource:
    source = _table(value, context)
    kind = source.get("kind")
    if kind == "direct":
        _check_keys(
            source,
            {
                "kind",
                "endpoints",
                "use_inference_endpoints",
                "headers",
                "header_env",
            },
            context,
        )
        endpoints = (
            _strings(source["endpoints"], f"{context}.endpoints")
            if "endpoints" in source
            else None
        )
        use_inference = source.get("use_inference_endpoints", False)
        if not isinstance(use_inference, bool):
            raise ValueError(f"{context}.use_inference_endpoints must be a boolean")
        return DirectCacheSource(
            endpoints=endpoints,
            use_inference_endpoints=use_inference,
            headers=_headers(source, context),
        )
    if kind == "dynamo":
        _check_keys(source, {"kind", "namespace", "components"}, context)
        namespace = source.get("namespace")
        if not isinstance(namespace, str):
            raise ValueError(f"{context}.namespace must be a string")
        return DynamoCacheSource(
            namespace=namespace,
            components=_strings(source.get("components"), f"{context}.components"),
        )
    raise ValueError(f"{context}.kind must be 'direct' or 'dynamo'")


def _headers(table: dict[str, Any], context: str) -> Headers | None:
    if "headers" not in table and "header_env" not in table:
        return None
    headers = _string_table(table.get("headers", {}), f"{context}.headers")
    env_headers = _string_table(table.get("header_env", {}), f"{context}.header_env")
    duplicates = headers.keys() & env_headers.keys()
    if duplicates:
        names = ", ".join(sorted(duplicates))
        raise ValueError(f"{context} defines headers twice: {names}")
    for header, env_name in env_headers.items():
        if env_name not in os.environ:
            raise ValueError(
                f"{context}.header_env references missing environment variable: "
                f"{env_name}"
            )
        headers[header] = os.environ[env_name]
    return tuple(headers.items())


def _validate_urls(values: tuple[str, ...], context: str) -> None:
    if not values:
        raise ValueError(f"{context} cannot be empty")
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{context} must contain non-empty strings")
        parsed = urlsplit(value)
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError(f"invalid HTTP URL in {context}") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"invalid HTTP URL in {context}")


def _validate_headers(headers: Headers) -> None:
    for header in headers:
        if not isinstance(header, tuple) or len(header) != 2:
            raise ValueError("headers must contain name/value pairs")
        name, value = header
        if not isinstance(name, str) or not name or not isinstance(value, str):
            raise ValueError("headers must contain non-empty string names and values")
        if any(
            ord(character) < 32 or ord(character) == 127
            for field in (name, value)
            for character in field
        ):
            raise ValueError("headers cannot contain control characters")


def _check_keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = value.keys() - allowed
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unknown fields in {context}: {names}")


def _table(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be a table")
    return value


def _string_table(value: Any, context: str) -> dict[str, str]:
    table = _table(value, context)
    if any(
        not isinstance(key, str) or not key or not isinstance(item, str)
        for key, item in table.items()
    ):
        raise ValueError(f"{context} must contain string keys and values")
    return dict(table)


def _list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{context} must be a non-empty array")
    return value


def _strings(value: Any, context: str) -> tuple[str, ...]:
    items = _list(value, context)
    if any(not isinstance(item, str) or not item for item in items):
        raise ValueError(f"{context} must contain non-empty strings")
    return tuple(items)


def _number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"clear timeout and retry interval in {context} must be numbers"
        )
    return float(value)
