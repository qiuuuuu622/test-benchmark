# Lightweight Endpoint Benchmark Interface Contract

## 1. Contract status

This document defines the package 0.2.0 input and output contract. The benchmark
target is an endpoint pool. A model name is a workload request field.

The following version fields are independent:

```json
{
  "schema_version": "1.0",
  "metric_semantics_version": "1.0",
  "package_version": "0.2.0"
}
```

## 2. Python API

The minimal public API is synchronous:

```python
from endpoint_benchmark import BenchmarkConfig, BenchmarkResult, run_benchmark

result: BenchmarkResult = run_benchmark(BenchmarkConfig(...))
```

The public configuration is composed rather than represented by one unstructured
argument dictionary:

```python
@dataclass(frozen=True)
class BenchmarkConfig:
    endpoint_pool: EndpointPoolConfig
    workload: WorkloadConfig
    load: LoadConfig
    measurement: MeasurementConfig
    preflight: PreflightConfig
    validity: ValidityConfig
    isolated_miss: bool
    prefix_cache_reset: PrefixCacheResetConfig
    output: OutputConfig
```

Conceptual subcontracts are:

```python
@dataclass(frozen=True)
class EndpointPoolConfig:
    endpoints: tuple[str, ...]
    routing: str = "round_robin"
    timeout_s: float = 720.0
    connection_mode: str = "reuse"
    api_key_env: str | None = None
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class WorkloadConfig:
    dataset: Path
    model: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    max_output_tokens_cap: int | None = None
    ignore_eos: bool | None = None
    extra_body: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class LoadConfig:
    concurrency: tuple[int, ...]
    num_prompts: int | None = None
    shuffle: bool = False
    seed: int | None = None
    warmup_prompts: int = 0


@dataclass(frozen=True)
class MeasurementConfig:
    metrics_url: str | None = None
    server_metrics_enabled: bool = True
    record_chunk_timestamps: bool = False


@dataclass(frozen=True)
class PreflightConfig:
    tokenize: bool = False
    max_model_len: int | None = None


@dataclass(frozen=True)
class ValidityConfig:
    min_success_rate: float = 1.0
    fail_on_request_error: bool = True


@dataclass(frozen=True)
class PrefixCacheResetConfig:
    enabled: bool = True
    timeout_s: float = 60.0
    retry_interval_s: float = 0.5
    reset_external: bool = False


@dataclass(frozen=True)
class OutputConfig:
    directory: Path = Path("benchmark_results")
    label: str = "benchmark"
    run_name: str | None = None
    overwrite: bool = False
```

These class names and responsibility boundaries are the current package 0.2.0
contract.

## 3. CLI contract

The command shape is:

```bash
endpoint-benchmark run [OPTIONS]
```

### 3.1 Endpoint pool

```text
--endpoint URL                Required; repeatable
--routing round_robin         Default: round_robin
--timeout SECONDS             Default: 720
--connection-mode reuse|new   Default: reuse
--api-key-env NAME            Optional; authentication is disabled by default
--header KEY=VALUE            Optional; repeatable
```

Each endpoint is a complete request URL, for example:

```text
http://host-a:8000/v1/chat/completions
```

The endpoint list must be non-empty. All endpoints are assumed to expose
equivalent OpenAI-compatible behavior.

### 3.2 Workload

```text
--dataset PATH                    Required JSONL path
--model NAME                      Default request payload value
--temperature FLOAT               Optional request override
--max-output-tokens N             Optional request override
--max-output-tokens-cap N         Optional safety ceiling
--ignore-eos / --no-ignore-eos    Optional vLLM extension override
--extra-body-json PATH            Optional validated request extensions
```

The model name is copied into the request payload. It is not used to load or
inspect a model.

The effective request-field precedence is:

```text
explicit CLI override > dataset request field > omit and use server default
```

`max-output-tokens-cap` is applied after precedence resolution.

`extra_body` must not override fields owned by the benchmark:

```text
messages, model, stream, stream_options
```

### 3.3 Load

```text
--concurrency N [N ...]       Required positive integer levels
--num-prompts N               Optional positive request limit
--shuffle                     Default: false
--seed N                      Optional reproducibility seed
--warmup-prompts N            Default: 0
```

