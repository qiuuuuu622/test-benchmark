from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from endpoint_benchmark.cli import main


class CliTest(unittest.TestCase):
    def test_invalid_completed_run_returns_distinct_exit_code(self) -> None:
        summary = {
            "concurrency": 1,
            "successful_requests": 0,
            "requested_requests": 1,
            "throughput": {
                "requests_per_second": 0,
                "input_tokens_per_second": None,
                "output_tokens_per_second": None,
                "total_tokens_per_second": None,
            },
            "e2e_latency_ms": {"p95": None},
            "ttft_ms": {"p95": None},
            "tpot_ms": {"p95": None},
            "inter_chunk_latency_ms": {"p95": None},
            "server_metrics": None,
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "endpoint_benchmark.cli.run_benchmark",
            return_value=SimpleNamespace(
                summaries=[summary],
                output_directory=Path(directory),
                valid=False,
            ),
        ):
            exit_code = main(
                [
                    "run",
                    "--endpoint",
                    "http://host:8000/v1/chat/completions",
                    "--dataset",
                    "unused.jsonl",
                    "--concurrency",
                    "1",
                ]
            )

        self.assertEqual(exit_code, 3)


if __name__ == "__main__":
    unittest.main()
