from __future__ import annotations

import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from endpoint_benchmark.cache_control import PrefixCacheResetError, reset_prefix_caches
from endpoint_benchmark.models import PrefixCacheResetConfig


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def read(self) -> bytes:
        return self._body


class CacheControlTest(unittest.TestCase):
    def test_accepts_empty_vllm_success_response(self) -> None:
        with patch(
            "endpoint_benchmark.cache_control.urllib.request.urlopen",
            return_value=FakeResponse(b""),
        ):
            record = reset_prefix_caches(
                endpoints=("http://host:8000/v1/chat/completions",),
                headers={},
                config=PrefixCacheResetConfig(),
                concurrency=1,
                phase="startup_check",
            )

        self.assertTrue(record["success"])
        self.assertEqual(record["engines"][0]["attempts"], 1)

    def test_reports_missing_vllm_dev_mode_on_404(self) -> None:
        error = HTTPError(
            url="http://host:8000/reset_prefix_cache",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=BytesIO(b'{"detail":"Not Found"}'),
        )
        with patch(
            "endpoint_benchmark.cache_control.urllib.request.urlopen",
            side_effect=error,
        ), self.assertRaisesRegex(
            PrefixCacheResetError,
            "VLLM_SERVER_DEV_MODE=1",
        ):
            reset_prefix_caches(
                endpoints=("http://host:8000/v1/chat/completions",),
                headers={},
                config=PrefixCacheResetConfig(),
                concurrency=1,
                phase="startup_check",
            )

    def test_deduplicates_engine_origin_and_retries_false_response(self) -> None:
        responses = [
            FakeResponse(b'{"success": false}'),
            FakeResponse(b'{"success": true}'),
        ]
        with patch(
            "endpoint_benchmark.cache_control.urllib.request.urlopen",
            side_effect=responses,
        ) as urlopen:
            record = reset_prefix_caches(
                endpoints=(
                    "http://host:8000/v1/chat/completions",
                    "http://host:8000/v1/completions",
                ),
                headers={},
                config=PrefixCacheResetConfig(retry_interval_s=0),
                concurrency=8,
                phase="post",
            )

        self.assertEqual(record["engine_count"], 1)
        self.assertEqual(record["engines"][0]["attempts"], 2)
        self.assertEqual(urlopen.call_count, 2)
        request = urlopen.call_args.args[0]
        self.assertIn("reset_running_requests=false", request.full_url)
        self.assertIn("reset_external=false", request.full_url)


if __name__ == "__main__":
    unittest.main()
