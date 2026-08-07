# 统一 Runtime Cache Control 设计（v0.2 已实现）

## 1. 文档状态

- 状态：已按 `endpoint-benchmark 0.2.0` 实现，39 个自动测试和三组真实多运行时完整流程均通过。
- 实现前基线：`endpoint-benchmark 0.1.1`。
- 实现版本：`endpoint-benchmark 0.2.0`。
- 本文同时记录实现时根据固定版本真实 API 做出的收敛调整。
- 真实 GPU 测试还暴露了 response-stage stale keep-alive；为避免重复执行已发送的 POST，通用传输层不会在 `getresponse()` 失败后自动重放请求。该调整不改变 cache control 合同。
- 真实矩阵直接使用 200 条业务数据，保留工具调用、两条图片请求和每条最大 8192 输出上限；没有 mock、抽样、输出截断或 `ignore_eos`。

本文解决一个问题：压测调用方只选择逻辑 Target，程序自动清理该 Target 下所有已声明或已发现实例的运行时缓存。覆盖直连 vLLM、直连 SGLang、Dynamo 1.3.0 legacy，以及它们的混合组合。

上游接口事实、源码证据和线上 smoke test 记录见 [Runtime cache clear matrix](../research/runtime-cache-clear-matrix.md)。

## 2. 当前问题

当前 [cache_control.py](../../src/endpoint_benchmark/cache_control.py) 从推理 endpoint 推导 vLLM origin，调用 `POST /reset_prefix_cache`，并要求响应为 `{"success": ...}`。它有四个缺口：

1. vLLM 0.24.0 成功时返回空 body 200，当前实现会误判失败。
2. SGLang 使用 `/flush_cache`，路径、busy 响应和 HiCache 清理均不同。
3. Dynamo 1.3.0 legacy 需要 discovery 后逐 instance ID 调用 `clear_kv_blocks`。
4. gateway 或负载均衡地址不能证明其后的全部物理实例均已清理。

## 3. 目标与边界

### 3.1 目标

1. 调用方只选择一个 Target，不传 runtime-specific 清理参数。
2. 推理入口与缓存实例来源分离。
3. 同一 Target 可以组合 direct vLLM、direct SGLang 和 Dynamo legacy。
4. direct 实例自动识别运行时；Dynamo 使用 discovery capability。
5. 对全部实例并行清理；transient 或 busy 错误在统一截止时间内重试。
6. 任一实例未知或清理失败时终止压测，不静默跳过。
7. 控制台输出有限重试日志，结果中保留逐实例摘要且不泄露 secret。
8. 保留旧 prefix-cache CLI，并把它们映射到统一的 direct/Dynamo 清理路径。

### 3.2 非目标与运行前提

- 不管理或重启模型进程。
- 不 drain 外部流量，不抢占运行中请求；压测期间必须没有外部请求。
- 部署拓扑在一次 clear 调用期间必须稳定；第一版不处理滚动扩缩容。
- 不支持 Dynamo 1.3.0 unified worker。
- 不泛化现有 tokenize preflight 或 Prometheus 指标。
- 不提供插件系统、本机锁或分布式锁。
- 不给 `run.json` 或 `cache_resets.json` 引入新的 schema version。

## 4. 核心决策

| 维度 | 决策 |
| --- | --- |
| 清理时机 | 首次 warmup 前 startup check；每档 warmup 后 pre clear；metrics-after 后 post clear；请求异常时 post-failure clear |
| 清理模式 | 默认启用；只有显式 `--no-reset-prefix-cache` 才跳过整套生命周期；没有 best-effort 模式 |
| 实例范围 | direct 使用显式地址；Dynamo 每次 clear discovery 一次 |
| direct 识别 | 依次尝试 vLLM `/version`、SGLang `/server_info`，命中后停止 |
| vLLM 成功 | 空 body 的 HTTP 2xx 兼容 0.24；非空 body 必须明确 `success=true` |
| SGLang 成功 | `/flush_cache` HTTP 2xx；该固定请求的 HTTP 400 可重试 |
| Dynamo 成功 | 每个 instance 明确返回 `status=success` |
| 外部缓存 | vLLM 默认 `reset_external=false`，`--reset-external-prefix-cache` 时为 true；SGLang 启用 HiCache 时追加 storage clear |
| 失败策略 | 60 秒内重试 transient/busy；其余立即 fail closed |
| 配置格式 | TOML，使用 Python 3.11 `tomllib` |

