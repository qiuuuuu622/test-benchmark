from __future__ import annotations

import csv
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from endpoint_benchmark.cache_control import CacheControlError
from endpoint_benchmark.models import (
    BenchmarkConfig,
    EndpointPoolConfig,
    LoadConfig,
    MeasurementConfig,
    OutputConfig,
    PrefixCacheResetConfig,
    RequestCase,
    WorkloadConfig,
)
from endpoint_benchmark.runner import (
    _build_payload,
    _effective_metrics_url,
    _fetch_metrics,
    _metrics_url_source,
    _redacted_configuration,
    _run_metadata,
    run_benchmark,
)
from endpoint_benchmark.target_config import DirectCacheSource, TargetConfig


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self.server.probe_paths.append(self.path)
        if self.path == "/version" and self.server.known_runtime:
            body = b'{"version":"0.24.0"}'
            self.send_response(200)
        else:
            body = b'{"unexpected":true}'
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if self.path.startswith("/reset_prefix_cache"):
            self.server.reset_count += 1
            self.server.reset_paths.append(self.path)
            self.server.events.append("reset")
            body = b""
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            return
        self.server.request_count += 1
        self.server.events.append("request")
        chunks = [
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "hello"}}]},
            {
                "choices": [{"delta": {"content": " world"}}],
            },
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
            },
        ]
        body = b"".join(
            f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks
        ) + b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def log_message(self, format: str, *args: object) -> None:
        pass


