from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from endpoint_benchmark.cli import _config_from_args, _parse_header, _parser, main


class CliTest(unittest.TestCase):
    def test_prefix_cache_flags_keep_legacy_defaults(self) -> None:
        args = _parser().parse_args(
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

        reset = _config_from_args(args).prefix_cache_reset

        self.assertTrue(reset.enabled)
        self.assertEqual(reset.timeout_s, 60.0)
        self.assertEqual(reset.retry_interval_s, 0.5)
        self.assertFalse(reset.reset_external)

    def test_prefix_cache_flags_override_defaults(self) -> None:
        args = _parser().parse_args(
            [
                "run",
                "--endpoint",
                "http://host:8000/v1/chat/completions",
                "--dataset",
                "unused.jsonl",
                "--concurrency",
                "1",
                "--no-reset-prefix-cache",
                "--prefix-cache-reset-timeout",
                "9",
                "--prefix-cache-reset-interval",
                "0",
                "--reset-external-prefix-cache",
            ]
        )

        reset = _config_from_args(args).prefix_cache_reset

        self.assertFalse(reset.enabled)
        self.assertEqual(reset.timeout_s, 9.0)
        self.assertEqual(reset.retry_interval_s, 0.0)
        self.assertTrue(reset.reset_external)

    def test_invalid_header_does_not_echo_its_value(self) -> None:
        secret = "do-not-echo-this"
        with self.assertRaises(ValueError) as raised:
            _parse_header(secret)

        self.assertNotIn(secret, str(raised.exception))

    def test_target_file_supplies_inference_endpoint_and_cache_sources(self) -> None:
        value = """
[targets.local]
inference_endpoints = ["http://host:8000/v1/chat/completions"]
clear_timeout_s = 12
clear_retry_interval_s = 0.25

[[targets.local.cache_sources]]
kind = "direct"
use_inference_endpoints = true
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.toml"
            path.write_text(value, encoding="utf-8")
            args = _parser().parse_args(
                [
                    "run",
                    "--target",
                    "local",
                    "--targets-file",
                    str(path),
                    "--dataset",
                    "unused.jsonl",
                    "--concurrency",
                    "1",
                ]
            )
            config = _config_from_args(args)

        self.assertEqual(
            config.endpoint_pool.endpoints,
            ("http://host:8000/v1/chat/completions",),
        )
        self.assertIsNotNone(config.target)
        self.assertEqual(config.prefix_cache_reset.timeout_s, 12.0)
        self.assertEqual(config.prefix_cache_reset.retry_interval_s, 0.25)

    def test_target_rejects_cli_header_override(self) -> None:
        args = _parser().parse_args(
            [
                "run",
                "--target",
                "local",
                "--header",
                "Authorization=secret",
                "--dataset",
                "unused.jsonl",
                "--concurrency",
                "1",
            ]
        )
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            _config_from_args(args)

    def test_target_and_endpoint_are_mutually_exclusive(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            _parser().parse_args(
                [
                    "run",
                    "--target",
                    "local",
                    "--endpoint",
                    "http://host:8000/v1",
                    "--dataset",
                    "unused.jsonl",
                    "--concurrency",
                    "1",
                ]
            )

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
