# 轻量级 Endpoint 压测工具输入输出接口契约

## 1. 契约状态

本文定义 v0.2 输入输出契约。Benchmark 的目标是逻辑 Target 或临时 endpoint pool，模型名只是 workload 请求字段。

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
    target: TargetConfig | None
    output: OutputConfig
```

子配置职责：

- `EndpointPoolConfig`：endpoints、routing、timeout、连接复用、认证和 headers；
- `WorkloadConfig`：dataset、model、生成参数和 `extra_body`；
- `LoadConfig`：并发档位、请求数、shuffle、seed 和 warmup；
- `MeasurementConfig`：默认从第一个 endpoint 推导的 metrics URL、关闭开关和可选 chunk 时间戳；
- `TargetConfig`：推理 endpoints、headers、direct/Dynamo cache sources 和统一 clear deadline；
- `OutputConfig`：输出根目录、label、可选 run name 和覆盖策略。

精确类名可在实现前调整，但职责边界属于契约。

## 3. CLI 契约

```bash
endpoint-benchmark run [OPTIONS]
```

### 3.1 Endpoint Pool

```text
--endpoint URL                与 --target 二选一；可重复
--target NAME                 与 --endpoint 二选一
--targets-file PATH           Target TOML；默认 XDG 配置路径
--routing round-robin         默认 round-robin
--timeout SECONDS             默认 7200
--connection-mode reuse|new   默认 reuse
--api-key-env NAME            可选，默认不启用鉴权
--header KEY=VALUE            可选，可重复
```

每个 endpoint 都是完整 URL，例如：

```text
http://host-a:8000/v1/chat/completions
```

endpoint 列表不能为空。v0.2 假设所有 endpoint 提供等价的 OpenAI-compatible 行为。Target 模式不允许用 `--header` 或 `--api-key-env` 覆盖 inference headers。

### 3.2 Workload

```text
--dataset PATH                    必填 JSONL 路径
--model NAME                      默认 payload 字段
--temperature FLOAT               可选覆盖
--max-output-tokens N             可选覆盖
--max-output-tokens-cap N         可选安全上限
--ignore-eos                      可选 vLLM 扩展
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
--shuffle                     默认 True
--seed N                      可选随机种子
--warmup-prompts N            默认 5
```

v0.2 使用 closed-loop。`concurrency` 是 endpoint pool 的总在途请求数，不是每个 endpoint 的并发数。不同并发档位顺序执行。

Warmup 不计入正式请求数、延迟、吞吐和 server metrics delta。

### 3.4 Measurement

```text
--metrics-url URL             可选显式覆盖
--no-server-metrics           关闭服务端 metrics
--record-chunk-timestamps     默认 false
```

所有请求观测直接进入同一个 endpoint-pool measurement pool。默认从第一个请求 endpoint 的 origin 推导 `/metrics`；也可以显式指定一个逻辑服务池指标 URL。Benchmark 不合并各机器独立的 Prometheus endpoint。

### 3.5 Runtime Cache 生命周期

```text
--reset-prefix-cache                 默认 true
--no-reset-prefix-cache              显式关闭整套清理生命周期
--prefix-cache-reset-timeout SEC     默认 60
--prefix-cache-reset-interval SEC    默认 0.5
--reset-external-prefix-cache        默认 false
```

首次 warmup 前，对全部唯一 cache instance 执行 `startup_check`。每个并发档位 warmup 后执行 pre-clear；全部实例成功后才采集 metrics-before 并开始正式计时。请求完成并采集 metrics-after 后执行 post-clear，成功后才能进入下一档；请求阶段异常时执行 post-failure clear。clear、probe 和 retry 均不计入 benchmark duration 或请求延迟。

direct source 依次用只读 `/version`、`/server_info` 识别 vLLM/SGLang；Dynamo 1.3.0 legacy source 对每个 component discovery 一次并逐 instance ID 调用 `clear_kv_blocks`。任一未知或失败实例都会 fail closed。Target TOML 可配置 `clear_timeout_s` 与 `clear_retry_interval_s`，显式 CLI 值优先；临时 `--endpoint` 模式使用相同内部 Target 路径。vLLM 必须通过 `VLLM_SERVER_DEV_MODE=1` 开启开发管理接口；只有传 `--reset-external-prefix-cache` 时才使用 `reset_external=true`。

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

调度前统一转换为标准字段，并在 run metadata 中记录格式和转换 warning。

## 5. 请求 Payload 契约

客户端强制设置：

```json
{
  "model": "served-model",
  "messages": [],
  "stream": true,
  "stream_options": {"include_usage": true}
}
```

客户端可以加入经过校验的 temperature、输出 token 上限、tools、tool choice 和后端扩展字段。

## 6. 指标定义

### 6.1 正式测量区间

`benchmark_duration_s` 从第一条正式请求开始，到最后一条正式请求结束。不包括 warmup、metrics 获取、数据集加载和结果写入。

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

成功请求缺少准确 usage 时，token throughput 为 `null` 或明确标记为 incomplete，不能用 chunk 数代替 token 数。

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
  "id": "request-001",
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
  "finish_reason": "length"
}
```

错误使用结构化形式：

```json
{
  "type": "timeout",
  "message": "Request exceeded 7200 seconds"
}
```

错误类别至少包括 connection、timeout、HTTP 4xx、HTTP 5xx、截断流、非法 SSE、非法 JSON、缺少 usage 和空输出。

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
  "routing": {
    "policy": "round_robin",
    "dispatch_count": {"endpoint-0": 500, "endpoint-1": 500}
  },
  "server_metrics": {
    "available": true,
    "url": "http://metrics-host/metrics",
    "accepted_tokens": 150000,
    "draft_tokens": 200000,
    "acceptance_rate": 0.75,
    "accepted_tokens_per_position": {}
  }
}
```

所有请求分布使用 endpoint pool 中的合并样本，禁止平均 endpoint 各自的 percentile。

## 10. Run Result Schema

顶层 JSON：

```json
{
  "schema_version": "1.0",
  "metric_semantics_version": "1.0",
  "package_version": "0.2.0",
  "run": {
    "label": "benchmark",
    "started_at": "RFC-3339 timestamp",
    "configuration": {},
    "dataset": {},
    "environment": {}
  },
  "summaries": [],
  "warnings": []
}
```

密钥和 authorization header 必须省略或脱敏。

## 11. 输出文件

每个 run 目录包含：

```text
run.json              脱敏后的有效配置和 run 元数据
summary.json          所有并发档位的结构化汇总
summary.csv           每个并发档位一行
requests.jsonl        请求级观测
server_metrics.json   原始 before/after 快照和规范化 counter delta
cache_resets.json     startup check 与每个档位 pre/post clear 的逐实例审计记录
```

终端表格展示 endpoint pool 总体结果：

```text
concurrency, successful_requests, requested_requests, requests_per_second,
input_tokens_per_second, output_tokens_per_second, total_tokens_per_second,
e2e_latency_ms_mean, e2e_latency_ms_p95, e2e_latency_ms_p99,
ttft_ms_mean, ttft_ms_p95, ttft_ms_p99,
tpot_ms_mean, tpot_ms_p95, tpot_ms_p99, acceptance_rate
```

未启用 server metrics 时隐藏相应列。

## 12. v0.2 暂缓项

- open-loop request-rate 调度；
- goodput 和 SLO；
- 严格 ITL；
- 非流式 endpoint；
- weighted、least-inflight 路由；
- 通用 gauge 处理；
- GPU utilization、显存、功耗和 NVML 指标；
- 分布式压测发生器协调。