class RunnerTest(unittest.TestCase):
    def test_run_metadata_uses_captured_start_time(self) -> None:
        config = BenchmarkConfig(
            endpoint_pool=EndpointPoolConfig(endpoints=("http://example.test/v1",)),
            workload=WorkloadConfig(dataset=Path("unused.jsonl")),
            load=LoadConfig(concurrency=(1,)),
        )
        target = TargetConfig(
            inference_endpoints=("http://example.test/v1",),
            cache_sources=(DirectCacheSource(use_inference_endpoints=True),),
        )

        metadata = _run_metadata(config, target, [], "captured-start")

        self.assertEqual(metadata["started_at"], "captured-start")

    def test_metrics_url_defaults_to_first_endpoint_origin(self) -> None:
        config = BenchmarkConfig(
            endpoint_pool=EndpointPoolConfig(
                endpoints=(
                    "http://host-a:8000/v1/chat/completions",
                    "http://host-b:8000/v1/chat/completions",
                )
            ),
            workload=WorkloadConfig(dataset=Path("unused.jsonl")),
            load=LoadConfig(concurrency=(1,)),
        )

        self.assertEqual(
            _effective_metrics_url(config),
            "http://host-a:8000/metrics",
        )
        self.assertEqual(
            _metrics_url_source(config),
            "derived_from_first_endpoint",
        )

    def test_invalid_metrics_port_remains_a_nonfatal_warning(self) -> None:
        warnings: list[str] = []
        url = "http://user:do-not-log@host:bad/metrics?token=do-not-log"
        with patch(
            "endpoint_benchmark.runner.fetch_metrics",
            side_effect=ValueError("invalid port"),
        ):
            snapshot = _fetch_metrics(url, warnings, "before")

        self.assertIsNone(snapshot)
        self.assertEqual(
            warnings,
            ["metrics before fetch failed for <invalid-url>: ValueError"],
        )
        self.assertNotIn("do-not-log", warnings[0])

    def test_cli_workload_fields_override_request_row(self) -> None:
        config = BenchmarkConfig(
            endpoint_pool=EndpointPoolConfig(endpoints=("http://example.test/v1",)),
            workload=WorkloadConfig(
                dataset=Path("unused.jsonl"),
                model="cli-model",
                temperature=0.0,
                max_output_tokens=64,
                max_output_tokens_cap=32,
                ignore_eos=True,
            ),
            load=LoadConfig(concurrency=(1,)),
        )
        case = RequestCase(
            request_id="case",
            messages=[{"role": "user", "content": "hi"}],
            request={
                "model": "row-model",
                "temperature": 0.8,
                "max_output_tokens": 128,
                "ignore_eos": False,
            },
            metadata={},
        )

        payload = _build_payload(config, case)

        self.assertEqual(payload["model"], "cli-model")
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["max_tokens"], 32)
        self.assertTrue(payload["ignore_eos"])

    def test_configuration_redacts_url_and_header_secrets(self) -> None:
        endpoint = (
            "http://user:pass@host:8000/v1/chat"
            "?token=endpoint-query-secret#fragment"
        )
        headers = (("Authorization", "Bearer inference-secret"),)
        target = TargetConfig(
            inference_endpoints=(endpoint,),
            headers=headers,
            cache_sources=(
                DirectCacheSource(
                    endpoints=(
                        "http://admin:pw@cache:8000/control?token=cache#fragment",
                    ),
                    headers=(("X-Admin", "cache-secret"),),
                ),
            ),
        )
        config = BenchmarkConfig(
            endpoint_pool=EndpointPoolConfig(
                endpoints=(endpoint,),
                headers=headers,
            ),
            workload=WorkloadConfig(dataset=Path("unused.jsonl")),
            load=LoadConfig(concurrency=(1,)),
            measurement=MeasurementConfig(
                metrics_url=(
                    "http://metrics-user:metrics-pass@host:9090/metrics"
                    "?token=metrics#fragment"
                )
            ),
            target=target,
        )

        serialized = json.dumps(_redacted_configuration(config, target))

        for secret in (
            "user",
            "pass",
            "endpoint-query-secret",
            "admin",
            "cache-secret",
            "inference-secret",
            "metrics-user",
            "metrics-pass",
            "token=metrics",
            "fragment",
        ):
            self.assertNotIn(secret, serialized)
        self.assertIn("Authorization", serialized)
        self.assertIn("X-Admin", serialized)

    def test_runs_endpoint_pool_and_writes_results(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.reset_count = 0
        server.request_count = 0
        server.reset_paths = []
        server.probe_paths = []
        server.events = []
        server.known_runtime = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                dataset = root / "workload.jsonl"
                dataset.write_text(
                    "\n".join(
                        json.dumps(
                            {
                                "id": index,
                                "messages": [
                                    {"role": "user", "content": f"prompt {index}"}
                                ],
                            }
                        )
                        for index in range(4)
                    )
                    + "\n",
                    encoding="utf-8",
                )
                base = f"http://127.0.0.1:{server.server_port}"
                output = root / "results"
                config = BenchmarkConfig(
                    endpoint_pool=EndpointPoolConfig(
                        endpoints=(f"{base}/a", f"{base}/b")
                    ),
                    workload=WorkloadConfig(dataset=dataset, model="test"),
                    load=LoadConfig(concurrency=(1, 2), warmup_prompts=1),
                    measurement=MeasurementConfig(server_metrics_enabled=False),
                    output=OutputConfig(directory=output),
                )

                result = run_benchmark(config)

                summary = result.summaries[0]
                self.assertEqual(summary["successful_requests"], 4)
                self.assertEqual(
                    summary["routing"]["dispatch_count"],
                    {"endpoint-0": 2, "endpoint-1": 2},
                )
                self.assertEqual(summary["ttft_ms"]["samples"], 4)
                self.assertEqual(summary["tpot_ms"]["samples"], 4)
                self.assertEqual(summary["inter_chunk_latency_ms"]["samples"], 4)
                self.assertTrue(summary["throughput"]["token_counts_complete"])
                self.assertEqual(
                    result.requests_by_concurrency[1][0].cached_input_tokens,
                    0,
                )
                self.assertEqual(server.reset_count, 5)
                self.assertEqual(server.request_count, 10)
                self.assertEqual(server.events[:3], ["reset", "request", "reset"])
                self.assertTrue(
                    all("reset_external=false" in path for path in server.reset_paths)
                )
                self.assertNotIn("/server_info", server.probe_paths)
                self.assertEqual(
                    [
                        item["phase"]
                        for item in result.cache_resets_by_concurrency[1]
                    ],
                    ["startup_check", "pre", "post"],
                )
                self.assertEqual(
                    [
                        item["phase"]
                        for item in result.cache_resets_by_concurrency[2]
                    ],
                    ["pre", "post"],
                )
                self.assertEqual(
                    result.cache_resets_by_concurrency[2][0]["instances"][0][
                        "runtime"
                    ],
                    "vllm",
                )
                run_output = result.output_directory
                self.assertEqual(run_output.parent, output)
                self.assertTrue(run_output.name.startswith("benchmark_"))
                self.assertTrue((run_output / "summary.json").is_file())
                self.assertTrue((run_output / "cache_resets.json").is_file())
                self.assertTrue((run_output / "endpoint_summary.csv").is_file())
                self.assertTrue((run_output / "endpoint_summary.json").is_file())
                with (run_output / "summary.csv").open(
                    newline="", encoding="utf-8"
                ) as handle:
                    csv_rows = list(csv.DictReader(handle))
                    csv_row = csv_rows[0]
                self.assertEqual(len(csv_rows), 2)
                self.assertEqual(
                    len(json.loads((run_output / "summary.json").read_text())),
                    2,
                )
                for metric in (
                    "e2e_latency_ms",
                    "ttft_ms",
                    "tpot_ms",
                    "inter_chunk_latency_ms",
                ):
                    for statistic in ("mean", "p95", "p99"):
                        self.assertIn(f"{metric}_{statistic}", csv_row)
                self.assertEqual(
                    len((run_output / "requests.jsonl").read_text().splitlines()),
                    8,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_disabled_cache_reset_skips_the_complete_lifecycle(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.reset_count = 0
        server.request_count = 0
        server.reset_paths = []
        server.probe_paths = []
        server.events = []
        server.known_runtime = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                dataset = root / "workload.jsonl"
                dataset.write_text(
                    '{"messages":[{"role":"user","content":"hello"}]}\n',
                    encoding="utf-8",
                )
                base = f"http://127.0.0.1:{server.server_port}"
                config = BenchmarkConfig(
                    endpoint_pool=EndpointPoolConfig(endpoints=(f"{base}/v1",)),
                    workload=WorkloadConfig(dataset=dataset, model="test"),
                    load=LoadConfig(concurrency=(1,), warmup_prompts=1),
                    measurement=MeasurementConfig(server_metrics_enabled=False),
                    prefix_cache_reset=PrefixCacheResetConfig(enabled=False),
                    output=OutputConfig(directory=root / "results"),
                )

                result = run_benchmark(config)

                self.assertEqual(server.request_count, 2)
                self.assertEqual(server.reset_count, 0)
                self.assertEqual(server.probe_paths, [])
                self.assertEqual(result.cache_resets_by_concurrency, {1: []})
                self.assertFalse(
                    result.run["configuration"]["prefix_cache_reset_enabled"]
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_cache_failure_prevents_measured_workload(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.reset_count = 0
        server.request_count = 0
        server.reset_paths = []
        server.probe_paths = []
        server.events = []
        server.known_runtime = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                dataset = root / "workload.jsonl"
                dataset.write_text(
                    '{"messages":[{"role":"user","content":"hello"}]}\n',
                    encoding="utf-8",
                )
                base = f"http://127.0.0.1:{server.server_port}"
                config = BenchmarkConfig(
                    endpoint_pool=EndpointPoolConfig(endpoints=(f"{base}/v1",)),
                    workload=WorkloadConfig(dataset=dataset, model="test"),
                    load=LoadConfig(concurrency=(1,)),
                    measurement=MeasurementConfig(server_metrics_enabled=False),
                    output=OutputConfig(directory=root / "results"),
                )

                with self.assertRaises(CacheControlError):
                    run_benchmark(config)

                self.assertEqual(server.request_count, 0)
                self.assertEqual(server.reset_count, 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
