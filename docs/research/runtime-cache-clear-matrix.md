# Runtime cache clear matrix

## 外部运行时事实

> 核查日期：2026-08-06。运行时事实只采用 vLLM / SGLang / Dynamo 官方文档、官方仓库源码和官方 release/tag；仓库事实来自本地源码、测试和文档。下文的“事实”均固定到 release commit；“推断”只表示由这些源码能得出的部署结论，不把它冒充成上游承诺。

### 版本基线

| 运行时 | 本文固定版本 | 官方证据 |
| --- | --- | --- |
| vLLM | `v0.24.0`，commit `ee0da84ab9e04ac7610e28580af62c365e898389`。截至核查日，官方 `0.24.x` 稳定 tag 是 `v0.24.0`（另有 `v0.24.0rc1/rc2`，本文不以 RC 为准）。 | [v0.24.0 release](https://github.com/vllm-project/vllm/releases/tag/v0.24.0)、[固定 commit](https://github.com/vllm-project/vllm/commit/ee0da84ab9e04ac7610e28580af62c365e898389) |
| SGLang | `v0.5.16`，release commit `fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1`；官方 release 页面在核查日标记为 **Latest**。 | [v0.5.16 release](https://github.com/sgl-project/sglang/releases/tag/v0.5.16)、[固定 commit](https://github.com/sgl-project/sglang/commit/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1) |
| Dynamo | `v1.3.0`，commit `8ce9e22f11576402102ea9d8b8e46233f5430a0d`；这里只核对与实例发现、vLLM/SGLang cache clear 有关的发布源码。 | [v1.3.0 release](https://github.com/ai-dynamo/dynamo/releases/tag/v1.3.0)、[固定 commit](https://github.com/ai-dynamo/dynamo/commit/8ce9e22f11576402102ea9d8b8e46233f5430a0d) |

### vLLM `v0.24.0`

#### 对外与内部接口

| 接口 | 明确事实 |
| --- | --- |
| HTTP | `POST /reset_prefix_cache`，查询参数为 `reset_running_requests: bool = false` 与 `reset_external: bool = false`。路由只在 `VLLM_SERVER_DEV_MODE` 开启时注册；该环境变量默认 `0`，官方注释将这类路由定位为开发/调试端点。[路由与默认参数](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/entrypoints/serve/dev/cache/api_router.py#L20-L43) [条件注册](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/entrypoints/openai/api_server.py#L197-L200) [环境变量定义](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/envs.py#L1288-L1291) |
| Python / engine 内部接口 | `EngineClient.reset_prefix_cache(reset_running_requests=False, reset_connector=False) -> bool`；`AsyncLLM` 将调用转发给 `EngineCore`。[协议](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/engine/protocol.py#L157-L161) [AsyncLLM 实现](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/engine/async_llm.py#L921-L926) |

#### 清理范围、返回值和运行中请求

- **事实：HTTP 200 只是“调用完成”的确认，不是“缓存确已清空”的确认。** 路由 `await` 内部布尔接口后直接丢弃返回值，固定返回空的 `Response(status_code=200)`；源码注释也明确说 API server 当前不检查 reset 是否成功。[源码](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/entrypoints/serve/dev/cache/api_router.py#L26-L43)

- **事实：本地 prefix cache reset 是逻辑索引失效，不是把 KV 显存逐字节清零。** `BlockPool.reset_prefix_cache()` 只有在除常驻 null block 外没有占用 block 时才成功；成功路径会替换/清空 block-hash 映射、清掉每个 block 的 hash，并发出 `AllBlocksCleared` 事件。[源码](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/core/block_pool.py#L656-L690)

- **事实：默认 `reset_running_requests=false` 时，仍被请求占用的 KV block 会令内部结果为 `False`。** HTTP 层仍会返回 200，因为该布尔结果被丢弃。[占用检查](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/core/block_pool.py#L665-L672) [HTTP 丢弃结果](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/entrypoints/serve/dev/cache/api_router.py#L32-L43)

- **事实：`reset_running_requests=true` 会把当前 running requests 全部 preempt 到 waiting queue，然后重置 prefix cache；它不是拒绝运行中请求。** 若请求仍在等待远端 KV transfer，强制 preempt 后 reset 仍可能失败并抛 `RuntimeError`，源码明确标注该情形尚不支持。[源码](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/core/sched/scheduler.py#L2148-L2191)

- **事实：`reset_external=true` 会额外调用当前 engine 配置的 KV connector reset，并把 connector 结果与本地结果合并。** 未配置 connector 时按成功的 no-op 处理；connector 返回精确的 `False` 才被视为失败。[源码](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/core/sched/scheduler.py#L2193-L2218)

- **事实：并非所有 connector 都真正实现外部清理。** 基类 `reset_cache()` 默认只记录日志并返回 `None`，而 scheduler 只把 `is False` 当失败。因此即使调用内部布尔接口，某个沿用基类默认实现的 connector 也可能被当作“未失败”，但其外部存储没有被清理。[connector 基类](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/base.py#L691-L703) [scheduler 判定](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/core/sched/scheduler.py#L2198-L2218)

#### 实例与副本覆盖范围

- **事实：vLLM 内置 DP load balancer 模式会向该 server 管理的所有 DP `core_engines` 并发调用 utility，但只返回第一个 engine 的结果。** `reset_prefix_cache` 正是 utility call；因此调用会尝试覆盖同一 vLLM server 内部管理的 DP engines，但聚合结果不是 `all()`。[内部 DP client 选择](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/engine/core_client.py#L107-L132) [DP utility fan-out 与首结果](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/engine/core_client.py#L1380-L1452)

- **事实：配置 external DP load balancer 时，上游选择的是“每个 DP rank 一个 client”的 `DPAsyncMPClient`，而不是内部 fan-out client。** [源码](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/engine/core_client.py#L126-L132) [类契约](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/engine/core_client.py#L1200-L1202)

- **推断：独立部署的多个 vLLM 服务副本（包括 external-DP rank、Kubernetes replicas、独立 P/D 实例）不能靠一次 HTTP 请求全部清理。** 端点只取得当前 FastAPI app 的 `engine_client`，没有服务发现或跨实例广播；external-DP 源码也明确采用 per-rank client。控制面需要枚举并逐实例/逐 rank fan-out。[当前 app 的 engine client](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/entrypoints/serve/dev/cache/api_router.py#L16-L17) [external-DP client 选择](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/engine/core_client.py#L126-L132)

### SGLang `v0.5.16`

#### 对外与内部接口

| 接口 | 明确事实 |
| --- | --- |
| 本地 radix/KV cache HTTP | `GET` 或 `POST /flush_cache?timeout=<seconds>`；`timeout` 默认 `0.0` 且必须非负。成功返回文本与 HTTP 200；失败返回内部 message（或 `Flush cache failed.`）与 HTTP 400。路由标记为 `AuthLevel.ADMIN_OPTIONAL`。[源码](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/entrypoints/http_server.py#L905-L920) |
| Python Engine | `Engine.flush_cache()` 同步执行 `tokenizer_manager.flush_cache()`；该入口没有暴露 timeout 参数，返回的是 `FlushCacheReqOutput`（含 `success`、`message`），不是裸布尔值。[Engine 入口](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/entrypoints/engine.py#L935-L936) [返回结构](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/io_struct.py#L1418-L1424) [manager 实现](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/tokenizer_control_mixin.py#L275-L281) |
| HiCache 外部 storage HTTP | 当前接口是 `POST /hicache/storage-backend/clear`；旧的 `GET|POST /clear_hicache_storage_backend` 仍存在但已标为 deprecated。两者成功用 200、失败用 400，并标记为 `AuthLevel.ADMIN_OPTIONAL`。[源码](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/entrypoints/http_server.py#L981-L1005) |

#### `/flush_cache` 的清理范围、返回值和运行中请求

- **事实：立即 flush 只在 scheduler “完全空闲”时执行。** 成功路径会 reset `tree_cache`，清空 request-to-token pool、KV allocator、grammar cache、metrics、draft worker cache pool，并默认调用设备 allocator 的 `empty_cache()`；不空闲则返回 `False`。[源码](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/scheduler.py#L3846-L3874)

- **事实：“完全空闲”比“没有 running/waiting request”更严格。** 检查还覆盖 chunked request、last batch、overlap result queue、PP microbatches、grammar queue、P/D disaggregation 队列与 transfer、decode offload、HiSparse staging，以及 HiCache 的 in-flight write/load/prefetch/backup。[源码](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/scheduler.py#L3686-L3734)

- **事实：默认 `timeout=0` 时，只要不完全空闲就立即失败，HTTP 返回 400；`timeout>0` 时会把 flush 挂起，等待自然变为空闲后执行，超时则返回 `Timed out waiting for idle state.`。** 同一 scheduler 已有 pending flush 时，新调用失败并返回 `Another flush_cache is already in progress.`。[deferred flush 实现](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/scheduler_components/flush_wrapper.py#L24-L65) [HTTP 状态映射](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/entrypoints/http_server.py#L905-L920)

- **事实：`/flush_cache` 本身不会 preempt/abort 运行中请求。** 若需要保留并重算运行中请求，SGLang 另有 `pause_generation` 的 `retract` 模式；该模式把 running requests 收回 waiting queue，源码注释明确说此时 KV cache 可 flush，恢复后自动重算。[pause 模式契约](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/io_struct.py#L1490-L1506) [HTTP pause/continue 路由](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/entrypoints/http_server.py#L1622-L1645)

#### HiCache 外部 storage 的独立清理语义

- **事实：`/flush_cache` 与 HiCache storage clear 不是同一个操作。** `HiRadixCache.reset()` 重置 controller、host pool 和本地 radix tree；普通 `HiCacheController.reset()` 清队列并重启 transfer threads，没有调用 storage backend 的 `clear()`。真正调用 backend `clear()` 的是 `HiRadixCache.clear_storage_backend()`。[HiRadix reset](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/mem_cache/hiradix_cache.py#L773-L780) [controller reset](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/cache_controller.py#L643-L671) [backend clear](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/mem_cache/hiradix_cache.py#L811-L831)

- **推断：如果控制目标包含 HiCache L3 / 外部 storage，不应把 `/flush_cache` 当作充分条件；还要调用 storage clear 接口，并按具体 backend 的能力验证。** 这是由上面两个独立代码路径得出的部署结论。[本地 reset 路径](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/mem_cache/hiradix_cache.py#L773-L780) [storage clear 路径](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/mem_cache/hiradix_cache.py#L811-L831)

- **事实：`v0.5.16` 的 HiCache HTTP 成功语义存在缺口。** `clear_hicache_storage_wrapped()` 只要 hierarchical cache 已启用，就忽略 `tree_cache.clear_storage_backend()` 的布尔返回并构造 `success=True`；但后者在 backend 没有 `clear`、抛异常或 storage 未启用时会返回 `False`。因此 HTTP 200 不能证明底层 storage 已实际清空。[wrapper 忽略返回值](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/scheduler.py#L3634-L3642) [backend 失败分支](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/mem_cache/hiradix_cache.py#L811-L831) [HTTP 状态映射](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/entrypoints/http_server.py#L997-L1005)

- **事实：HiCache storage clear 的 scheduler wrapper 没有 `is_fully_idle()` 检查。** 这与 `/flush_cache` 的严格空闲门槛不同；源码没有承诺它会等待在途请求/transfer 排空。[HiCache clear wrapper](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/scheduler.py#L3634-L3642) [flush 的空闲检查](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/scheduler.py#L3846-L3874)

#### 实例与副本覆盖范围

- **事实：同一个 SGLang server 的控制面按 `server_args.dp_size` 建立 `FanOutCommunicator`，`flush_cache` 与 `clear_hicache_storage` 都走该机制。** communicator 的契约是 fan-out 给所有 recipients，并等齐全部响应。[communicator 初始化](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/tokenizer_control_mixin.py#L90-L144) [等待全部响应](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/communicator.py#L13-L23) [收齐条件](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/communicator.py#L90-L103)

- **事实：尽管会等待所有内部 DP responses，`flush_cache()` 和 `clear_hicache_storage()` 都只返回结果数组的 `[0]`，没有对所有 rank 做 `all(success)` 聚合。** 因而 HTTP 状态只反映首个 response；其他内部 DP rank 的失败理论上可能被遮蔽。[flush/clear 返回首结果](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/tokenizer_control_mixin.py#L275-L289) [官方已有的 all-success 聚合工具](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/communicator.py#L105-L110)

- **推断：独立部署的多个 SGLang server 副本或独立 P/D 实例不能靠一次 `/flush_cache` 全部清理。** 上游 fan-out 的边界是当前 tokenizer manager 配置的 `dp_size` recipients，不包含外部服务发现；控制面仍需逐服务实例 fan-out，并逐个检查 HTTP 结果/日志。[fan-out 边界](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/tokenizer_control_mixin.py#L132-L144)

### 非破坏性实例识别与能力探测

#### 官方只读接口

- **事实：vLLM `v0.24.0` 提供 `GET /version`，固定返回 `{"version": VLLM_VERSION}`，且处理函数不调用 engine。** 该路由属于默认注册的 instrumentator routers，不依赖 `VLLM_SERVER_DEV_MODE`，适合做每实例、无副作用的 vLLM 指纹探测。[路由与返回体](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/entrypoints/serve/instrumentator/basic.py#L53-L56) [默认注册](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/entrypoints/serve/instrumentator/__init__.py#L7-L14)

- **事实：SGLang `v0.5.16` 提供 `GET /server_info`；旧的 `GET /get_server_info` 仍可用但已 deprecated。** 新接口读取每个 DP 的 internal state，并返回展开的 `server_args`、scheduler info、`internal_states`、SGLang `version` 和 KV-event publisher 描述；它是只读接口，但返回信息明显多于一个版本探针。[源码](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/entrypoints/http_server.py#L733-L765)

- **事实：vLLM 的 OpenAPI 可以用 `--disable-fastapi-docs` 关闭。** 该选项默认 `False`；开启后同时把 OpenAPI schema、Swagger UI 与 ReDoc 关闭，FastAPI 构造时令 `openapi_url=None`。[选项契约](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/entrypoints/openai/cli_args.py#L281-L292) [应用构造](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/entrypoints/openai/api_server.py#L171-L178)

- **事实：SGLang 的 OpenAPI 可以用环境变量 `DISABLE_OPENAPI_DOC` 关闭。** 未关闭时 schema 路径显式设为 `/openapi.json`；关闭时 `openapi_url=None`。[源码](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/entrypoints/http_server.py#L428-L432)

#### 建议的无副作用探测顺序（推断）

1. 对每个解析后的实例地址调用 `GET /version`；只有在 HTTP 200、JSON 顶层含字符串 `version`，且版本/产品契约与预期 vLLM 相符时，才把它识别为 vLLM 候选。该接口本身只读且不访问 engine。[vLLM 返回契约](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/entrypoints/serve/instrumentator/basic.py#L53-L56)
2. 若不是 vLLM 候选，再调用 `GET /server_info`；校验 `version`，并可用 `internal_states`、scheduler/server 参数结构确认 SGLang。该调用只读但会向内部 DP 读取状态，应设置短超时、响应体大小上限，且不要把完整响应写入普通日志。[SGLang 返回契约](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/entrypoints/http_server.py#L743-L765)
3. `GET /openapi.json` 只能作为可选的能力补充：schema 存在时可只读检查目标 clear route 与参数；schema 404/不可达不能证明 route 不存在，因为两个运行时都允许关闭 OpenAPI。[vLLM 可关闭](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/entrypoints/openai/api_server.py#L171-L178) [SGLang 可关闭](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/entrypoints/http_server.py#L428-L432)
4. 能力探测结果应绑定到**实例地址 + 运行时种类 + 版本 + 探测时间**，不能把某个负载均衡地址的一次结果外推给所有后端副本；两套上游的内部 fan-out 都有明确边界。[vLLM 边界](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/engine/core_client.py#L126-L132) [SGLang 边界](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/tokenizer_control_mixin.py#L132-L144)

#### 接口冲突与误探测风险

- **事实：绝不能用 `GET /flush_cache` 判断 SGLang route 是否存在。** SGLang 官方把 `/flush_cache` 同时注册为 GET 和 POST，GET 会真的执行清理；deprecated 的 `/clear_hicache_storage_backend` 也接受 GET 并执行 storage clear。[flush route](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/entrypoints/http_server.py#L905-L920) [deprecated HiCache route](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/entrypoints/http_server.py#L981-L1005)

- **推断：也不要用 clear endpoint 的 POST、GET、自动重试或“试一次看状态码”作为能力探针。** vLLM 的 POST 会产生实际 reset 尝试且无论内部 `False` 都返回 200；SGLang 的 GET/POST 都有副作用。只读识别应止于 `/version`、`/server_info`、可选 OpenAPI。[vLLM HTTP 语义](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/entrypoints/serve/dev/cache/api_router.py#L20-L43) [SGLang HTTP 语义](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/entrypoints/http_server.py#L905-L920)

- **推断：仅凭路径或单个 `version` 字段不能完全排除反向代理、兼容网关或自定义 middleware 的同名响应。** 应同时校验 HTTP content type、严格 JSON shape 与允许的版本范围；多信号冲突或不足时必须保持 unknown 并 fail closed，不能强猜。官方源码只保证各自原生 server 的上述返回结构。[vLLM shape](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/entrypoints/serve/instrumentator/basic.py#L53-L56) [SGLang shape](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/entrypoints/http_server.py#L743-L765)

- **推断：OpenAPI 缺失是“未知”，不是“不支持”；OpenAPI 存在也只证明当前 HTTP frontend 暴露了 route，不证明所有后端副本版本/能力一致。** 两个运行时都能关闭 schema，且多副本 fan-out 边界均小于任意外部集群范围。[vLLM OpenAPI 开关](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/entrypoints/openai/api_server.py#L171-L178) [SGLang OpenAPI 开关](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/entrypoints/http_server.py#L428-L432)

### 直接对照

| 维度 | vLLM `v0.24.0` | SGLang `v0.5.16` |
| --- | --- | --- |
| 默认可用性 | `/reset_prefix_cache` 是 dev-mode 路由，默认不注册。[证据](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/envs.py#L1288-L1291) | `/flush_cache` 直接注册为 `GET|POST` 管理路由。[证据](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/entrypoints/http_server.py#L905-L920) |
| HTTP 成功语义 | 固定 200，内部 `False` 被吞掉；不能据此证明清理成功。[证据](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/entrypoints/serve/dev/cache/api_router.py#L32-L43) | 本地 `/flush_cache` 用 200/400 映射 `success`；但内部 DP 只取首结果。HiCache storage clear 另有忽略 backend 返回值的问题。[HTTP 映射](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/entrypoints/http_server.py#L905-L920) [首结果](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/tokenizer_control_mixin.py#L275-L289) [HiCache wrapper](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/scheduler.py#L3634-L3642) |
| 运行中请求 | 默认失败但 HTTP 仍 200；可用 `reset_running_requests=true` 强制 preempt/requeue，远端 transfer 场景仍可能抛错。[证据](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/core/sched/scheduler.py#L2148-L2191) | `/flush_cache` 不 preempt；默认立即 400，或用 `timeout>0` 等待完全空闲。另有 pause/retract 流程。[证据](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/scheduler_components/flush_wrapper.py#L24-L65) |
| 外部/L3 cache | `reset_external=true` 调 connector，但未实现 `reset_cache()` 的 connector 默认会被当作未失败。[证据](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/base.py#L691-L703) | `/flush_cache` 与 `/hicache/storage-backend/clear` 是独立路径；后者的 HTTP 200 也不能证明 backend clear 成功。[证据](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/mem_cache/hiradix_cache.py#L811-L831) |
| 多副本 | 内部 DP-LB 会 fan-out；external-DP 与独立服务副本需外部 fan-out。[证据](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/engine/core_client.py#L126-L132) | 同一 server 内按 `dp_size` fan-out；独立 server/P-D 副本需外部 fan-out。[证据](https://github.com/sgl-project/sglang/blob/fdebc938f7f4d16fe6b9f55dcd9a767cf0899ea1/python/sglang/srt/managers/tokenizer_control_mixin.py#L132-L144) |

## Dynamo v1.3.0：发现优先于运行时猜测

### Legacy worker 的共同 capability

- **事实：`python -m dynamo.vllm` 与 `python -m dynamo.sglang` 在 `v1.3.0` 都走各自 legacy `main`，不是 `unified_main`。** [vLLM 入口](https://github.com/ai-dynamo/dynamo/blob/v1.3.0/components/src/dynamo/vllm/__main__.py) [SGLang 入口](https://github.com/ai-dynamo/dynamo/blob/v1.3.0/components/src/dynamo/sglang/__main__.py)

- **事实：两种 legacy worker 都注册 `<namespace>.<component>.clear_kv_blocks` 分布式 endpoint。** vLLM decode/prefill worker 将它绑定到 `handler.clear_kv_blocks`；SGLang decode/prefill worker 也注册同名 endpoint。因此 discovery 中出现该 endpoint 本身就是无副作用的 capability 证据，不需要先猜 engine 类型。[vLLM 注册](https://github.com/ai-dynamo/dynamo/blob/v1.3.0/components/src/dynamo/vllm/worker_factory.py#L386-L401) [vLLM serve](https://github.com/ai-dynamo/dynamo/blob/v1.3.0/components/src/dynamo/vllm/worker_factory.py#L608-L624) [SGLang decode 注册](https://github.com/ai-dynamo/dynamo/blob/v1.3.0/components/src/dynamo/sglang/init_llm.py#L57-L62) [SGLang decode serve](https://github.com/ai-dynamo/dynamo/blob/v1.3.0/components/src/dynamo/sglang/init_llm.py#L146-L169)

- **事实：共同 RPC 的响应 contract 是 `status=success|error`，但内部操作仍按 engine 分流。** vLLM 调 `reset_prefix_cache(reset_connector=True)` 并显式检查精确的 `False`；SGLang 先拒绝 active request，再调 `flush_cache()`，配置 HiCache storage 时还调 `clear_hicache_storage()`。[vLLM handler](https://github.com/ai-dynamo/dynamo/blob/v1.3.0/components/src/dynamo/vllm/handlers.py#L1903-L1915) [SGLang handler](https://github.com/ai-dynamo/dynamo/blob/v1.3.0/components/src/dynamo/sglang/request_handlers/handler_base.py#L821-L874)

- **推断：Dynamo legacy 目标应先按 discovery endpoint capability 分派，而不是先发 vLLM `/version` 或 SGLang `/server_info`。** 对 discovery 返回的每个 endpoint instance 定向调用一次，并逐实例要求 `status=success`；只对没有 `clear_kv_blocks` capability、但有可直连 HTTP system-server 地址的实例，才退回前文的只读 runtime fingerprint。一个 frontend/LB 健康或一次负载均衡 RPC 不能证明全部 instance 已清理。

### Unified 与文档时序风险

- **事实：`v1.3.0` 另有 `dynamo.vllm.unified_main` 和 `dynamo.sglang.unified_main`，两者走 common backend，而不是上述 legacy 注册代码。** [vLLM unified 入口](https://github.com/ai-dynamo/dynamo/blob/v1.3.0/components/src/dynamo/vllm/unified_main.py) [SGLang unified 入口](https://github.com/ai-dynamo/dynamo/blob/v1.3.0/components/src/dynamo/sglang/unified_main.py)

- **版本边界：不要把 newer/latest 文档里的 `/engine/control/clear_kv_blocks` 反推到 `v1.3.0` unified worker。** 对固定 tag 的源码核查没有找到该 route；`v1.3.0` legacy 的可验证接口是 discovery endpoint `clear_kv_blocks`。`/engine/flush_cache` 的 vLLM handler又不检查 `reset_prefix_cache()` 的布尔结果，不能用作“已成功清空”的强证明。[v1.3.0 vLLM flush handler](https://github.com/ai-dynamo/dynamo/blob/v1.3.0/components/src/dynamo/vllm/handlers.py#L1533-L1555)

- **探测顺序结论：** 先读取 discovery 元数据中的 `namespace/component/endpoint/instance_id/transport`，以明确注册的 capability 为准；再区分 legacy 与 unified；最后才对具有直连 HTTP 地址的未知实例做只读 runtime fingerprint。任何 clear 调用都只属于清理阶段，不属于探测阶段。

## 实现前仓库审计（endpoint-benchmark 0.1.1 基线）

### 基线已实现边界

- 请求传输层是通用的 OpenAI-compatible SSE endpoint pool；`EndpointPoolConfig` 只描述 endpoint、header 与 round-robin，没有 runtime/discovery/cache-capability 模型。[models.py](../../src/endpoint_benchmark/models.py#L12-L35) [transport.py](../../src/endpoint_benchmark/transport.py)

- cache clear 实现只有 [cache_control.py](../../src/endpoint_benchmark/cache_control.py#L21-L126) 的 vLLM `POST /reset_prefix_cache`：按 endpoint 的 `scheme://netloc` 去重、并发调用所有唯一 origin，固定携带 `reset_running_requests=false` 与配置的 `reset_external`。仓库没有 `clear_kv`/`clear_kv_blocks` 符号，也没有 SGLang `/flush_cache` 或 Dynamo discovery/RPC adapter。

- reset 生命周期位于 [runner.py](../../src/endpoint_benchmark/runner.py#L40-L149)：startup、warmup 后的 pre、正式运行后的 post，以及请求阶段抛错时的 post-failure。该 runner 自己的请求在这些边界已经停止，但它不会 drain 外部流量，也不能证明后端完全空闲。

- 配置面只有 `PrefixCacheResetConfig`（enabled、timeout、retry interval、reset external），CLI 也只有 vLLM-oriented `--reset-prefix-cache` 等开关。[models.py](../../src/endpoint_benchmark/models.py#L117-L130) [cli.py](../../src/endpoint_benchmark/cli.py#L87-L95)

- 其他 runtime 绑定仍存在：preflight 从第一个 endpoint 推导 vLLM `/tokenize`；metrics 默认也只从第一个 origin 采集，并硬编码 `vllm:` speculative decode 指标。[preflight.py](../../src/endpoint_benchmark/preflight.py#L19-L20) [server_metrics.py](../../src/endpoint_benchmark/server_metrics.py) [runner.py](../../src/endpoint_benchmark/runner.py#L373-L388)

- `pyproject.toml` 没有 vLLM、SGLang 或 Dynamo runtime 依赖；全仓源码、测试与文档检索未发现 `sglang` 或 `dynamo` 集成。[pyproject.toml](../../pyproject.toml)

### 已确认的不兼容与覆盖缺口

1. **vLLM 0.24.0 响应不兼容。** `_reset_one()` 强制把 body 解析为 JSON，并要求存在 `success` 字段；官方 v0.24.0 成功 handler 返回空 200。因此当前实现会把一次已执行的 reset 判为 `invalid reset response`。[本地解析](../../src/endpoint_benchmark/cache_control.py#L57-L109) [上游响应](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/entrypoints/serve/dev/cache/api_router.py#L32-L43)
2. **同 origin 会被折叠。** 多个 API path 指向同一 origin 时只发一次 reset；这适合一个 origin 对应一个 server，却无法证明 origin 后面的 LB replicas 全被覆盖。[去重实现](../../src/endpoint_benchmark/cache_control.py#L27-L34) [现有测试](../../tests/test_cache_control.py#L26-L75)
3. **没有逐实例自动识别。** 当前代码直接构造 vLLM reset URL，没有只读 `/version`、`/server_info` probe，没有 capability cache，也没有 unknown-runtime fail-closed 分支。
4. **成功条件写死为新版 vLLM JSON。** 测试 mock 只覆盖 `{"success": true}` 与 `{"success": false}`，并未覆盖 v0.24.0 空 200、SGLang 200/400 文本或 Dynamo `status` contract。[test_cache_control.py](../../tests/test_cache_control.py#L26-L75)

### 对后续实现的最小事实约束

1. 把“推理入口”与“清理实例来源”分开；一个 LB inference endpoint 不能自动充当实例发现。
2. discovery 返回 capability 时优先用 capability；只有直连 HTTP instance 且没有明确 capability 时，才按 `GET /version` → 未命中后 `GET /server_info` 的顺序只读识别。
3. 每个实例保存 `instance_id/address/runtime/version/capability`，清理结果逐实例记录；第一版以单次 discovery snapshot 为边界，并要求 clear 期间拓扑稳定。
4. 任一实例未知、缺少受支持清理接口、响应不可验证或清理失败，都应 fail closed；不能静默跳过后继续压测。
5. 三条路径必须分别判定：vLLM 接受任意 2xx（只能证明 handler 完成）、SGLang 的固定 flush 请求接受 2xx 并重试 400、Dynamo legacy 要求 `status=success`。不要把它们归一成一个虚构的 `success` JSON contract。

## 线上环境验证记录

> 记录日期：2026-08-06。以下是 `vllm_mooncake` 的环境观察与真实业务数据测试，不是上游产品合同。

### 安全边界

- 用户明确授权终止 GPU 4、5 上自己的 `gpu-card-holder`；测试前按 PID file、process group、GPU UUID 和命令行交叉确认，只终止 `436051`、`436089` 两个占卡进程组。
- GPU 5 上既有 vLLM 服务不属于占卡进程，测试全程未终止；GPU 4 只启动一个可追踪的临时 vLLM，测试结束后停止。
- vLLM 固定使用 `reset_running_requests=false`；只对已证明映射到 GPU 4、5 的两个物理实例清理。
- 业务数据原文件只读；临时适配副本与 SSH tunnel 在测试后删除/关闭。
- 日志只保存时间、实例地址、runtime/version、有限响应摘要与重试结果，不保存认证信息或完整 `/server_info`。

### 实例映射

| 对象 | 观察结果 | 证据边界 |
| --- | --- | --- |
| GPU / 地址 | 运行时与模型 | 映射证据 |
| --- | --- | --- |
| GPU 4 / `127.0.0.1:8004` | 临时 vLLM `0.24.0`，`qwen35-4b`，prefix cache 与 dev route 开启 | 启动命令固定 `CUDA_VISIBLE_DEVICES=4`；独立 process group `446960`；加载后 GPU 4 使用 57,391 MiB；`/version` 和 `/v1/models` 均通过 |
| GPU 5 / `127.0.0.1:8002` | 既有 vLLM `0.26.0`，`qwen35-4b`，prefix cache 与 dev route 开启 | 既有命令固定 `CUDA_VISIBLE_DEVICES=5`；GPU 5 模型显存约 57,193 MiB；`/version` 和 `/v1/models` 均通过 |

本地用两个独立 SSH forward 暴露为 `127.0.0.1:38004`、`127.0.0.1:38002`。因此每个 audit address 对应一个已确认的物理 vLLM server，不把 gateway 或 LB 当作实例。

### 真实业务数据测试

#### 第一轮有界回归（历史）

数据集使用用户指定的 `requests_200_seed42_max8k_openai.jsonl`，共 200 行、约 5.8 MiB；每行输出目标为 8192。为了让测试时间有界，正式测试统一使用 `--max-output-tokens-cap 64 --ignore-eos`，输入不截断，实测输入 token 为 1,191–27,007。

数据中的 `tools` 位于顶层；当时版本的 loader 只读取 `request.tools`，因此曾生成只读临时适配副本。两台既有服务都因未启用 `--enable-auto-tool-choice` / tool parser 在 warmup 返回 HTTP 400；runner 正确地没有进入 clear 或 measurement。为避免为了压测改动既有 GPU 5 服务，第一轮测试直接读取原文件；这个历史结果不代表 tool-calling 压测。后续已修复 loader，并在下面的完整流程中覆盖原始 tools 与图片。

| 运行 | 结果 | cache control 证据 |
| --- | --- | --- |
| `--target dual_vllm`，concurrency 4，8 条 | `8/8`，成功率 100% | 两个实例各一条 pre record，均首次成功 |
| 初次全量，concurrency 1 | `200/200`，成功率 100% | vLLM 0.24/0.26 各一次 pre clear |
| 初次全量，concurrency 8 | `198/200`；两条均为 vLLM 0.24 陈旧 keep-alive 的 `RemoteDisconnected` | clear 本身两实例均成功；默认 100% 门槛令命令返回退出码 3 |
| 修复后全量，concurrency 8 | `200/200`，23.04 秒，8.68 req/s，48,819.54 input tok/s；E2E P95 1,384.86 ms，TTFT P95 845.51 ms | vLLM 0.24 空 body HTTP 200；vLLM 0.26 `{"success":true}` HTTP 200；两者 `attempts=1` |

初次全量测试曾在 response-stage `RemoteDisconnected` 后自动重连并重放 POST；当时重跑中 vLLM 0.24 的 3 次 transport retry 全部恢复，两个 endpoint 各完成 100 条请求。后续审查确认等待响应时无法证明服务端尚未执行请求，当前实现不再自动重放这类失败，而是记录 connection error；回归测试同时断言只发送一次 POST。上述 `200/200` 保留为当时的历史运行结果。

原始结果保留在 `/private/tmp/endpoint-benchmark-cache-control-real/`。测试结束后已关闭 tunnel、停止 GPU 4 临时 vLLM，并重新启动原来的 GPU 4/5 占卡程序；显存恢复为 72,803 / 72,804 MiB，GPU 5 既有 `/version` 仍返回 `0.26.0`。

#### 无截断完整流程

后续测试直接读取原始 200 行数据：loader 将顶层 `tools` 归一到请求体，并把两条含 `<image>` 与顶层 data-URI `images` 的记录转换为 OpenAI `image_url` content。临时服务为 GPU 4 上的 vLLM `0.26.0` / Qwen3.5-4B，启用 auto tool choice、`qwen3_coder` tool parser、prefix cache 与 dev route；GPU 5 的既有服务和占卡程序均未改动。测试不传 `--max-output-tokens-cap` 和 `--ignore-eos`，每个 concurrency 使用 8 条 warmup，正式请求按自然 EOS 或数据中的 8192 上限结束。

精确 `/tokenize` 预检表明输入 token 为 3,335–29,151（均值 7,785.235），输入加请求输出上限为 11,527–37,343；因此临时服务从最初的 32K 调整为 `--max-model-len 65536`，200 条均无 context violation。完整命令在一次不中断的进程中依次运行 concurrency 1 和 8：

| concurrency | 成功 | 时长 | req/s | input tok/s | output tok/s | total tok/s | E2E P95 | TTFT P95 | TPOT P95 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 200/200 | 525.28 s | 0.381 | 2,964.25 | 184.16 | 3,148.42 | 8,110.91 ms | 1,736.25 ms | 4.37 ms |
| 8 | 200/200 | 152.47 s | 1.312 | 10,212.07 | 512.10 | 10,724.17 | 18,544.88 ms | 5,187.72 ms | 13.06 ms |

两轮均无失败、超时或 transport retry。c1 输出 token 均值 483.685、P95 1,572.5、最大 2,674，finish reason 为 114 个 `stop` 和 86 个 `tool_calls`；c8 分别为 390.405、1,333.1、2,191，finish reason 为 105 个 `stop` 和 95 个 `tool_calls`。TTFT 分别有 199/197 个样本，缺失项是只有 tool-call delta、没有文本 output delta 的成功响应。数据没有 temperature，服务默认采样会使两轮实际输出量不同，因此 req/s 和 token/s 是本次真实样本结果，不能把 c1/c8 的输出吞吐差异完全归因于并发。

每轮 warmup 后的 pre clear 都在一次尝试内返回 HTTP 200 与 `{"success":true}`。全流程监控从基线 reset counter 1 开始，观察到 c1 pre clear、c8 pre clear 和压测后手工 final clear，counter 依次到 2、3、4；三次清理时 running/waiting 均为 0，且 2 秒窗口内 KV usage 为 0。由于流式 usage 的 `prompt_tokens_details` 为 `null`，另用同一条 4,485-token 业务请求做 A/B：清理后的首次请求 prefix hit 增量为 0，不清理重复请求命中 4,224 token，再清理后的首次请求命中重新为 0；两次 prefix reset 都返回 `success:true`，MM/encoder reset 都为 HTTP 200，最终状态再次为 idle 且 KV usage 为 0。

结果与证据保存在 `/Users/zhuanzmima0000/Desktop/工作记录/推理优化/压测结果导出/qwen35-4b-full-flow_20260806/`。测试后已复制两次服务日志、停止临时 vLLM、确认 GPU 4 显存先降到 0、关闭 SSH tunnel、删除远端与本地临时文件，并恢复 GPU 4 占卡程序；最终 GPU 4/5 分别使用 72,803 / 72,804 MiB，GPU 5 全程未改动。

本次运行还发现 `run.json.started_at` 在旧实现中取的是结果序列化时间，而非流程入口时间；它不影响基于 monotonic clock 的时长和延迟指标。实现已改为入口捕获并增加回归测试；本次原始 artifact 未事后改写，实际流程时间边界以 cache monitor 的 `2026-08-06T10:35:00Z` 至 `10:49:07Z` 为准。

#### Dynamo、vLLM、SGLang 完整组合矩阵

在同一台 H100 机器上又补跑三组真实组合，没有使用 mock。原始 200 行业务数据不抽样、不设置 `--max-output-tokens-cap`、不设置 `--ignore-eos`；全部行携带 tools，索引 21、46 的 data-URI 图片也保留。每组以 4 条 warmup 后清理、concurrency 1 全量 200 条、再次 warmup 后清理、concurrency 8 全量 200 条的顺序运行。

运行时固定为：Dynamo `1.3.0.post1` + vLLM `0.23.0+cu129` + torch `2.11.0+cu129`，以及 SGLang `0.5.10.post1` + torch `2.9.1+cu129`。机器 driver 是 `550.54.15`；Dynamo 默认 CUDA 13 wheel 和 SGLang 0.5.16 均无法在该 driver 上启动，所以没有降低 Dynamo 需求版本，而是改用两套官方 CUDA 12.9 运行时。SGLang 0.5.10.post1 的实际模型类为 `Qwen3_5ForConditionalGeneration`。

| 组合 | concurrency | 成功 | 时长 | req/s | input tok/s | output tok/s | E2E P95 | TTFT P95 | TPOT P95 | send-stage retry |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Dynamo+vLLM | 1 | 200/200 | 470.11 s | 0.425 | 3,312.09 | 191.37 | 6,527.54 ms | 1,861.99 ms | 4.29 ms | 0 |
| Dynamo+vLLM | 8 | 200/200 | 106.26 s | 1.882 | 14,652.72 | 1,005.24 | 12,995.12 ms | 2,642.92 ms | 7.31 ms | 0 |
| vLLM+SGLang | 1 | 200/200 | 751.20 s | 0.266 | 2,030.95 | 164.20 | 11,991.55 ms | 3,586.53 ms | 5.47 ms | 50 |
| vLLM+SGLang | 8 | 200/200 | 223.10 s | 0.896 | 6,838.51 | 527.27 | 26,739.57 ms | 7,609.17 ms | 12.92 ms | 59 |
| direct SGLang+Dynamo/vLLM | 1 | 200/200 | 788.46 s | 0.254 | 1,934.98 | 162.87 | 13,018.17 ms | 3,717.48 ms | 5.41 ms | 10 |
| direct SGLang+Dynamo/vLLM | 8 | 200/200 | 267.48 s | 0.748 | 5,703.86 | 510.65 | 29,403.48 ms | 7,464.42 ms | 13.25 ms | 29 |

三组共 1,200 条正式请求全部成功。两个双端点组合在每个 concurrency 下都各自分配 100/100；四次 mixed 图片请求也全部成功。vLLM+SGLang 与 direct+Dynamo 的 retry 都是复用 stale keep-alive 时、尚未进入 `getresponse()` 的 send-stage retry；服务端完成记录仍恰好等于正式请求数，没有 response-stage 重放。

| 组合 | 每个 concurrency 的 pre clear | 流程后 final clear |
| --- | --- | --- |
| Dynamo+vLLM | 发现一个 instance；`clear_kv_blocks`、`status=success`、attempt 1 | 同一 instance attempt 1 成功 |
| vLLM+SGLang | vLLM `reset_prefix_cache` HTTP 200；SGLang `flush_cache` HTTP 200；均 attempt 1 | 两个 direct 实例均 attempt 1 成功 |
| direct SGLang+Dynamo/vLLM | SGLang `flush_cache` HTTP 200；发现一个 Dynamo instance 并返回 `status=success`；均 attempt 1 | direct 与 Dynamo 两条路径均 attempt 1 成功 |

Dynamo file discovery 还暴露了真实启动竞态：同一 worker 的 `generate`、`clear_kv_blocks`、`get_perf_metrics` 注册曾有一个或两个在约 12 秒后停止续租。两次不完整状态均被拒绝并保留日志；没有修改或伪造 Dynamo，第三次官方 worker 启动后三个租约持续刷新，才执行 smoke clear 和完整压测。这个问题不会被 benchmark 静默绕过：缺少 `clear_kv_blocks` 时 discovery 得到零实例，pre clear 会 fail closed。生产环境应使用 etcd discovery，不应把本地 file backend 当作高可用控制面。

final clear 后按精确 process group 发送 SIGTERM，Dynamo frontend/worker 和 SGLang 在 5 秒内全部退出；GPU 4 临时 compute process 为零，`8014/8016/8114` 均拒绝连接，Dynamo instance 文件数为 0。随后只恢复 GPU 4 占卡程序，显存约 72,794 MiB；GPU 5 的既有 vLLM 与占卡进程全程未停止。完整结果、逐请求 JSONL、runtime/service 日志、两次 discovery 竞态日志和 cleanup log 保存在 `/Users/zhuanzmima0000/Desktop/工作记录/推理优化/压测结果导出/real-runtime-matrix_20260806/`。

### 未实测边界

| 场景 | 当前证据 |
| --- | --- |
| dev route 未启用、未知 runtime | 自动测试覆盖 fail-closed；真实矩阵只使用已启用受支持 clear capability 的实例 |
| HiCache、Dynamo SGLang worker、Dynamo legacy 多 worker | HiCache 和多 instance fan-out 有自动测试；真实环境覆盖 direct SGLang 与单个 Dynamo/vLLM legacy worker，不向这些拓扑外推 |
| active request、LB/gateway | 保留为已接受部署限制；本次只在 warmup 完成后清理显式 direct 实例和 discovery 返回的 worker |
