# Lightweight Endpoint Benchmark Architecture

## 1. Purpose

This project is a lightweight benchmark client for OpenAI-compatible streaming
HTTP endpoints. Its target is an endpoint or endpoint pool, not a model. A model
name is only a request payload field.

The client must be usable in a fresh inference container without changing its
Torch, CUDA, NVIDIA, or vLLM environment.

## 2. Design principles

### 2.1 Endpoint-oriented design

- A single endpoint is one complete request URL.
- An endpoint pool contains one or more equivalent endpoints.
- A benchmark run sends one workload to one endpoint pool.
- Multiple endpoints use a routing policy; package 0.2.0 supports round-robin.
- `model` belongs to the workload payload, not the target abstraction.
- The client does not load, discover, or inspect models.

### 2.2 Zero runtime dependencies

The installed package has no third-party runtime dependencies and must not
depend on:

- `torch`, `vllm`, `transformers`, or `tokenizers`;
- CUDA or `nvidia-*` Python packages;
- NumPy, pandas, SciPy, or a Prometheus client;
- requests, HTTPX, aiohttp, Typer, or Rich.

The implementation uses Python standard-library facilities such as `argparse`,
`csv`, `dataclasses`, `http.client`, `json`, `math`, `pathlib`, `statistics`,
`threading`, `time`, and `concurrent.futures`.

A release should provide a pure Python `py3-none-any` wheel so installation does
not require compilation or dependency resolution in the benchmark container.

### 2.3 Measurement correctness

- Use `time.perf_counter()` as the monotonic high-resolution clock.
- Record the first observable output while consuming the SSE stream, not after
  buffering the response.
- Exclude warmup, metrics collection, dataset loading, and result serialization
  from the measured benchmark interval.
- Never treat an SSE chunk as a token.
- Use server-reported usage for input and output token counts.
- Return unavailable metrics as `null`; do not replace them with zero or an
  unlabelled estimate.
- Version metric semantics independently from the package version.

### 2.4 Preserve raw observations

Each request produces a structured result. Summaries are derived from those
results rather than being the only retained data. This permits recalculating
percentiles and future SLO metrics without rerunning the benchmark.

Chunk timestamps are optional because they can substantially increase output
size. When enabled, they represent SSE chunk arrival times, not strict token
arrival times.

### 2.5 Calculate once over one measurement pool

- Every endpoint writes request observations into one shared measurement pool.
- Latency percentiles and throughput are calculated once from all request
  samples in that pool. Per-endpoint results are not calculated and recombined.
- Prometheus counters are sampled before and after a complete concurrency run.
- By default, one logical metrics URL is derived as `/metrics` on the first
  endpoint origin. It can be explicitly overridden or disabled, and must expose
  whole-pool counters when pool-wide server metrics are required.
- The benchmark does not combine Prometheus metrics from individual machines.

## 3. System boundaries

The benchmark client owns:

- dataset loading and normalization;
- request construction;
- optional request-level cache-identity isolation;
- load scheduling and endpoint routing;
- HTTP/SSE transport;
- client-observed timing;
- optional server metrics collection and counter-delta calculation;
- the simple direct vLLM prefix-cache reset lifecycle;
- request-level results, summaries, and serialization.

It does not own:

- model loading or tokenizer installation;
- vLLM lifecycle management;
- GPU, CUDA, or NVML discovery;
- runtime discovery, backend probing, or Dynamo cache management;
- load balancer configuration;
- correctness or quality evaluation of generated text.

## 4. Component architecture

```text
CLI
 └── Configuration loader and validator
      └── Benchmark runner
           ├── Dataset loader and normalizer
           ├── Isolated miss validator and message transformer
           ├── Load scheduler
           │    └── Endpoint pool router
           ├── Streaming HTTP client
           │    └── Stream recorder
           ├── Server metrics collector
           └── Result aggregator
                └── JSON / JSONL / CSV writers
```

### 4.1 CLI

The CLI parses arguments and renders concise progress and result tables. It does
not implement benchmark logic. CLI and Python callers use the same configuration
and runner APIs.

### 4.2 Configuration

Configuration is split by responsibility:

- `EndpointPoolConfig`: where requests are sent and how targets are selected;
- `WorkloadConfig`: dataset and default request payload fields;
- `LoadConfig`: concurrency, request count, order, seed, and warmup;
- `MeasurementConfig`: timing details, the derived or explicit metrics URL, and
  its disable switch;
- `PrefixCacheResetConfig`: mandatory-by-default per-level pre/post cache reset;
- `isolated_miss`: request-level identity isolation that disables reset;
- `OutputConfig`: result location and serialization policy.

Frozen dataclasses are preferred so a running benchmark cannot silently mutate
its effective configuration.

### 4.3 Dataset loader

The loader validates JSONL rows and converts them to a canonical `RequestCase`.
Legacy aliases may be accepted by a compatibility adapter, but the rest of the
system sees only canonical fields.

Precedence for request fields is:

```text
explicit CLI override > request-row value > server default
```

A configured output-token cap is always applied as a safety ceiling.

### 4.4 Isolated miss transformer

Before any network I/O, isolated mode validates every case and then rewrites a
deep copy of each logical request. Text receives one random identity marker;
vLLM image UUIDs or SGLang image URLs receive media-specific identities. A
dataset may use only one image strategy. The module does not probe runtimes,
discover Dynamo workers, or send `cache_salt`.

### 4.5 Load scheduler