pre clear 定义 measurement 的缓存初始状态；startup check 提前验证所有实例的管理能力，post/post-failure clear 保证本工具退出或进入下一档前完成清理。每次调用都重新执行 direct probe 与 Dynamo discovery，不复用可能过期的实例集合。

## 5. Target 配置

### 5.1 查找与选择

- `--targets-file PATH` 显式指定配置；默认读取 `${XDG_CONFIG_HOME:-~/.config}/endpoint-benchmark/targets.toml`。
- `--target NAME` 选择 Target。
- `--target` 与现有 `--endpoint` 互斥。
- Target 模式不允许 CLI 覆盖 endpoint 或 headers，避免一半来自配置、一半来自参数。

### 5.2 示例

```toml
[targets.mixed]
inference_endpoints = [
  "http://gateway:8080/v1/chat/completions",
]
headers = { "X-Tenant" = "benchmark" }
header_env = { "Authorization" = "INFERENCE_AUTH_HEADER" }
clear_timeout_s = 60.0
clear_retry_interval_s = 0.5

[[targets.mixed.cache_sources]]
kind = "direct"
endpoints = [
  "http://vllm-worker:8000",
  "http://sglang-worker:30000",
]
header_env = { "Authorization" = "ADMIN_AUTH_HEADER" }

[[targets.mixed.cache_sources]]
kind = "dynamo"
namespace = "benchmark"
components = ["VllmWorker", "SglangWorker"]
```

简单直连 Target 可以复用 inference endpoints：

```toml
[targets.local]
inference_endpoints = [
  "http://127.0.0.1:8000/v1/chat/completions",
]

[[targets.local.cache_sources]]
kind = "direct"
use_inference_endpoints = true
```

`header_env` 的 key 是 HTTP header 名，value 是环境变量名；环境变量内容作为完整 header value 原样使用。例如 Authorization 环境变量需要自行包含 `Bearer ` 前缀。缺失变量立即报错，解析值不得进入日志或结果。

header 名和值中的控制字符会在 Target 配置边界以不包含原值的通用错误拒绝，避免底层 HTTP 异常回显环境变量中的 secret。

Target 级 headers 只用于 inference 请求。显式 direct endpoints 只使用该 source 自己的 headers；`use_inference_endpoints=true` 且 source 未配置 headers 时，才复用 Target headers，避免把推理凭据发送到另一台管理主机。

### 5.3 校验

- 未知字段报错。
- Target 至少包含一个 inference endpoint 和一个 cache source。
- timeout 必须大于零，retry interval 不能为负。
- direct source 必须且只能选择 `endpoints` 或 `use_inference_endpoints=true`。
- direct endpoint 必须是物理管理实例，不能是隐藏副本的负载均衡地址。
- Dynamo source 必须提供非空且不含 `.` 的 `namespace` 和 `components`（运行时 endpoint segment 合同），不扫描整个 namespace。

### 5.4 临时 `--endpoint` 模式

保留现有 `--endpoint`：

- 每个 endpoint 同时作为 inference endpoint 和 direct cache instance。
- 继续使用现有 `--header` 与 `--api-key-env`。
- 不承诺清理 endpoint 背后的隐藏副本。
- 保留 cache lifecycle、timeout、retry interval 和 vLLM external reset flags；不提供手工 runtime type flag。

## 6. 最小模块边界

实现新增 `target_config.py`，并替换现有 [cache_control.py](../../src/endpoint_benchmark/cache_control.py)：

```text
target_config.py
  load_target(path, name) -> TargetConfig

cache_control.py
  clear_caches(target, concurrency, phase, reset_external) -> dict
  _clear_direct(...)  # vLLM / SGLang 分支
  _clear_dynamo_sources(...)
```

