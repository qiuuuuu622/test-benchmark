"""Configuration and result data models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class EndpointPoolConfig:
    """Request endpoint pool configuration."""

    endpoints: tuple[str, ...]
    routing: str = "round_robin"
    timeout_s: float = 720.0
    connection_mode: str = "reuse"
    api_key_env: str | None = None
    headers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.endpoints:
            raise ValueError("at least one endpoint is required")
        for endpoint in self.endpoints:
            parsed = urlsplit(endpoint)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"invalid HTTP endpoint: {endpoint}")
        if self.routing != "round_robin":
            raise ValueError("v0.1 only supports round_robin routing")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if self.connection_mode not in {"reuse", "new"}:
            raise ValueError("connection_mode must be 'reuse' or 'new'")


@dataclass(frozen=True)
class WorkloadConfig:
    """Dataset and request payload defaults."""

    dataset: Path
    model: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    max_output_tokens_cap: int | None = None
    ignore_eos: bool | None = None
    extra_body: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if (
            self.max_output_tokens_cap is not None
            and self.max_output_tokens_cap <= 0
        ):
            raise ValueError("max_output_tokens_cap must be positive")
        reserved = {"messages", "model", "stream", "stream_options"}
        conflicts = reserved.intersection(self.extra_body)
        if conflicts:
            names = ", ".join(sorted(conflicts))
            raise ValueError(f"extra_body cannot override: {names}")


@dataclass(frozen=True)
class LoadConfig:
    """Closed-loop load configuration."""

    concurrency: tuple[int, ...]
    num_prompts: int | None = None
    shuffle: bool = False
    seed: int | None = None
    warmup_prompts: int = 0

    def __post_init__(self) -> None:
        if not self.concurrency or any(value <= 0 for value in self.concurrency):
            raise ValueError("concurrency levels must be positive")
        if self.num_prompts is not None and self.num_prompts <= 0:
            raise ValueError("num_prompts must be positive")
        if self.warmup_prompts < 0:
            raise ValueError("warmup_prompts cannot be negative")


@dataclass(frozen=True)
class MeasurementConfig:
    """Client and optional server measurement configuration."""

    metrics_url: str | None = None
    server_metrics_enabled: bool = True
    record_chunk_timestamps: bool = False


@dataclass(frozen=True)
class PrefixCacheResetConfig:
    """Prefix-cache reset lifecycle configuration."""

    enabled: bool = True
    timeout_s: float = 60.0
    retry_interval_s: float = 0.5
    reset_external: bool = False

    def __post_init__(self) -> None:
        if self.timeout_s <= 0:
            raise ValueError("prefix cache reset timeout must be positive")
        if self.retry_interval_s < 0:
            raise ValueError("prefix cache reset retry interval cannot be negative")


@dataclass(frozen=True)
class OutputConfig:
    """Result serialization configuration."""

    directory: Path = Path("benchmark_results")
    label: str = "benchmark"
    run_name: str | None = None
    overwrite: bool = False


@dataclass(frozen=True)
class BenchmarkConfig:
    """Complete benchmark configuration."""

    endpoint_pool: EndpointPoolConfig
    workload: WorkloadConfig
    load: LoadConfig
    measurement: MeasurementConfig = field(default_factory=MeasurementConfig)
    prefix_cache_reset: PrefixCacheResetConfig = field(
        default_factory=PrefixCacheResetConfig
    )
    output: OutputConfig = field(default_factory=OutputConfig)


@dataclass(frozen=True)
class RequestCase:
    """Canonical dataset row."""

    request_id: str
    messages: list[dict[str, Any]]
    request: dict[str, Any]
    metadata: dict[str, Any]


@dataclass
class RequestResult:
    """Client observations for one request."""

    request_id: str
    index: int
    endpoint_index: int
    success: bool
    status_code: int | None
    error: dict[str, str] | None
    input_tokens: int | None
    output_tokens: int | None
    e2e_latency_ms: float
    ttft_ms: float | None
    tpot_ms: float | None
    started_at_offset_ms: float
    finished_at_offset_ms: float
    finish_reason: str | None
    chunk_arrival_offsets_ms: list[float] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)


@dataclass
class BenchmarkResult:
    """Complete result for all concurrency levels."""

    run: dict[str, Any]
    summaries: list[dict[str, Any]]
    requests_by_concurrency: dict[int, list[RequestResult]]
    server_metrics_by_concurrency: dict[int, dict[str, Any] | None]
    cache_resets_by_concurrency: dict[int, list[dict[str, Any]]]
    warnings: list[str]
    output_directory: Path
