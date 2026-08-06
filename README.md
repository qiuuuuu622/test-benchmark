# endpoint-benchmark：从零开始压测 vLLM Endpoint

`endpoint-benchmark` 是一个面向 OpenAI 兼容流式接口的轻量压测工具。它不把目标抽象成“模型”，而是抽象成一个或多个 HTTP endpoint；多个 endpoint 会按照 round-robin 分发请求，并在同一个测量池中统计 TTFT、TPOT、E2E 延迟和 token 吞吐。

运行时不依赖 PyTorch、CUDA 或 NVIDIA Python 包。只有启动 vLLM 服务的环境需要安装 vLLM 及其 GPU 依赖，压测客户端可以放在另一台机器或单独容器中。

下面以单张 NVIDIA GPU、`Qwen/Qwen3.5-4B` 和本地 `8000` 端口为例，从模型下载开始完成一次压测。

## 1. 准备环境

建议使用 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。确认机器能看到 GPU：

```bash
nvidia-smi
uv --version
```

创建两个相互隔离的虚拟环境：一个运行 vLLM 服务，一个运行压测客户端。

```bash
mkdir -p ~/qwen35-benchmark
cd ~/qwen35-benchmark

uv venv --python 3.12 .venv-vllm
uv venv --python 3.12 .venv-benchmark
```

安装 vLLM。该环境会包含 PyTorch 和 CUDA 相关 Python 依赖：

```bash
uv pip install --python .venv-vllm/bin/python vllm --torch-backend=auto
```

在本项目根目录安装 `endpoint-benchmark`。它本身没有 Torch/CUDA 运行时依赖：

```bash
cd /path/to/test-benchmark
uv pip install --python ~/qwen35-benchmark/.venv-benchmark/bin/python -e .
```

也可以安装已经构建好的 wheel：

```bash
uv pip install \
  --python ~/qwen35-benchmark/.venv-benchmark/bin/python \
  --no-deps \
  dist/endpoint_benchmark-0.1.0-py3-none-any.whl
```

## 2. 下载 Qwen3.5-4B

安装 Hugging Face CLI：

```bash
uv tool install huggingface_hub
```

下载模型到独立目录：

```bash
mkdir -p ~/qwen35-benchmark/models

hf download Qwen/Qwen3.5-4B \
  --local-dir ~/qwen35-benchmark/models/Qwen3.5-4B
```

模型约 9 GB，请预留足够磁盘空间。如果 Hugging Face 要求登录，先执行：

```bash
hf auth login
```

## 3. 在单张 GPU 上启动 vLLM Server

下面固定使用物理 GPU 0，并将服务监听在 `8000` 端口：

```bash
cd ~/qwen35-benchmark

CUDA_VISIBLE_DEVICES=0 \
VLLM_SERVER_DEV_MODE=1 \
.venv-vllm/bin/vllm serve ./models/Qwen3.5-4B \
  --host 0.0.0.0 \
  --port 8000 \
  --served-model-name qwen35-4b \
  --tensor-parallel-size 1 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.70 \
  --enable-prefix-caching
```

参数说明：

- `CUDA_VISIBLE_DEVICES=0`：只让该服务看到一张 GPU。
- `--served-model-name qwen35-4b`：客户端请求体中的 `model` 必须使用这个名字。
- `--tensor-parallel-size 1`：单卡运行，不做张量并行。
- `--max-model-len 32768`：输入 token 与最大输出 token 之和不能超过 32K。
- `--gpu-memory-utilization 0.70`：最多使用约 70% GPU 显存，可根据机器上的其他任务调整。
- `--enable-prefix-caching`：启用 vLLM prefix cache。
- `VLLM_SERVER_DEV_MODE=1`：启用清理 prefix cache 所需的开发管理接口。该接口不应暴露在生产网络中。

保持该终端运行，另外打开一个终端检查服务：

```bash
curl -fsS http://127.0.0.1:8000/v1/models
```

再检查 prefix-cache 管理接口：

```bash
curl -fsS -X POST \
  'http://127.0.0.1:8000/reset_prefix_cache?reset_running_requests=false&reset_external=false'
```

正常响应应包含：

```json
{"success":true}
```

如果返回 404，请确认启动 vLLM 时设置了 `VLLM_SERVER_DEV_MODE=1`。`endpoint-benchmark` 默认会在每个并发档位测量前后清理 prefix cache，并在正式压测前检查该接口。

## 4. 准备 JSONL 测试数据

数据集必须是 JSONL：每一行是一个独立 JSON 对象，至少包含非空的 `messages`。推荐格式如下：

