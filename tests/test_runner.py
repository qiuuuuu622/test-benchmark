from __future__ import annotations

import csv
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from endpoint_benchmark.models import (
    BenchmarkConfig,
    EndpointPoolConfig,
    LoadConfig,
    MeasurementConfig,
    OutputConfig,
    RequestCase,
    WorkloadConfig,
)
from endpoint_benchmark.runner import (
    _build_payload,
    _effective_metrics_url,
    _metrics_url_source,
    run_benchmark,
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers["Content-Length"])
        self.rfile.read(length)
        if self.path.startswith("/reset_prefix_cache"):
            self.server.reset_count += 1
            body = b'{"success": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            return
        chunks = [
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "hello"}}]},
            {
                "choices": [{"delta": {"content": " world"}}],
            },
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
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

    def test_runs_endpoint_pool_and_writes_results(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.reset_count = 0
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
                    load=LoadConfig(concurrency=(2,)),
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
                self.assertEqual(server.reset_count, 3)
                self.assertEqual(
                    [
                        item["phase"]
                        for item in result.cache_resets_by_concurrency[2]
                    ],
                    ["startup_check", "pre", "post"],
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
                    csv_row = next(csv.DictReader(handle))
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
                    4,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