The current implementation uses closed-loop scheduling. `concurrency` means total in-flight requests
across the endpoint pool, not concurrency per endpoint. Concurrency levels run
sequentially.

Warmup requests are excluded from formal counts, timing, throughput, and server
metrics deltas.

### 3.4 Measurement

```text
--metrics-url URL             Optional explicit override
--no-server-metrics           Disable server metrics collection
--record-chunk-timestamps     Default: false
--preflight-tokenize          Default: false
--max-model-len N             Optional; also enables tokenize preflight
--min-success-rate RATE       Default: 1.0
--fail-on-request-error       Default: true; --no-fail-on-request-error overrides
```

All request observations enter one endpoint-pool measurement pool. The optional
metrics URL is a single logical service-pool metrics source. By default it is
derived as `/metrics` on the first endpoint origin. The benchmark does not
combine per-machine Prometheus endpoints. `max-model-len` is a preflight check,
not a server setting. The minimum success rate is evaluated per concurrency
level; `fail-on-request-error` only controls exit code 3 for an invalid run.

### 3.5 Prefix cache lifecycle

```text
--reset-prefix-cache                 Default: true
--no-reset-prefix-cache              Explicitly disable reset
--prefix-cache-reset-timeout SEC     Default: 60
--prefix-cache-reset-interval SEC    Default: 0.5
--reset-external-prefix-cache        Default: false
--isolated-miss                      Give every logical request a unique cache identity
```

For every concurrency level, warmup is followed by a pre-reset. The measured
workload starts only after every unique engine origin reports `success=true`.
After all workload requests complete, metrics-after is collected and a post-reset
must succeed before the next level begins. Reset time is excluded from benchmark
duration and latency. vLLM must expose the development endpoint by starting with
`VLLM_SERVER_DEV_MODE=1`.

Before any warmup, the runner performs a `startup_check` reset against every
unique engine origin. An HTTP 404 aborts immediately with an explicit
`VLLM_SERVER_DEV_MODE=1` diagnostic.

`--isolated-miss` skips startup, pre, post, and failure resets. It rejects an
explicit `--reset-prefix-cache`, external reset, reset timeout, or reset interval.
Use `--no-reset-prefix-cache` for the cache-enabled baseline in an A/B run.
Dynamo is treated as an ordinary HTTP endpoint; the client does not discover
runtimes, probe backends, or clear Dynamo caches.

### 3.6 Output

```text
--output-dir PATH             Run root; default: benchmark_results
--label NAME                  Default: benchmark
--run-name NAME               Optional fixed run-directory name
--overwrite                   Default: false
```

Without `--run-name`, output is written to a unique
`<output-dir>/<label>_<YYYYMMDD_HHMMSS>/` directory. With an explicit run name,
an existing non-empty directory requires `--overwrite`.

### 3.7 Example

```bash
endpoint-benchmark run \
  --endpoint http://host-a:8000/v1/chat/completions \
  --endpoint http://host-b:8000/v1/chat/completions \
  --routing round-robin \
  --model served-model \
  --dataset workload.jsonl \
  --num-prompts 1000 \
  --concurrency 1 8 16 32 64 \
  --warmup-prompts 10 \
  --max-output-tokens-cap 1024 \
  --reset-prefix-cache \
  --output-dir results \
  --label run-001
```

## 4. Dataset contract

The canonical format is UTF-8 JSONL with one object per request:

```json
{
  "id": "request-001",
  "messages": [
    {"role": "user", "content": "Explain speculative decoding."}
  ],
  "request": {
    "max_output_tokens": 256,
    "temperature": 0.0
  },
  "metadata": {
    "category": "reasoning",
    "prompt_tokens_estimate": 12
  }
}
```

### 4.1 Required fields

- `messages`: a non-empty OpenAI-compatible message array.

### 4.2 Optional fields

- `id`: stable request identifier; generated from the row index if absent;
- `request`: per-request generation fields;
- `metadata`: arbitrary analysis metadata not sent to the server.

Tool requests may include `tools` and `tool_choice` inside `request`.

### 4.3 Legacy normalization

A compatibility adapter may accept these aliases:

```text
target_tokens
max_tokens
max_completion_tokens
prompt_tokens_est
```

