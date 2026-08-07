# Summary 增量写入规格

## 背景

当前 benchmark 会在所有 concurrency 档位结束后统一写入结果。运行时间较长时，用户无法查看已完成档位的 summary；后续档位异常也会导致这些 summary 没有落盘。

## 目标

- 每个 concurrency 档位完整结束后，立即更新 `summary.json` 和 `summary.csv`。
- benchmark 启动时创建并打印输出目录，便于运行中查看结果。
- 保持现有文件名、schema、指标含义和最终输出不变。

## 行为

1. benchmark 开始时创建输出目录，CLI 打印其绝对路径。
2. 每个 concurrency 档位生成 summary 后，将所有已完成档位作为累计快照写入 `summary.json` 和 `summary.csv`。
3. 每个文件先写同目录临时文件，再原子替换正式文件，避免读取到半截内容。
4. 只有完整结束的档位进入快照；正在执行或失败的档位不写入。
5. 后续档位异常时，已成功写入的 summary 保留，异常仍按现有方式向上抛出。
6. run 正常结束后，继续生成现有全部结果文件并返回完整的 `BenchmarkResult`。

## 最小实现

- 在现有 runner 的 concurrency 循环中，summary 加入累计列表后立即写入快照。
- 在现有 output 模块中增加一个 summary 快照写入入口，供增量写入和最终写入复用。
- 使用 Python 标准库完成临时文件和原子替换，不增加依赖。
- 保持请求明细、endpoint summary、server metrics、cache reset 等数据的现有内存和最终写入逻辑。

## 验收

- 两个 concurrency 档位运行时，第一个完成后即可读取合法的 `summary.json` 和 `summary.csv`，且只包含第一个档位。
- 第二个档位失败后，第一个档位的 summary 仍然存在且可解析。
- 正常完成后，两个 summary 文件包含全部档位，其他结果文件与当前行为一致。
- 现有 summary schema 和 benchmark 指标测试继续通过。

## 非目标

- 不按请求或定时写入。
- 不增量写入 summary 之外的结果文件。
- 不降低内存占用。
- 不支持中断恢复、运行状态文件或 append-only journal。
- 不保证 `summary.json` 与 `summary.csv` 跨文件事务一致。
