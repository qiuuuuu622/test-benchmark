# 轻量级 Endpoint 压测工具输入输出接口契约

## 1. 契约状态

本文定义 package 0.2.0 的输入输出契约。Benchmark 的目标是 endpoint pool，模型名只是 workload 请求字段。

版本字段相互独立：

```json
{
  "schema_version": "1.0",
  "metric_semantics_version": "1.0",
  "package_version": "0.2.0"
}
```

## 2. Python API

最小公共 API 为同步接口：

```python
from endpoint_benchmark import BenchmarkConfig, BenchmarkResult, run_benchmark

result: BenchmarkResult = run_benchmark(BenchmarkConfig(...))
```

配置使用组合结构：

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

子配置职责：

- `EndpointPoolConfig`：endpoints、routing、timeout、连接复用、认证和 headers；
- `WorkloadConfig`：dataset、model、生成参数和 `extra_body`；
- `LoadConfig`：并发档位、请求数、shuffle、seed 和 warmup；
- `MeasurementConfig`：默认从第一个 endpoint 推导的 metrics URL、关闭开关和可选 chunk 时间戳；
- `PreflightConfig`：可选 tokenize 检查和上下文长度上限；
- `ValidityConfig`：最低成功率和无效结果的 CLI 退出策略；
- `PrefixCacheResetConfig`：简单 direct vLLM reset lifecycle；
- `OutputConfig`：输出根目录、label、可选 run name 和覆盖策略。

这些类名和职责边界属于当前 package 0.2.0 契约。

## 3. CLI 契约

```bash
endpoint-benchmark run [OPTIONS]
```

### 3.1 Endpoint Pool

```text
--endpoint URL                必填，可重复
--routing round_robin         默认 round_robin
--timeout SECONDS             默认 720
--connection-mode reuse|new   默认 reuse
--api-key-env NAME            可选，默认不启用鉴权
--header KEY=VALUE            可选，可重复
```

每个 endpoint 都是完整 URL，例如：

```text
http://host-a:8000/v1/chat/completions
```

endpoint 列表不能为空。所有 endpoint 必须提供等价的 OpenAI-compatible 行为。

### 3.2 Workload

```text
--dataset PATH                    必填 JSONL 路径
--model NAME                      默认 payload 字段
--temperature FLOAT               可选覆盖
--max-output-tokens N             可选覆盖
--max-output-tokens-cap N         可选安全上限
--ignore-eos / --no-ignore-eos    可选 vLLM 扩展覆盖
--extra-body-json PATH            可选扩展参数文件
```

模型名只写入请求 payload，不用于加载或检查模型。

字段优先级：

```text
CLI 显式覆盖 > 数据集 request 字段 > 省略并使用服务端默认值
```

`max-output-tokens-cap` 在优先级解析后执行。`extra_body` 禁止覆盖：

```text
messages, model, stream, stream_options
```

### 3.3 Load

```text
--concurrency N [N ...]       必填，正整数档位
--num-prompts N               可选请求数上限
--shuffle                     默认 false
--seed N                      可选随机种子
--warmup-prompts N            默认 0
```

当前实现使用 closed-loop。`concurrency` 是 endpoint pool 的总在途请求数，不是每个 endpoint 的并发数。不同并发档位顺序执行。

Warmup 不计入正式请求数、延迟、吞吐和 server metrics delta。

### 3.4 Measurement

```text
--metrics-url URL             可选显式覆盖
--no-server-metrics           关闭服务端 metrics
--record-chunk-timestamps     默认 false
--preflight-tokenize          默认 false
--max-model-len N             可选；同时开启 tokenize preflight
--min-success-rate RATE       默认 1.0
--fail-on-request-error       默认 true，可用 --no-fail-on-request-error 覆盖
```

所有请求观测直接进入同一个 endpoint-pool measurement pool。默认从第一个请求 endpoint 的 origin 推导 `/metrics`；也可以显式指定一个逻辑服务池指标 URL。Benchmark 不合并各机器独立的 Prometheus endpoint。`max-model-len` 只用于 preflight 检查，不修改服务端配置；最低成功率逐并发档位判断，`fail-on-request-error` 只控制无效 run 是否返回退出码 3。

### 3.5 Prefix Cache 生命周期

