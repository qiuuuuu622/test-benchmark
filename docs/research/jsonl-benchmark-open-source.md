# JSONL 压测数据统一解析：开源项目调研

## 结论

本仓库应继续以 `RequestCase` 作为唯一 canonical model，在数据集加载 seam 内按来源格式选择 adapter。runner、preflight 与 transport 只接收 `RequestCase`，不感知 ShareGPT、业务字段名或历史 alias。

推荐的最小接口是：

```python
load_dataset(path, load, dataset_format="auto") -> DatasetLoadResult
```

`DatasetLoadResult` 至少包含 `cases`、最终采用的 `format` 和结构化 warnings。adapter 用普通函数和字典注册即可，不需要继承体系或插件框架。

## 一手来源与可借鉴做法

### JSON Lines

[JSON Lines 官方格式说明](https://jsonlines.org/)要求 UTF-8、每行一个合法 JSON value，并建议最后一行也保留换行符。它强调逐 record 处理与 Unix pipeline 兼容性，因此一行应当是一个可独立解析的压测请求；不要把文件头伪装成第一条特殊 record。

### vLLM Benchmarks

[vLLM benchmark dataset 文档](https://docs.vllm.ai/en/stable/api/vllm/benchmarks/datasets/datasets/)为 ShareGPT、Hugging Face、自定义 JSONL、随机数据、多模态等来源提供不同 dataset 实现，但最终统一产出 `SampleRequest`。`SampleRequest` 将 chat messages、request overrides、request ID 和预期输出长度集中为一个内部模型。

[vLLM bench serve CLI](https://docs.vllm.ai/en/latest/cli/bench/serve/)使用显式 `--dataset-name` 选择 `sharegpt`、`hf`、`custom` 等格式。这支持本仓库增加显式 `--dataset-format`，把自动识别仅作为便利性兜底。

### NVIDIA AIPerf

[AIPerf Raw Payload Replay](https://docs.nvidia.com/aiperf/tutorials/datasets-inputs/raw-payload-replay)把每个 JSONL object 视为一个完整请求；自动识别只查看首个非空 record，并在字段可能与其他格式冲突时要求显式指定 `--custom-dataset-type raw_payload`。这说明格式应按文件确定，歧义必须失败，不能逐行猜测。

[AIPerf Custom Dataset Guide](https://docs.nvidia.com/aiperf/tutorials/datasets-inputs/custom-dataset-guide)把 single-turn、multi-turn、random pool 和 trace replay 分成不同 dataset types，因为它们不只是字段差异，还具有不同调度语义。本仓库当前 v1 应只支持“一行一次独立请求”；真实多轮或 trace replay 应使用新 schema 和 scheduler，而不是塞进现有 adapter。

### MLPerf LoadGen

[MLPerf LoadGen README](https://github.com/mlcommons/inference/blob/master/loadgen/README.md)明确让 LoadGen 对模型和输入输出数据格式保持无感：benchmark/QSL 负责数据加载和预处理，LoadGen 只生成 sample ID 和流量。对应到本仓库，runner 不应出现任何业务方字段判断；所有差异必须在 dataset adapter 结束。

### lm-evaluation-harness

[lm-evaluation-harness Task Guide](https://github.com/EleutherAI/lm-evaluation-harness/blob/main/docs/task_guide.md)通过 `process_docs`、`doc_to_text`、`doc_to_target` 等配置把不同原始 dataset record 映射为统一执行语义，而不是要求所有来源先使用相同字段。它同时把 task metadata/version 与数据映射配置分开，说明来源格式版本和结果 schema 版本应分别记录。

### OpenAI Evals 与 Promptfoo

[OpenAI Evals 构建指南](https://github.com/openai/evals/blob/main/docs/build-eval.md)采用一行一个 sample，统一使用 `input`，按 evaluator 再增加 `ideal` 等字段，并要求变更 eval 时提升版本。[Promptfoo test case 文档](https://www.promptfoo.dev/docs/configuration/test-cases/)将输入变量、断言和 metadata 分开。对本仓库最有用的原则是：请求 payload 与不发送给服务端的分析信息必须分区；纯性能压测不需要把质量断言纳入 v1 canonical schema。

## 对本仓库的直接建议

1. 新 canonical record 使用 `schema`、`id`、`messages`、`request`、`metadata` 五个顶层字段；新数据要求稳定且文件内唯一的 `id`。
2. `request` 只表达单请求 payload override；`metadata` 永不发送。并发、请求率、随机种子等 run 配置不写进每条 record。
3. 首批 adapter 只实现真实存在的格式，例如 `canonical-v1`、`openai-payload-v1`、`sharegpt-v1` 和一个已确认的业务格式。不要先造通用 JSONPath 映射 DSL。
4. 选择顺序为：显式 CLI 格式 > record 的精确 schema 标记 > 首个非空 record 的无歧义 shape detection。整份文件固定一个 adapter；混合来源应先转换为 canonical JSONL。
5. alias 同时出现且值冲突、格式歧义、重复 ID、未知 schema version、缺少 messages 都应报带路径、行号、格式和字段的错误；legacy 转换才记 warning。
6. `run.json` 的 dataset metadata 记录来源格式、canonical schema version、记录数、warnings；必要时再加文件 hash 以支持可复现回放。
7. 运行时继续只用 Python 标准库做针对性校验。可以发布 JSON Schema 给业务方和 CI 使用，但不必为此增加运行时依赖。