不建立 adapter 基类、factory、plugin registry 或 session 对象。direct 分支与 Dynamo source 函数已经是足够的内部接缝。

`clear_caches()` 每次调用：

1. 从所有 cache sources 得到本次实例列表。
2. 对 direct 实例顺序执行只读 runtime probe。
3. 并行清理每个实例。
4. 只对 transient/busy 失败实例重试至总截止时间。
5. 全部成功时返回一条审计记录；任一失败时抛 `CacheControlError`。

## 7. 实例发现与识别

### 7.1 Direct

配置 URL 先规范化为管理 origin，再去重。每个实例严格按以下顺序识别：

1. `GET /version`。
2. 若响应是 HTTP 200 且 JSON 恰为仅含字符串 `version` 的对象，则识别为 vLLM，并停止探测。
3. 否则调用 `GET /server_info`。
4. 若响应是 HTTP 200，且 JSON 包含字符串 `version`、展开后的字符串 `model_path` 与数组 `internal_states`，则识别为 SGLang；顶层 `enable_hierarchical_cache` 为 `true` 时再清理 HiCache 存储。
5. 仍不匹配时立即失败。

`/server_info` 不会发送给已经识别为 vLLM 的实例。版本用于日志与结果，不建立版本 allowlist；接口合同变化由 smoke test 和自动测试发现。

不得用 clear endpoint、`HEAD`、`OPTIONS` 或 `/openapi.json` 作为必要识别步骤。完整 `/server_info` body 不得落盘。

### 7.2 Dynamo 1.3.0 legacy

对每个 `namespace + component`：

1. 使用当前 event loop、`${DYN_DISCOVERY_BACKEND:-etcd}` 和 `${DYN_REQUEST_PLANE:-tcp}` 创建 `DistributedRuntime`。
2. 调用 `runtime.endpoint("dyn://<namespace>.<component>.clear_kv_blocks")`，再 `await endpoint.client()`。
3. 对该 client 调用一次 `await wait_for_instances()`，在共享 deadline 内等待 discovery 初始化完成并取得当次 instance ID snapshot；返回空列表或超时均失败。
4. 对每个 instance 使用 `await client.direct({}, instance_id, annotated=False)` 定向调用，并完整消费异步 response stream。

capability 本身就是运行时无关的识别结果，不再猜测后端是 vLLM 还是 SGLang。零实例、discovery 失败或调用期间实例消失均失败。

第一版不重复 discovery，也不等待连续 snapshot 稳定。部署方必须保证 clear 调用期间不滚动扩缩容。

## 8. 清理合同

### 8.1 Direct vLLM

```text
POST /reset_prefix_cache
  ?reset_running_requests=false
  &reset_external={false|true}
```

`reset_external` 默认 false，仅在传入 `--reset-external-prefix-cache` 时为 true。

- 兼容 vLLM 0.24.0：空 body 的 2xx 视为 handler 完成。
- 非空 body 必须是 `{"success": true}`；`success=false` 或未知响应重试至截止时间，避免把 2xx 假阴性记作清理成功。
- 404/405 立即失败；404 提示检查 `VLLM_SERVER_DEV_MODE=1`。
- 不使用 `reset_running_requests=true`。

vLLM 0.24.0 的 HTTP handler 会丢弃底层布尔结果，因此空 body 的 2xx 只能证明 handler 完成；vLLM 0.26.0 会显式返回底层 `success`。旧版仍依赖“没有外部流量且 runner 请求已经结束”的前提，不虚构更强保证。

### 8.2 Direct SGLang

```text
POST /flush_cache?timeout=0
```

- 2xx 视为本地 cache 清理成功。
- 该固定请求的 400 表示 flush 未成功，统一重试至截止时间；其他 4xx 立即失败。
- 不调用 pause/retract，不使用具有副作用的 GET route 探测。

若 `/server_info` 表明启用 HiCache，在本地 flush 成功后调用：

```text
POST /hicache/storage-backend/clear
```

该调用任意 2xx 视为成功。

### 8.3 Dynamo legacy