They are normalized to canonical fields before scheduling. Normalization warnings
are recorded in the top-level `warnings` field of `run.json`.

### 4.4 Isolated miss media

In isolated mode every warmup and measured logical request receives one random
16-character hexadecimal identity. Text receives a `[rid:<identity>]` prefix in
the earliest controllable system or user text. Multimodal rows use standard
`messages` content parts and exactly one strategy per dataset:

- vLLM image parts place `{isolated_miss_id}` in the top-level `uuid`; the URL is
  unchanged;
- SGLang image parts place `{isolated_miss_id}` in the URL and omit `uuid`.

Every image must declare exactly one placeholder strategy. Mixed strategies and
unsupported media fail before network I/O. The client does not send `cache_salt`.

## 5. Request payload contract

The client enforces `messages`, `stream`, and `stream_options`. `model` is added
only when provided by the CLI or request case:

```json
{
  "model": "served-model",
  "messages": [],
  "stream": true,
  "stream_options": {"include_usage": true}
}
```

The client may add validated workload fields such as temperature, output-token
limit, tools, tool choice, and backend-specific extensions. Isolated mode rewrites
a deep copy of `messages`; retries before a response reuse the same encoded body.

## 6. Metric definitions

### 6.1 Measured interval

`benchmark_duration_s` begins immediately before formal workload submission and
ends when all formal request results have been collected. It excludes warmup,
cache clearing, server metrics retrieval, dataset loading, and output writing,
but includes a small amount of client scheduling overhead.

### 6.2 End-to-end latency

```text
e2e_latency_ms = stream_end_ms - request_start_ms
```

### 6.3 TTFT

```text
ttft_ms = first_observable_output_ms - request_start_ms
```

The first observable output is the first non-empty `content` or `tool_calls`
delta. For tool-call workloads this is time to first observable output, retained
under the familiar TTFT name.

### 6.4 TPOT

For a successful request with accurate usage and more than one completion token:

```text
tpot_ms = (stream_end_ms - first_observable_output_ms)
          / (completion_tokens - 1)
```

TPOT is `null` when the required observations are unavailable.

### 6.5 Throughput

All throughput is endpoint-pool-wide:

```text
request_throughput_rps = successful_requests / benchmark_duration_s

input_token_throughput_tps =
    sum(input_tokens) / benchmark_duration_s

output_token_throughput_tps =
    sum(output_tokens) / benchmark_duration_s

total_token_throughput_tps =
    (sum(input_tokens) + sum(output_tokens)) / benchmark_duration_s
```

If any successful request lacks input or output token usage, all three token
throughput values are `null` and `token_counts_complete=false`. Chunk counts are
never substituted for token counts.

System output throughput is distinct from single-request generation speed;
`1000 / TPOT` must not be reported as endpoint-pool throughput.

### 6.6 Distribution fields

Every latency or token-length distribution uses:

```json
{
  "samples": 1000,
  "mean": 12.3,
  "min": 4.1,
  "p50": 10.2,
  "p90": 17.1,
  "p95": 19.4,
  "p99": 26.8,
  "max": 44.0
}
```

Unavailable distributions have zero samples and `null` statistics.

## 7. Server metrics

For every formal concurrency run:

```text
read metrics before
run workload
read metrics after
calculate counter deltas
calculate rates from the deltas
```

The configured URL is one logical metrics source and should already represent
the whole endpoint pool when pool-wide server metrics are required. Combining
metrics from individual machines is outside the benchmark client's scope.

For speculative decoding:

```text
accepted_tokens = accepted-token counter delta
draft_tokens = draft-token counter delta
acceptance_rate = accepted_tokens / draft_tokens
```

If the configured URL cannot be read before and after the run, server metrics
are marked unavailable and their rates are `null`. This does not invalidate the
client-side request timing and throughput calculated from the measurement pool.

## 8. Request result schema

Each formal request produces a JSONL record conceptually shaped as:

```json
{
  "concurrency": 32,
  "request_id": "request-001",
  "index": 0,
  "endpoint_index": 0,
  "success": true,
  "status_code": 200,
  "error": null,
  "input_tokens": 128,
  "output_tokens": 256,
  "e2e_latency_ms": 3120.5,
  "ttft_ms": 184.2,
  "tpot_ms": 11.51,
  "started_at_offset_ms": 12.4,
  "finished_at_offset_ms": 3132.9,
  "finish_reason": "length",
  "transport_retries": 0,
  "inter_chunk_latencies_ms": [11.2, 10.8],
  "chunk_arrival_offsets_ms": null
}
```