```text
--reset-prefix-cache                 默认 true
--no-reset-prefix-cache              显式关闭
--prefix-cache-reset-timeout SEC     默认 60
--prefix-cache-reset-interval SEC    默认 0.5
--reset-external-prefix-cache        默认 false
--isolated-miss                      为每个逻辑请求生成独立 cache identity
```

每个并发档位在 warmup 后执行 pre-reset；所有唯一 engine origin 返回 `success=true` 后才开始正式计时。正式请求全部结束后，先采集 metrics-after，再执行 post-reset；全部成功后才允许进入下一档。Reset 耗时不计入 benchmark duration 或延迟。vLLM 必须通过 `VLLM_SERVER_DEV_MODE=1` 开启开发管理接口。

任何 warmup 之前，runner 会先对全部唯一 engine origin 执行一次 `startup_check` reset。若返回 HTTP 404，立即终止并明确提示需要设置 `VLLM_SERVER_DEV_MODE=1`。

`--isolated-miss` 跳过 startup、pre、post 和失败清理，并拒绝显式 reset、external reset、reset timeout 或 reset interval 参数。A/B 的 cache-enabled baseline 使用 `--no-reset-prefix-cache`。Dynamo 只作为普通 HTTP endpoint 使用；客户端不执行 runtime discovery、backend probe 或 Dynamo cache clear。

### 3.6 Output

```text
--output-dir PATH             结果根目录，默认 benchmark_results
--label NAME                  默认 benchmark
--run-name NAME               可选固定 run 目录名
--overwrite                   默认 false
```

未指定 `--run-name` 时，自动写入 `<output-dir>/<label>_<YYYYMMDD_HHMMSS>/` 唯一目录。显式指定 run name 且目标非空时，必须使用 `--overwrite`。

### 3.7 示例

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

## 4. 数据集契约

标准格式为 UTF-8 JSONL，每行一个请求对象：

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

必填字段：

- `messages`：非空 OpenAI-compatible message 数组。

可选字段：

- `id`：稳定请求 ID，缺失时根据行号生成；
- `request`：单请求生成参数；
- `metadata`：不发送给服务端的分析元数据。

工具请求可以在 `request` 中包含 `tools` 和 `tool_choice`。

兼容 adapter 可以识别：

```text
target_tokens
max_tokens
max_completion_tokens
prompt_tokens_est
```

调度前统一转换为标准字段，并在 `run.json` 顶层 `warnings` 中记录发生过的转换。

### 4.1 Isolated miss 多模态约束

isolated 模式为每个 warmup 和正式逻辑请求生成一个随机 16 位十六进制 identity。文本在最早可控的 system 或 user 文本前增加 `[rid:<identity>]`。多模态数据直接使用标准 `messages` content part，同一数据集只能选择一种策略：

- vLLM 图片 part 在顶层 `uuid` 中放置 `{isolated_miss_id}`，URL 不变；
- SGLang 图片 URL 中放置 `{isolated_miss_id}`，且不提供 `uuid`。

每张图片必须且只能声明一种占位符策略。混用策略和不支持媒体会在任何网络 I/O 前失败。客户端不发送 `cache_salt`。

## 5. 请求 Payload 契约

客户端强制设置 `messages`、`stream` 和 `stream_options`；`model` 只在 CLI 或请求样例提供时加入：

```json
{
  "model": "served-model",
  "messages": [],
  "stream": true,
  "stream_options": {"include_usage": true}
}
```

客户端可以加入经过校验的 temperature、输出 token 上限、tools、tool choice 和后端扩展字段。isolated 模式只改写 `messages` 的深拷贝；收到响应前的安全重试复用同一份编码请求体。

## 6. 指标定义

### 6.1 正式测量区间

`benchmark_duration_s` 从正式 workload 提交前开始，到全部正式请求结果收集完成。不包括 warmup、cache reset、metrics 获取、数据集加载和结果写入，但包含少量客户端调度开销。

### 6.2 E2E Latency

```text
e2e_latency_ms = stream_end_ms - request_start_ms
```

### 6.3 TTFT

```text
ttft_ms = first_observable_output_ms - request_start_ms
```

