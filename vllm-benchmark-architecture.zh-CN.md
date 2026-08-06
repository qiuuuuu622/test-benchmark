# 轻量级 Endpoint 压测工具架构设计

## 1. 项目目标

本项目是面向 OpenAI-compatible 流式 HTTP endpoint 的轻量压测客户端。测试目标是 endpoint 或 endpoint pool，而不是模型；模型名仅是请求 payload 中的一个字段。

客户端必须能在新的推理容器中直接使用，且不改变容器中的 Torch、CUDA、NVIDIA 或 vLLM 环境。

## 2. 设计原则

### 2.1 以 Endpoint 为中心

- 单个 endpoint 是一个完整请求 URL。
- endpoint pool 包含一个或多个功能等价的 endpoint。
- 一次 benchmark run 对一个 endpoint pool 施加一份 workload。
- 多 endpoint 通过路由策略分发；v0.1 支持 round-robin。
- `model` 属于 workload payload，不属于测试目标抽象。
- 客户端不加载、发现或检查模型。

### 2.2 运行时零第三方依赖

安装后的包不能依赖：

- `torch`、`vllm`、`transformers`、`tokenizers`；
- CUDA 或 `nvidia-*` Python 包；
- NumPy、pandas、SciPy、Prometheus client；
- requests、HTTPX、aiohttp、Typer、Rich。

实现仅使用 Python 标准库，例如 `argparse`、`csv`、`dataclasses`、`http.client`、`json`、`math`、`pathlib`、`statistics`、`threading`、`time` 和 `concurrent.futures`。

发布时提供纯 Python 的 `py3-none-any` wheel，使压测容器安装时不需要编译或解析额外依赖。

### 2.3 测量正确性

- 使用 `time.perf_counter()` 单调高精度时钟。
- 消费 SSE 流时实时记录首个可观察输出，不能在缓存完整响应后补记。
- warmup、metrics 抓取、数据集加载和结果写入不计入正式测量区间。
- SSE chunk 不能当作 token。
- 输入、输出 token 数使用服务端返回的 usage。
- 无法测量的指标返回 `null`，不能用零或未标明的估算值代替。
- 指标语义版本与软件包版本分别管理。

### 2.4 保留原始观测

每个请求生成结构化结果，汇总统计从请求级结果派生。这样未来可以重新计算 percentile 和 SLO，无需重新压测。

chunk 时间戳默认不保存，避免结果文件过大。启用后记录的是 SSE chunk 到达时间，不是严格的 token 到达时间。

### 2.5 在统一 Measurement Pool 上一次计算

- 所有 endpoint 的请求观测直接写入同一个 measurement pool。
- 延迟 percentile 和 throughput 基于该 pool 的全部请求样本一次计算，不先计算各 endpoint 指标再合并。
- Prometheus counter 在每个正式并发档位开始前和结束后各抓取一次。
- 默认从第一个 endpoint 的 origin 推导 `/metrics` 作为唯一逻辑 metrics URL；可以显式覆盖或关闭。若需要 pool 总体服务端指标，该 URL 必须提供整个服务池的 counter。
- Benchmark 客户端不合并各机器独立暴露的 Prometheus 指标。

## 3. 系统边界

客户端负责：

- 数据集读取与规范化；
- 请求构造；
- 负载调度和 endpoint 路由；
- HTTP/SSE 传输；
- 客户端计时；
- 可选服务端 metrics 采集和 counter delta 计算；
- 请求级结果、汇总和序列化。

客户端不负责：

- 加载模型或安装 tokenizer；
- 管理 vLLM 生命周期；
- 探测 GPU、CUDA 或 NVML；
- 重置服务端 cache 或改变服务端状态；
- 配置负载均衡器；
- 评价生成内容的正确性或质量。

## 4. 组件架构

```text
CLI
 └── 配置加载与校验
      └── Benchmark Runner
           ├── 数据集加载与规范化
           ├── Load Scheduler
           │    └── Endpoint Pool Router
           ├── Streaming HTTP Client
           │    └── Stream Recorder
           ├── Server Metrics Collector
           └── Result Aggregator
                └── JSON / JSONL / CSV Writer
```

### 4.1 CLI

CLI 只负责解析参数、展示进度和结果表格，不实现压测逻辑。CLI 和 Python 调用方共用同一套配置和 runner API。

### 4.2 配置层

配置按职责拆分：

- `EndpointPoolConfig`：请求发到哪里、如何选择目标；
- `WorkloadConfig`：数据集和默认 payload 字段；
- `LoadConfig`：并发、请求数、顺序、随机种子和 warmup；
- `MeasurementConfig`：计时细节、自动推导或显式 metrics URL 及关闭开关；
- `PrefixCacheResetConfig`：默认启用的每档 pre/post prefix cache reset；
- `OutputConfig`：结果路径和序列化策略。

配置优先使用 frozen dataclass，防止运行期间静默改变有效配置。

### 4.3 数据集加载器

加载器校验 JSONL，并规范化为统一的 `RequestCase`。旧字段由兼容 adapter 处理，核心模块只接触标准字段。

字段优先级：

```text
CLI 显式覆盖 > 数据集单请求字段 > 省略并使用服务端默认值
```

