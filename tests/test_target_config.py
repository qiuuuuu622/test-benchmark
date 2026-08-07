from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from endpoint_benchmark.target_config import (
    DirectCacheSource,
    DynamoCacheSource,
    load_target,
)


class TargetConfigTest(unittest.TestCase):
    def test_loads_mixed_target_and_resolves_header_environment(self) -> None:
        value = """
[targets.mixed]
inference_endpoints = ["http://gateway:8080/v1/chat/completions"]
headers = { X-Tenant = "benchmark" }
header_env = { Authorization = "INFERENCE_AUTH_HEADER" }
clear_timeout_s = 12
clear_retry_interval_s = 0.25

[[targets.mixed.cache_sources]]
kind = "direct"
endpoints = ["http://vllm:8000", "http://sglang:30000"]
header_env = { Authorization = "ADMIN_AUTH_HEADER" }

[[targets.mixed.cache_sources]]
kind = "dynamo"
namespace = "benchmark"
components = ["VllmWorker", "SglangWorker"]
"""
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "INFERENCE_AUTH_HEADER": "Bearer inference-secret",
                "ADMIN_AUTH_HEADER": "Bearer admin-secret",
            },
        ):
            path = Path(directory) / "targets.toml"
            path.write_text(value, encoding="utf-8")
            target = load_target(path, "mixed")

        self.assertEqual(target.clear_timeout_s, 12.0)
        self.assertEqual(
            dict(target.headers)["Authorization"], "Bearer inference-secret"
        )
        direct, dynamo = target.cache_sources
        self.assertIsInstance(direct, DirectCacheSource)
        self.assertEqual(
            dict(direct.headers or ())["Authorization"], "Bearer admin-secret"
        )
        self.assertIsInstance(dynamo, DynamoCacheSource)
        self.assertEqual(dynamo.components, ("VllmWorker", "SglangWorker"))

    def test_rejects_unknown_fields(self) -> None:
        value = """
[targets.bad]
inference_endpoints = ["http://host:8000/v1"]
surprise = true

[[targets.bad.cache_sources]]
kind = "direct"
use_inference_endpoints = true
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.toml"
            path.write_text(value, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown fields.*surprise"):
                load_target(path, "bad")

    def test_rejects_missing_header_environment_variable(self) -> None:
        value = """
[targets.bad]
inference_endpoints = ["http://host:8000/v1"]
header_env = { Authorization = "DEFINITELY_MISSING_TARGET_SECRET" }

[[targets.bad.cache_sources]]
kind = "direct"
use_inference_endpoints = true
"""
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {}, clear=True
        ):
            path = Path(directory) / "targets.toml"
            path.write_text(value, encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "DEFINITELY_MISSING_TARGET_SECRET"
            ):
                load_target(path, "bad")

    def test_rejects_header_control_characters_without_echoing_secret(self) -> None:
        value = """
[targets.bad]
inference_endpoints = ["http://host:8000/v1"]
header_env = { Authorization = "INFERENCE_AUTH_HEADER" }

[[targets.bad.cache_sources]]
kind = "direct"
use_inference_endpoints = true
"""
        secret = "Bearer do-not-log\n"
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"INFERENCE_AUTH_HEADER": secret}, clear=True
        ):
            path = Path(directory) / "targets.toml"
            path.write_text(value, encoding="utf-8")
            with self.assertRaises(ValueError) as raised:
                load_target(path, "bad")

        self.assertNotIn(secret.strip(), str(raised.exception))


if __name__ == "__main__":
    unittest.main()