首个可观察输出是第一个非空 `content` 或 `tool_calls` delta。工具调用场景下其严格含义是 time to first observable output，但对外保留常用名称 TTFT。

### 6.4 TPOT

成功请求具有准确 usage 且输出 token 大于 1 时：

```text
tpot_ms = (stream_end_ms - first_observable_output_ms)
          / (completion_tokens - 1)
```

缺少必要观测时 TPOT 为 `null`。

### 6.5 Throughput

所有吞吐都是 endpoint pool 总体吞吐：

```text
request_throughput_rps = successful_requests / benchmark_duration_s

input_token_throughput_tps =
    sum(input_tokens) / benchmark_duration_s

output_token_throughput_tps =
    sum(output_tokens) / benchmark_duration_s

total_token_throughput_tps =
    (sum(input_tokens) + sum(output_tokens)) / benchmark_duration_s
```

任一成功请求缺少输入或输出 token usage 时，三种 token throughput 都为 `null`，且 `token_counts_complete=false`；不能用 chunk 数代替 token 数。

系统总体 output throughput 和单请求生成速度不同，不能将 `1000 / TPOT` 当作 endpoint pool throughput。

### 6.6 分布字段

每项延迟或 token 长度分布使用统一结构：

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

没有样本时 `samples` 为零，其他统计为 `null`。

## 7. Server Metrics

每个正式并发档位执行：

```text
读取 metrics before
执行 workload
读取 metrics after
计算 counter delta
使用 delta 计算 rate
```

配置 URL 是一个逻辑 metrics source；如果需要 endpoint pool 总体服务端指标，它应当已经代表整个服务池。合并各机器的独立 metrics 不属于 Benchmark 客户端职责。

Speculative decoding：

```text
accepted_tokens = accepted-token counter delta
draft_tokens = draft-token counter delta
acceptance_rate = accepted_tokens / draft_tokens
```

如果配置 URL 无法完成 before/after 抓取，server metrics 标记为 unavailable，相关 rate 为 `null`。这不影响 measurement pool 上计算的客户端请求计时和 throughput。

## 8. 请求级结果 Schema

每条正式请求产生一个 JSONL record：

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

错误使用结构化形式：

```json
{
  "type": "timeout",
  "message": "Request exceeded 7200 seconds"
}
```

当前错误类别包括 `connection`、`timeout`、非 200 响应统一使用的 `http_error`、`truncated_stream` 和 `invalid_json`。成功的 200 流缺少 usage 或可观察输出时，相应 token、TTFT、TPOT 字段为 `null`，但不会仅因此改判失败。

## 9. 并发档位 Summary Schema

每个并发档位产生一份总体 summary：

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

所有请求分布使用 endpoint pool 中的合并样本，禁止平均 endpoint 各自的 percentile。

## 10. Run Result Schema

`run.json`：

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

三个 cache strategy 字段仅在 isolated 模式出现；该模式的 `cache_resets` 列表为空。

密钥和 authorization header 必须省略或脱敏。

## 11. 输出文件

每个 run 目录包含：

```text
run.json              脱敏后的有效配置和 run 元数据
summary.json          所有并发档位的结构化汇总
summary.csv           每个并发档位一行
endpoint_summary.json 按并发档位和 endpoint 拆分的诊断汇总
endpoint_summary.csv  每个并发档位、每个 endpoint 一行
requests.jsonl        请求级观测
server_metrics.json   规范化后的 server counter delta
cache_resets.json     startup 与每个档位 pre/post reset 审计记录
preflight.json        启用且成功完成 tokenize preflight 时生成
```

终端表格展示 endpoint pool 总体结果：

```text
concurrency, successful_requests, requested_requests, requests_per_second,
input_tokens_per_second, output_tokens_per_second, total_tokens_per_second,
e2e_latency_ms_p95, ttft_ms_p95, tpot_ms_p95,
inter_chunk_latency_ms_p95, acceptance_rate
```

未启用 server metrics 时 `acceptance_rate` 显示为 `-`。

## 12. 暂缓项

- open-loop request-rate 调度；
- goodput 和 SLO；
- 严格 ITL；
- 非流式 endpoint；
- weighted、least-inflight 路由；
- 通用 gauge 处理；
- GPU utilization、显存、功耗和 NVML 指标；
- 分布式压测发生器协调。