```json
{"id":"request-001","messages":[{"role":"user","content":"请用三句话介绍北京。"}],"request":{"max_output_tokens":128}}
{"id":"request-002","messages":[{"role":"user","content":"解释什么是大语言模型的首 token 延迟。"}],"request":{"max_output_tokens":128}}
```

将内容保存为：

```text
~/qwen35-benchmark/requests.jsonl
```

工具也兼容顶层的 `target_tokens`、`max_tokens` 和 `max_completion_tokens`，它们会被规范化为 `request.max_output_tokens`。如果命令行指定 `--max-output-tokens-cap 128`，数据集中更大的输出长度会被截到 128。

开始前要确认：

```text
实际输入 token 数 + 最大输出 token 数 <= vLLM 的 --max-model-len
```

否则该请求会收到 HTTP 400，不会进入成功请求的延迟分位数统计。

## 5. 执行第一次压测

进入工作目录并执行：

```bash
cd ~/qwen35-benchmark

.venv-benchmark/bin/endpoint-benchmark run \
  --endpoint http://127.0.0.1:8000/v1/chat/completions \
  --model qwen35-4b \
  --dataset ./requests.jsonl \
  --concurrency 1 4 8 16 \
  --num-prompts 200 \
  --warmup-prompts 4 \
  --max-output-tokens-cap 128 \
  --ignore-eos \
  --connection-mode new \
  --no-server-metrics \
  --output-dir ./results \
  --label qwen35-4b-single-gpu
```

主要参数：

- `--endpoint`：完整的 OpenAI Chat Completions URL。可以重复指定多个 endpoint。
- `--concurrency 1 4 8 16`：依次测试四个总并发档位；不是每个 endpoint 各自的并发。
- `--num-prompts 200`：每个并发档位最多使用 200 条数据。
- `--warmup-prompts 4`：正式计时前发送 4 条预热请求。
- `--max-output-tokens-cap 128`：限制单条请求最多生成 128 tokens，适合先做烟测。
- `--ignore-eos`：忽略模型提前输出 EOS，尽量生成到设定上限，使吞吐对比更稳定。
- `--connection-mode new`：每个请求建立新连接，适合当前版本的可靠性验证。
- `--no-server-metrics`：不读取 vLLM `/metrics`，但不会关闭客户端 TTFT、TPOT、E2E、request/s 或 token/s 统计。

输出目录会自动带时间戳，例如：

```text
results/qwen35-4b-single-gpu_20260806_120000/
```

其中常用文件包括：

- `summary.csv`：每个并发档位一行的汇总指标。
- `summary.json`：JSON 格式的汇总结果。
- `requests.jsonl`：每条请求的成功状态、endpoint、TTFT、TPOT 和 E2E 等原始结果。
- `cache_resets.json`：每次 prefix-cache 清理的审计记录。
- `run.json`：完整运行配置和数据集告警。

## 6. 多个 Endpoint 的 round-robin 示例

如果有四个副本，只需重复传入 `--endpoint`：

```bash
.venv-benchmark/bin/endpoint-benchmark run \
  --endpoint http://host:8104/v1/chat/completions \
  --endpoint http://host:8105/v1/chat/completions \
  --endpoint http://host:8106/v1/chat/completions \
  --endpoint http://host:8107/v1/chat/completions \
  --routing round_robin \
  --model qwen35-4b \
  --dataset ./requests.jsonl \
  --concurrency 4 8 16 \
  --num-prompts 200 \
  --warmup-prompts 4 \
  --max-output-tokens-cap 128 \
  --ignore-eos \
  --connection-mode new \
  --no-server-metrics \
  --output-dir ./results \
  --label qwen35-4b-four-endpoints
```

所有请求按 round-robin 分发，但会进入同一个测量池。因此 `ttft_ms_p95` 是四个副本所有成功请求合并后的整体 P95，不是第一个副本的 P95，也不是四个副本 P95 的平均数。

`--no-server-metrics` 只关闭服务端 Prometheus 指标采集。四副本池级的 TTFT、TPOT、E2E、request/s 和 token/s 仍然由客户端统一计算。只有在已有一个能够代表整个副本池的统一 metrics 入口时，才应改用：

```bash
--metrics-url http://metrics-aggregator:PORT/metrics
```

不要把某一个副本的 `/metrics` 当成整个池的服务端指标。

## 7. 核心指标怎么理解

### TTFT：Time to First Token

TTFT 是从客户端开始发送请求，到客户端收到第一个有效输出 token 的时间：