```text
<namespace>.<component>.clear_kv_blocks
```

- 每个 instance 发送空 request 并完整消费 response stream。
- 只有明确的 `status = "success"` 成功。
- `status = "error"` 可重试。
- 不使用 `/engine/flush_cache`，不实现 v1.3.0 unified route。

## 9. 重试、错误与日志

每次 `clear_caches()` 共用一个 monotonic deadline：

```text
default deadline = 60.0 s
default retry interval = 0.5 s
```

| 错误 | 处理 |
| --- | --- |
| 网络错误、连接超时、HTTP 5xx | 重试 |
| SGLang `/flush_cache?timeout=0` 的 HTTP 400 | 重试 |
| Dynamo `status=error` | 重试 |
| 401/403、404/405、其他 4xx | 立即失败 |
| 未知 runtime 或响应合同 | 立即失败 |
| 配置、环境变量或 secret 解析错误 | 立即失败 |

控制台只打印每个实例的首次失败、每 5 秒一次的重试进度和最终结果。不得每 0.5 秒重复刷屏。

URL 输出前移除 userinfo、query 和 fragment；无法安全解析的 URL 使用固定占位符。响应摘要最多 1 KiB，并移除控制字符；headers 和环境变量解析值永不输出。实现还会从响应摘要与错误文本中替换已配置的 header value，防止服务端回显 secret。

## 10. Runner 与结果

runner 生命周期为：

```text
resolve target
resolve output directory
load dataset / optional preflight
if reset enabled:
  clear_caches(target, first concurrency, startup_check)

for each concurrency:
  warmup
  if reset enabled:
    clear_caches(target, concurrency, pre)
  metrics before
  measured requests
  metrics after
  if reset enabled:
    clear_caches(target, concurrency, post)

write existing result files
```

规则：

- clear、probe 和 retry 不计入 benchmark duration。
- startup/pre clear 失败时不发送 measured workload，并返回退出码 `1`。
- post clear 失败时不进入下一档；请求阶段抛异常时先执行 post-failure clear。
- `--no-reset-prefix-cache` 跳过 startup、pre、post 和 post-failure，`cache_resets.json` 中对应档位为空数组。
- 退出码 `3` 继续只表示压测已完成但成功率不达标。

沿用现有 `BenchmarkResult.cache_resets_by_concurrency`、`run.json` 和 `cache_resets.json` 容器，不新增 schema version。startup record 放在首个 concurrency 下，每档保存通用 pre/post record：

```json
{
  "phase": "pre",
  "concurrency": 8,
  "duration_ms": 1234.5,
  "success": true,
  "instances": [
    {
      "source": "direct-0",
      "address": "http://worker:8000",
      "instance_id": null,
      "runtime": "vllm",
      "version": "0.24.0",
      "operation": "reset_prefix_cache",
      "attempts": 1,
      "status_code": 200,
      "response": "",
      "success": true
    }
  ]
}
```

第一版不建立持续审计 journal。clear 失败时以控制台错误为准；只有成功完成的 benchmark 才保证结果文件完整。

## 11. CLI、版本与依赖

新增：

```text
--targets-file PATH
--target NAME
```

保留并接入统一 cache control：

```text
--reset-prefix-cache
--no-reset-prefix-cache
--prefix-cache-reset-timeout
--prefix-cache-reset-interval
--reset-external-prefix-cache
```

`--reset-prefix-cache` 默认 true，`--no-reset-prefix-cache` 显式关闭；timeout 默认 60 秒、interval 默认 0.5 秒，Target 模式未显式传 CLI 值时沿用 TOML。`--reset-external-prefix-cache` 默认 false 且只影响 vLLM。保留 `PrefixCacheResetConfig` 及其包导出；临时 `--endpoint` 仍转成内部 `TargetConfig`，runner 不维护第二套清理实现。

package version 升级为 `0.2.0`；结果文件名称和顶层容器保持现状，只替换 cache reset record。

基础安装继续保持零第三方运行时依赖。TOML、HTTP、线程并发均使用标准库。Dynamo 是 Linux 可选依赖：

