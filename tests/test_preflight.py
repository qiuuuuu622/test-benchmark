from __future__ import annotations

import json
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from endpoint_benchmark.models import (
    BenchmarkConfig,
    EndpointPoolConfig,
    LoadConfig,
    PreflightConfig,
    RequestCase,
    WorkloadConfig,
)
from endpoint_benchmark.preflight import run_token_preflight


class FakeResponse:
    def __init__(self, value: dict[str, int]) -> None:
        self._body = BytesIO(json.dumps(value).encode())

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def read(self) -> bytes:
        return self._body.read()


def config(max_model_len: int = 128) -> BenchmarkConfig:
    return BenchmarkConfig(
        endpoint_pool=EndpointPoolConfig(
            endpoints=("http://host:8000/v1/chat/completions",)
        ),
        workload=WorkloadConfig(dataset=Path("unused"), model="served-model"),
        load=LoadConfig(concurrency=(1,)),
        preflight=PreflightConfig(tokenize=True, max_model_len=max_model_len),
    )


class PreflightTest(unittest.TestCase):
    def test_reports_real_token_distribution(self) -> None:
        case = RequestCase(
            request_id="one",
            messages=[{"role": "user", "content": "hello"}],
            request={"max_output_tokens": 16},
            metadata={},
        )
        with patch(
            "endpoint_benchmark.preflight.urllib.request.urlopen",
            return_value=FakeResponse({"count": 32, "max_model_len": 256}),
        ) as urlopen:
            result = run_token_preflight(config(), [case], {})

        self.assertEqual(result["input_tokens"]["max"], 32.0)
        self.assertEqual(result["requested_total_tokens"]["max"], 48.0)
        self.assertEqual(urlopen.call_args.args[0].full_url, "http://host:8000/tokenize")

    def test_rejects_context_overflow(self) -> None:
        case = RequestCase(
            request_id="too-long",
            messages=[{"role": "user", "content": "hello"}],
            request={"max_output_tokens": 64},
            metadata={},
        )
        with patch(
            "endpoint_benchmark.preflight.urllib.request.urlopen",
            return_value=FakeResponse({"count": 100}),
        ), self.assertRaisesRegex(ValueError, "context-length violations"):
            run_token_preflight(config(128), [case], {})


if __name__ == "__main__":
    unittest.main()
