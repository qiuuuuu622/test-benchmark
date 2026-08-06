# endpoint-benchmark

A zero-runtime-dependency benchmark client for OpenAI-compatible streaming
endpoints. It sends one workload to an endpoint pool, routes requests with
round-robin, and calculates client-observed TTFT, TPOT, latency, and token
throughput from one shared measurement pool.

The project is currently an alpha implementation of the contracts in:

- `vllm-benchmark-architecture.md`
- `vllm-benchmark-interface-contract.md`

## Development install

```bash
python -m pip install -e .
```

## Example

```bash
endpoint-benchmark run \
  --endpoint http://host-a:8000/v1/chat/completions \
  --endpoint http://host-b:8000/v1/chat/completions \
  --model served-model \
  --dataset workload.jsonl \
  --concurrency 1 8 32 \
  --output-dir results \
  --label run-001
```

This creates a timestamped run directory such as
`results/run-001_20260805_213045/`.

Prefix-cache reset is enabled before and after every concurrency level by
default. Start vLLM with `VLLM_SERVER_DEV_MODE=1`, or explicitly opt out with
`--no-reset-prefix-cache`.
The runner checks this endpoint before any warmup and fails immediately with a
clear dev-mode error if it is unavailable.