An error is structured:

```json
{
  "type": "timeout",
  "message": "Request exceeded 7200 seconds"
}
```

Current error categories are `connection`, `timeout`, `http_error` for every
non-200 response, `truncated_stream`, and `invalid_json`. A completed 200 stream
with missing usage or no observable output remains successful, with the affected
token, TTFT, or TPOT fields set to `null`.

## 9. Concurrency summary schema

Each concurrency level produces one aggregate summary:

```json
{
  "concurrency": 32,
  "requested_requests": 1000,
  "completed_requests": 1000,
  "successful_requests": 998,
  "failed_requests": 2,
  "timed_out_requests": 1,
  "success_rate": 0.998,
  "min_success_rate": 1.0,
  "valid": false,
  "benchmark_duration_s": 41.2,
  "throughput": {
    "requests_per_second": 24.22,
    "input_tokens_per_second": 6200.1,
    "output_tokens_per_second": 3100.4,
    "total_tokens_per_second": 9300.5,
    "token_counts_complete": true
  },
  "input_tokens": {},
  "output_tokens": {},
  "e2e_latency_ms": {},
  "ttft_ms": {},
  "tpot_ms": {},
  "inter_chunk_latency_ms": {},
  "transport_retries": 0,
  "routing": {
    "policy": "round_robin",
    "dispatch_count": {"endpoint-0": 500, "endpoint-1": 500}
  },
  "errors": {"timeout": 1, "http_error": 1},
  "server_metrics": {
    "available": true,
    "url": "http://metrics-host/metrics",
    "url_source": "explicit",
    "accepted_tokens": 150000,
    "draft_tokens": 200000,
    "acceptance_rate": 0.75,
    "accepted_tokens_per_position": {}
  }
}
```

All request distributions use the combined request sample set from the endpoint
pool. The implementation must not average endpoint-specific percentiles.

## 10. Run result schema

`run.json` is shaped as:

```json
{
  "run": {
    "label": "benchmark",
    "started_at": "RFC-3339 timestamp",
    "package_version": "0.2.0",
    "schema_version": "1.0",
    "metric_semantics_version": "1.0",
    "cache_mode": "isolated_miss",
    "text_strategy": "system_prefix",
    "media_strategy": "none|vllm_uuid|sglang_unique_content",
    "configuration": {},
    "dataset": {},
    "environment": {}
  },
  "valid": true,
  "preflight": null,
  "cache_resets": {},
  "warnings": []
}
```

The three cache-strategy fields are present only in isolated mode. Its
`cache_resets` lists are empty.

Secrets and authorization header values are omitted or redacted.

## 11. Output files

One run directory contains:

```text
run.json              Effective redacted configuration and run metadata
summary.json          Structured summaries for all concurrency levels
summary.csv           One row per concurrency level
endpoint_summary.json Per-concurrency, per-endpoint diagnostic summaries
endpoint_summary.csv  One row per concurrency level and endpoint
requests.jsonl        Request-level observations
server_metrics.json   Normalized server counter deltas
cache_resets.json     Startup and per-level pre/post reset audit records
preflight.json        Written when tokenize preflight is enabled and succeeds
```

The terminal table displays endpoint-pool-wide values:

```text
concurrency, successful_requests, requested_requests, requests_per_second,
input_tokens_per_second, output_tokens_per_second, total_tokens_per_second,
e2e_latency_ms_p95, ttft_ms_p95, tpot_ms_p95,
inter_chunk_latency_ms_p95, acceptance_rate
```

When metrics collection is disabled, `acceptance_rate` is displayed as `-`.

## 12. Deferred contracts

The following are intentionally deferred:

- open-loop request-rate scheduling;
- goodput and SLO thresholds;
- strict inter-token latency;
- non-streaming endpoints;
- weighted or least-inflight routing;
- generic gauge processing;
- GPU utilization, memory, power, and NVML metrics;
- distributed load-generator coordination.