```toml
[project.optional-dependencies]
dynamo = ["ai-dynamo-runtime==1.3.0.post1"]
```

只有实际使用 Dynamo source 时才延迟 import。

## 12. 最小测试集

继续使用现有 `unittest`，不增加测试框架：

1. TOML Target 解析、未知字段、缺失环境变量，以及 header 控制字符的脱敏拒绝。
2. vLLM 空 body 200 与 `success=true` 成功，`success=false` 重试/失败，且不调用 `/server_info`。
3. SGLang 识别、flush 400 重试和可选 HiCache storage clear。
4. mixed direct source 对每个 vLLM/SGLang 实例各调用一次。
5. Dynamo discovery 一次并逐 instance ID 调用，只有 `status=success` 成功。
6. 任一未知或失败实例阻止 measured workload。
7. runner 默认执行 startup/pre/post，失败路径执行 post-failure；显式关闭后不 probe、不 clear，审计数组为空。
8. secret、headers 和完整 `/server_info` 不进入日志或结果。
9. response-stage 连接失败不自动重放已发送的 POST。
10. CLI 默认值、显式 disable、timeout/interval 和 external reset 覆盖正确。

验证命令：

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```

真实 GPU smoke 与完整业务流测试是补充证据，不作为自动验收前提。环境、限制、性能数据、A/B 清理证明与场景矩阵统一记录在 [线上环境验证记录](../research/runtime-cache-clear-matrix.md#线上环境验证记录)。

### 12.1 真实多运行时完整矩阵

2026-08-06 在一台 8×H100 80GB、NVIDIA driver `550.54.15` 的机器上，直接读取用户提供的 `requests_200_seed42_max8k_openai.jsonl`。200 行都包含 tools，其中索引 21、46 还包含 data-URI 图片；vLLM 精确预检输入 token 为 3,335–29,151，均值 7,785.235。每组都在同一进程中顺序执行 concurrency 1 和 8，每轮 4 条 warmup 后执行 pre clear，再发送全部 200 条正式请求。

| 组合 | concurrency | 成功 | 时长 | req/s | input tok/s | output tok/s | total tok/s | E2E P95 | TTFT P95 | TPOT P95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Dynamo 1.3.0.post1 + vLLM 0.23.0 | 1 | 200/200 | 470.11 s | 0.425 | 3,312.09 | 191.37 | 3,503.46 | 6,527.54 ms | 1,861.99 ms | 4.29 ms |
| Dynamo 1.3.0.post1 + vLLM 0.23.0 | 8 | 200/200 | 106.26 s | 1.882 | 14,652.72 | 1,005.24 | 15,657.96 | 12,995.12 ms | 2,642.92 ms | 7.31 ms |
| direct vLLM 0.23.0 + SGLang 0.5.10.post1 | 1 | 200/200 | 751.20 s | 0.266 | 2,030.95 | 164.20 | 2,195.15 | 11,991.55 ms | 3,586.53 ms | 5.47 ms |
| direct vLLM 0.23.0 + SGLang 0.5.10.post1 | 8 | 200/200 | 223.10 s | 0.896 | 6,838.51 | 527.27 | 7,365.79 | 26,739.57 ms | 7,609.17 ms | 12.92 ms |
| direct SGLang 0.5.10.post1 + Dynamo/vLLM | 1 | 200/200 | 788.46 s | 0.254 | 1,934.98 | 162.87 | 2,097.84 | 13,018.17 ms | 3,717.48 ms | 5.41 ms |
| direct SGLang 0.5.10.post1 + Dynamo/vLLM | 8 | 200/200 | 267.48 s | 0.748 | 5,703.86 | 510.65 | 6,214.50 | 29,403.48 ms | 7,464.42 ms | 13.25 ms |

验证结果：

- 1,200 条正式请求全部成功。两个双端点 Target 在每个 concurrency 下都严格分到 100/100；索引 21、46 的图片请求在每轮都成功。
- 每轮 cache audit 覆盖 Target 的全部 source：Dynamo `clear_kv_blocks`、vLLM `reset_prefix_cache`、SGLang `flush_cache` 均一次成功。三组流程结束后又分别执行 final clear，全部成功。
- vLLM+SGLang 的 transport retry 为 50/59，mixed 为 10/29，全部发生在复用连接的 send stage；请求进入 `getresponse()` 后不重放。Dynamo+vLLM 为 0/0。所有重试后的正式请求仍各只有一个服务端完成记录。
- 结果、逐请求 JSONL、cache audit、服务日志和清理日志保存在 `/Users/zhuanzmima0000/Desktop/工作记录/推理优化/压测结果导出/real-runtime-matrix_20260806/`。

真实环境做了两项版本收敛：Dynamo 的默认 CUDA 13 wheel 与 driver 550 不兼容，因此保持要求的 `ai-dynamo-runtime==1.3.0.post1`，配套使用官方 `vLLM 0.23.0+cu129`、`torch 2.11.0+cu129`；SGLang 0.5.16 同样要求 CUDA 13，因此选用仍支持 Qwen3.5 且基于 CUDA 12.9 的官方 `0.5.10.post1`、`torch 2.9.1+cu129`。这只调整测试运行时的兼容版本，没有放宽 cache contract。

该矩阵验证了三种真实 runtime 组合、全部统一 clear adapter 与业务数据路径；运行时版本当时只自动记录 pre clear，并在每组末尾执行显式 final clear。2026-08-07 恢复的 startup/post 调度复用同一个 `clear_caches()`，其顺序与 disable 分支由下节的真实 socket 回归覆盖，不把旧审计记录改写成新生命周期结果。

### 12.2 Cache 生命周期回归

2026-08-07 完整自动测试为 42/42。runner 回归通过本地真实 HTTP socket 执行两个 concurrency：观测到 1 次 startup check、每档各 1 次 pre/post，共 5 次 reset；首段事件顺序为 `reset → warmup request → reset`，所有 vLLM 请求都带 `reset_external=false`。另一路显式关闭测试完成 warmup 与 measurement，但 runtime probe 和 reset 均为 0，`cache_resets.json` 对应值为空数组。

## 13. 已接受限制

| 限制 | 处理 |
| --- | --- |
| vLLM 0.24 空 body 2xx 可能掩盖底层 `False` | 0.26 非空 body 会校验 `success`；0.24 仍依赖无外部流量和请求排空 |
| 某些 connector 的 external reset 可能是 no-op | 调用官方接口并记录结果，不增加 sidecar |
| direct 地址可能被误填为 LB | 配置合同禁止；程序无法从普通 URL 可靠识别 |
| clear 期间出现新实例 | 第一版依赖拓扑稳定；出现真实滚动部署需求后再增加 convergence |
| 多台压测机同时运行 | 第一版依赖运维互斥；出现冲突后再增加 lease |
| clear 失败没有完整结果文件 | 控制台保留错误；需要事故审计时再增加 append-only journal |
| Dynamo file discovery 多端点租约注册存在启动竞态 | 本次观察到部分 `generate`/`clear_kv_blocks` 注册约 12 秒后过期；runner 会因零 clear instance fail closed。测试保留两次失败日志，只在官方未修改 worker 的三个端点持续刷新后开始测量；生产部署应使用 etcd discovery |

## 14. 实现顺序与完成条件

实现顺序：

1. Target TOML、CLI 互斥和严格校验。
2. direct 顺序识别与 vLLM/SGLang clear。
3. Dynamo discovery 与逐实例 RPC。
4. runner startup/pre/post 生命周期、显式 disable 和逐实例结果。
5. 最小测试、README 与接口文档同步。

完成必须同时满足：

- 第 12 节自动测试全部通过。
- mixed Target 不需要 per-run runtime 参数。
- 每个已配置或已发现实例都有一条成功记录。
- 任一未知或失败实例都不能进入 measured workload。
- 基础安装继续为零第三方运行时依赖。
- 没有 session、adapter 层级、拓扑收敛、锁或全局结果 schema 迁移。

上述条件已经由自动测试、第 12.1 节真实 runtime 矩阵和第 12.2 节生命周期回归共同满足。
