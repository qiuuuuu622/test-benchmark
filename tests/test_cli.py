from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from endpoint_benchmark.cli import _config_from_args, _parser, main


class CliTest(unittest.TestCase):
    def _parse(self, *extra: str):
        return _parser().parse_args(
            [
                "run",
                "--endpoint",
                "http://host:8000/v1/chat/completions",
                "--dataset",
                "unused.jsonl",
                "--concurrency",
                "1",
                *extra,
            ]
        )

    def test_prefix_cache_flags_keep_legacy_defaults(self) -> None:
        reset = _config_from_args(self._parse()).prefix_cache_reset

        self.assertTrue(reset.enabled)
        self.assertEqual(reset.timeout_s, 60.0)
        self.assertEqual(reset.retry_interval_s, 0.5)

    def test_no_reset_prefix_cache_keeps_baseline_mode(self) -> None:
        config = _config_from_args(self._parse("--no-reset-prefix-cache"))

        self.assertFalse(config.isolated_miss)
        self.assertFalse(config.prefix_cache_reset.enabled)

    def test_reset_options_override_defaults(self) -> None:
        config = _config_from_args(
            self._parse(
                "--no-reset-prefix-cache",
                "--prefix-cache-reset-timeout",
                "9",
                "--prefix-cache-reset-interval",
                "0",
                "--reset-external-prefix-cache",
            )
        )

        reset = config.prefix_cache_reset
        self.assertFalse(reset.enabled)
        self.assertEqual(reset.timeout_s, 9.0)
        self.assertEqual(reset.retry_interval_s, 0.0)
        self.assertTrue(reset.reset_external)

    def test_isolated_miss_disables_reset(self) -> None:
        config = _config_from_args(self._parse("--isolated-miss"))

        self.assertTrue(config.isolated_miss)
        self.assertFalse(config.prefix_cache_reset.enabled)

    def test_isolated_miss_rejects_explicit_reset_options(self) -> None:
        args = self._parse(
            "--isolated-miss",
            "--reset-prefix-cache",
            "--prefix-cache-reset-timeout",
            "9",
            "--prefix-cache-reset-interval",
            "0",
            "--reset-external-prefix-cache",
        )

        with self.assertRaisesRegex(ValueError, "cannot be combined") as raised:
            _config_from_args(args)

        message = str(raised.exception)
        for option in (
            "--reset-prefix-cache",
            "--prefix-cache-reset-timeout",
            "--prefix-cache-reset-interval",
            "--reset-external-prefix-cache",
        ):
            self.assertIn(option, message)

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