输出 token cap 始终作为最终安全上限。

### 4.4 负载调度器

v0.1 使用 closed-loop concurrency：每个 worker 完成前一个请求后再发下一个请求。并发值表示整个 endpoint pool 的总并发，而不是每个 endpoint 的并发。

不同并发档位顺序执行并分别生成 summary。warmup 在正式测量前运行，不计入结果。

每个档位 warmup 完成后，对全部唯一 engine origin 执行 pre-reset，再采集 metrics-before 并开始正式计时。正式请求全部结束后先采集 metrics-after，再执行 post-reset；所有 reset 成功后才能进入下一档。

第一次 warmup 之前先执行 startup reset，确认全部引擎都暴露开发管理接口；未启用时明确提示 dev mode 并 fail-closed。

配置模型应允许未来加入 open-loop scheduler，而不改变 endpoint、workload、measurement 和 result 契约。未来可支持 request rate、最大并发以及 Poisson/constant 到达分布。

### 4.5 Endpoint Pool Router

v0.1 提供线程安全且确定性的 round-robin 路由。记录每个 endpoint 的分发请求数用于审计，但默认性能报告只展示 endpoint pool 总体结果。

路由只负责选择请求 endpoint。无论请求由哪个 endpoint 服务，其结果都直接进入同一个 measurement pool。

### 4.6 Streaming HTTP Client

客户端发送 OpenAI-compatible 流式请求，并在 SSE record 到达时立即消费。默认每个 worker 复用自己的 HTTP 连接；可选 new-connection 模式用于将建连计入 workload。

传输层只报告协议观测和错误，不负责计算汇总统计。

### 4.7 Stream Recorder

记录：

- 请求开始时间；
- 首个非空 `content` 或 `tool_calls` 到达时间；
- SSE 流结束时间；
- usage；
- finish reason；
- 可选的输出 chunk 相对到达时间。

TTFT、E2E latency 和请求级 TPOT 均从这些观测按照版本化公式派生。

### 4.8 Server Metrics Collector

除非显式关闭，每个正式并发档位开始前和结束后读取一个逻辑 Prometheus metrics URL。默认从第一个 endpoint 的 origin 推导 `/metrics`；该 URL 应提供所需的服务池总体 counter，并保存原始 before/after 快照以便审计。

Collector 计算已知 speculative decoding counter 的 delta，但不合并各机器 source，也不盲目处理任意 gauge。需要集群级指标聚合时，应由配置 URL 背后的 Prometheus 或其他服务端指标系统完成。

### 4.9 Result Aggregator

基于统一 measurement pool 计算总体完成数、吞吐、token 长度分布、延迟分布和可选服务端 counter delta。所有可选分布都必须携带样本数。

## 5. 建议目录结构

```text
pyproject.toml
src/
  endpoint_benchmark/
    __init__.py
    cli.py
    config.py
    dataset.py
    models.py
    routing.py
    scheduler.py
    transport.py
    timing.py
    server_metrics.py
    cache_control.py
    aggregation.py
    output.py
tests/
  test_dataset.py
  test_routing.py
  test_stream_timing.py
  test_server_metrics.py
  test_cache_control.py
  test_aggregation.py
  test_runner.py
```

最终 distribution name 和 import package name 尚待确定。

## 6. v0.1 范围

包含：

- 每次 run 使用一个 endpoint pool；
- 一个或多个 OpenAI-compatible chat-completion URL；
- round-robin 路由；
- 带 usage 的 SSE 流式响应；
- JSONL 数据集；
- closed-loop 并发档位；
- warmup；
- 零运行时依赖 HTTP 客户端；
- 请求级 JSONL 和聚合 JSON/CSV；
- endpoint pool 总体 throughput、TTFT、TPOT、E2E latency；
- 从自动推导或显式指定的逻辑 metrics URL 计算 speculative decoding delta。

不包含：

- open-loop request-rate 调度；
- 分布式压测发生器；
- 本地 tokenization；
- 严格 ITL；
- GPU、功耗监控；
- 自动清理 cache；
- 模型质量评测；
- Web UI。

## 7. 测试策略

计时测试使用可控的 fake transport 和 clock，验证语义而非真实墙钟数值。必须覆盖：

- role-only chunk 不触发 TTFT；
- 首个 content/tool-call 在流消费期间触发 TTFT；
- metrics 和 warmup 不进入测量区间；
- TPOT 要求准确 usage 且输出 token 大于 1；
- 缺少 usage 时不伪造 token throughput；
- round-robin 确定且线程安全；
- 并发是 endpoint pool 总并发；
- 所有 endpoint 的请求结果直接进入同一个 measurement pool；
- 启用的 metrics URL 在 workload 前后各采集一次；
- 正确分类 HTTP、timeout、截断流和非法 SSE 错误。

## 8. 演进规则

- 公共配置与结果 schema 必须版本化。
- 新增可选字段应保持向后兼容。
- 指标公式变化必须提升 metric semantics version。
- 历史字段别名只存在于 dataset adapter。
- 后端专用参数放在 adapter 或经校验的 `extra_body` 中，不能污染 endpoint 抽象。