The current implementation uses closed-loop concurrency: each worker begins its next request after its
previous request completes. A concurrency value is the total concurrency for the
whole endpoint pool, not concurrency per endpoint.

Concurrency levels run sequentially and produce separate summaries. Warmup runs
before formal measurement and is excluded from results.

After warmup, each level resets all unique engine origins before metrics-before
and formal timing. After all requests complete, metrics-after is collected before
a second reset. Every reset must succeed before advancing to the next level.
Before the first warmup, a startup reset verifies that every engine exposes the
development management endpoint and fails closed with a dev-mode diagnostic.
Isolated mode skips every reset phase; `--no-reset-prefix-cache` provides the
cache-enabled baseline for A/B runs.

The configuration model should allow a future open-loop scheduler without
changing endpoint, workload, measurement, or result contracts. Future scheduling
may support request rate, maximum concurrency, and Poisson or constant arrivals.

### 4.6 Endpoint pool router

Package 0.2.0 supports thread-safe round-robin routing. Routing records the number of
requests dispatched to each endpoint for audit purposes, but the default
performance report remains endpoint-pool-wide.

Routing only selects request endpoints. Every request result is added directly
to the same measurement pool, regardless of which endpoint served it.

### 4.7 Streaming HTTP client

The client sends OpenAI-compatible streaming requests and consumes SSE records as
they arrive. Each worker may reuse its own HTTP connection. Connection reuse is
the default; a new-connection mode may be selected when connection setup should
be part of the workload.

The transport reports protocol observations and errors but does not calculate
summary statistics. A stale reused connection may be retried only before a
response exists, using the exact same encoded request body.

### 4.8 Stream recorder

The recorder captures:

- request start;
- first valid `content` or `tool_calls` output arrival;
- stream completion;
- usage information;
- finish reason;
- optional output-chunk arrival offsets.

TTFT, end-to-end latency, and request-level TPOT are derived from these
observations according to the versioned metric definitions.

### 4.9 Server metrics collector

Unless disabled, the collector reads one logical Prometheus metrics URL before
and after each formal concurrency run. It defaults to `/metrics` on the first
endpoint origin and is expected to expose the desired service-pool-wide counters.
It compares the before/after snapshots in memory and persists the normalized
counter deltas.

The collector calculates deltas for known speculative-decoding counters. It does
not combine per-machine sources or blindly transform arbitrary gauges. Cluster
metrics aggregation, when needed, belongs to Prometheus or another service-side
metrics system behind the configured URL.

### 4.10 Result aggregator

The aggregator calculates endpoint-pool-wide counts, throughput, token-length
distributions, latency distributions, and optional server counter deltas. Every
optional distribution includes its sample count.

## 5. Suggested package layout

```text
pyproject.toml
src/
  endpoint_benchmark/
    __init__.py
    __main__.py
    aggregation.py
    cache_control.py
    cli.py
    dataset.py
    isolated_miss.py
    models.py
    output.py
    preflight.py
    routing.py
    runner.py
    server_metrics.py
    transport.py
    version.py
tests/
  test_cli.py
  test_dataset.py
  test_isolated_miss.py
  test_preflight.py
  test_routing.py
  test_server_metrics.py
  test_cache_control.py
  test_aggregation.py
  test_runner.py
  test_transport.py
```

## 6. Package 0.2.0 scope

Package 0.2.0 includes:

- one endpoint pool per run;
- one or more OpenAI-compatible chat-completion URLs;
- round-robin routing;
- streaming SSE responses with usage enabled;
- JSONL datasets;
- closed-loop concurrency levels;
- warmup requests;
- simple direct vLLM prefix-cache reset;
- request-level isolated cache identities for text, vLLM images, and SGLang images;
- zero-runtime-dependency HTTP execution;
- request-level JSONL and aggregate JSON/CSV output;
- endpoint-pool throughput, TTFT, TPOT, and E2E latency;
- speculative-decoding deltas from one derived or explicit metrics URL.

Package 0.2.0 excludes:

- open-loop request-rate scheduling;
- distributed benchmark generators;
- local tokenization;
- strict inter-token latency;
- GPU and power monitoring;
- target files, runtime discovery, backend probes, and Dynamo runtime cache clear;
- model quality evaluation;
- a web UI.

## 7. Testing strategy

Tests use controlled fake transports and clocks where timing behavior matters.
They assert semantics rather than real wall-clock durations.

Required behavioral coverage includes:

- role-only chunks do not trigger TTFT;
- first content or tool-call output triggers TTFT during stream consumption;
- metrics collection and warmup do not enter measured duration;
- TPOT requires accurate completion-token usage and more than one token;
- missing usage does not produce fabricated token throughput;
- round-robin is deterministic and thread-safe;
- total concurrency applies to the endpoint pool;
- all endpoints contribute directly to one request measurement pool;
- the enabled metrics URL is sampled once before and after the workload;
- isolated warmup and measured requests use unique identities and never reset caches;
- stale-connection retries reuse the same encoded request body;
- HTTP, timeout, truncated-stream, and malformed-SSE errors are classified.

## 8. Evolution rules

- Public configuration and result schemas are versioned.
- New optional fields are backward-compatible.
- Metric formula changes require a new metric-semantics version.
- Historical aliases stay in the dataset adapter rather than leaking into core
  models.
- Backend-specific request extensions belong in the isolated message transformer
  or validated `extra_body`, not in the endpoint abstraction.