```text
TTFT = 首个有效输出到达时间 - 请求开始时间
```

它包含请求传输、服务端排队、prompt 预填充和首 token 生成等时间。TTFT 越低，用户越快看到回答开始出现。长输入或服务端排队通常会显著拉高 TTFT。

### TPOT：Time per Output Token

TPOT 表示首 token 之后，平均生成一个后续输出 token 所需的时间：

```text
TPOT = (请求结束时间 - 首个输出时间) / (输出 token 数 - 1)
```

单位是 `ms/token`，越低越好。它反映持续解码速度，但它是单个请求解码阶段的平均值，不等于相邻 token 间隔的逐 token 分布。只有输出 token 数至少为 2 时，该请求才有有效 TPOT。

### ITL：Inter-Token Latency

ITL 是流式输出中，相邻两个有效输出 token 或 chunk 的到达时间差：

```text
ITL[i] = 第 i 个输出到达时间 - 第 i-1 个输出到达时间
```

ITL 描述输出是否平滑。平均 TPOT 可能很好，但如果少数 token 停顿很久，ITL P95/P99 会暴露这种卡顿。

当前版本默认不保存每个 chunk 的到达时间。加入下面的参数后，原始到达偏移会写入 `requests.jsonl` 的 `chunk_arrival_offsets_ms`：

```bash
--record-chunk-timestamps
```

需要注意：SSE chunk 不一定严格等于一个 token。当前汇总 CSV 提供 TPOT，但还没有直接提供 ITL mean/P95/P99；ITL 聚合需要根据原始 chunk 时间进一步计算。

### E2E Latency：完整请求延迟

E2E 是从请求开始到完整流结束的时间：

```text
E2E = 请求结束时间 - 请求开始时间
```

它最接近用户等待完整答案的总时长，通常同时受到输入长度、输出长度、排队和解码速度影响。

### Request Throughput

`requests_per_second` 表示整个 endpoint pool 每秒完成的成功请求数。它是池级吞吐，不是各副本 request/s 的平均值。

### Token Throughput

汇总 CSV 提供三类池级吞吐：

- `input_tokens_per_second`：所有成功请求的输入 tokens / 该档位统一测量时长。
- `output_tokens_per_second`：所有成功请求的输出 tokens / 该档位统一测量时长。
- `total_tokens_per_second`：输入与输出 tokens 之和 / 该档位统一测量时长。

不同工具可能只把 `output_tokens_per_second` 称为 token/s。对比结果时，应先确认双方使用的是输入、输出还是总 token 吞吐。

### Mean、P95 和 P99

- `mean`：所有有效成功请求的算术平均值。
- `P95`：约 95% 的有效样本不超过该值。
- `P99`：约 99% 的有效样本不超过该值，更容易反映长尾卡顿。

延迟分位数只基于成功且拥有对应测量值的请求。因此看 P95/P99 时，必须同时检查 `successful_requests`、`requested_requests` 和错误明细，不能用低成功率换取看似更好的延迟。

## 8. 如何选择合理并发

逐档增加并发，重点观察：

1. `output_tokens_per_second` 是否继续增长。
2. TTFT P95/P99 是否突然上升。
3. E2E P95/P99 是否出现明显长尾。
4. 成功请求数是否等于请求总数。

如果并发继续升高，但 token/s 不再增长甚至下降，同时 TTFT 急剧增加，说明 endpoint pool 已进入排队或过载区间。通常应选择吞吐接近峰值、但 TTFT 和 E2E 长尾仍可接受的档位。

## 9. 常见问题

### 启动检查提示缺少 reset_prefix_cache

确认 vLLM 启动命令包含：

```bash
VLLM_SERVER_DEV_MODE=1
```

如果明确不需要清理 prefix cache，可以传 `--no-reset-prefix-cache`，但不同并发档位可能受到历史 cache 状态影响。

### 请求返回 maximum context length

减小输入长度或 `--max-output-tokens-cap`，或者在显存允许时提高 vLLM 的 `--max-model-len`。

### 为什么 `acceptance_rate` 是空的

`acceptance_rate` 属于可选的服务端 speculative decoding 指标。使用 `--no-server-metrics`，或服务端没有对应 counter 时，它会为空；这不代表客户端请求失败。客户端成功率应看 `successful_requests / requested_requests`。

### 架构和接口契约

更完整的设计说明见：

- `vllm-benchmark-architecture.zh-CN.md`
- `vllm-benchmark-interface-contract.zh-CN.md`
