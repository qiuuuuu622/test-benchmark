from __future__ import annotations

import contextlib
import io
import json
import unittest
from collections.abc import AsyncIterator
from unittest.mock import patch

from endpoint_benchmark.cache_control import CacheControlError, clear_caches
from endpoint_benchmark.target_config import (
    DirectCacheSource,
    DynamoCacheSource,
    TargetConfig,
)


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]


def direct_target(
    *endpoints: str,
    headers: tuple[tuple[str, str], ...] = (),
) -> TargetConfig:
    return TargetConfig(
        inference_endpoints=endpoints,
        headers=headers,
        cache_sources=(DirectCacheSource(use_inference_endpoints=True),),
        clear_retry_interval_s=0,
    )


class CacheControlTest(unittest.TestCase):
    def test_vllm_accepts_empty_and_success_2xx_without_sglang_probe(self) -> None:
        secret = "Bearer result-secret"
        for status, body in (
            (200, b""),
            (204, b""),
            (200, b'{"success":true}'),
        ):
            calls: list[str] = []

            def urlopen(
                request: object,
                timeout: float,
                *,
                expected_status: int = status,
                expected_body: bytes = body,
                seen: list[str] = calls,
            ) -> FakeResponse:
                del timeout
                url = request.full_url
                seen.append(url)
                if url.endswith("/version"):
                    return FakeResponse(200, b'{"version":"0.24.0"}')
                return FakeResponse(expected_status, expected_body)

            with self.subTest(status=status), patch(
                "endpoint_benchmark.cache_control.urllib.request.urlopen",
                side_effect=urlopen,
            ), contextlib.redirect_stderr(io.StringIO()):
                record = clear_caches(
                    direct_target(
                        "http://host:8000/v1/chat/completions",
                        headers=(("Authorization", secret),),
                    ),
                    1,
                    reset_external=True,
                )

            instance = record["instances"][0]
            self.assertEqual(instance["status_code"], status)
            self.assertNotIn(secret, instance["response"])
            self.assertFalse(any("server_info" in url for url in calls))
            self.assertIn("reset_external=true", calls[-1])

    def test_vllm_retries_2xx_when_reset_reports_false(self) -> None:
        reset_calls = 0

        def urlopen(request: object, timeout: float) -> FakeResponse:
            nonlocal reset_calls
            del timeout
            if request.full_url.endswith("/version"):
                return FakeResponse(200, b'{"version":"0.26.0"}')
            reset_calls += 1
            body = b'{"success":false}' if reset_calls == 1 else b'{"success":true}'
            return FakeResponse(200, body)

        with patch(
            "endpoint_benchmark.cache_control.urllib.request.urlopen",
            side_effect=urlopen,
        ), contextlib.redirect_stderr(io.StringIO()):
            record = clear_caches(direct_target("http://host:8000/v1"), 1)

        self.assertEqual(reset_calls, 2)
        self.assertEqual(record["instances"][0]["attempts"], 2)

    def test_sglang_retries_busy_flush_and_clears_hicache_storage(self) -> None:
        flush_calls = 0
        calls: list[str] = []

        def urlopen(request: object, timeout: float) -> FakeResponse:
            nonlocal flush_calls
            del timeout
            url = request.full_url
            calls.append(url)
            if url.endswith("/version"):
                return FakeResponse(404, b"not found")
            if url.endswith("/server_info"):
                return FakeResponse(
                    200,
                    json.dumps(
                        {
                            "version": "0.5.16",
                            "model_path": "test-model",
                            "enable_hierarchical_cache": True,
                            "internal_states": [],
                            "sensitive": "do-not-persist",
                        }
                    ).encode(),
                )
            if "/flush_cache" in url:
                flush_calls += 1
                if flush_calls == 1:
                    return FakeResponse(400, b"busy")
                return FakeResponse(200)
            return FakeResponse(200, b'{"success":true}')

        with patch(
            "endpoint_benchmark.cache_control.urllib.request.urlopen",
            side_effect=urlopen,
        ), contextlib.redirect_stderr(io.StringIO()):
            record = clear_caches(direct_target("http://sglang:30000/v1"), 4)

        instance = record["instances"][0]
        self.assertEqual(instance["runtime"], "sglang")
        self.assertEqual(instance["attempts"], 3)
        self.assertEqual(
            instance["operation"], "flush_cache+clear_hicache_storage"
        )
        self.assertEqual(flush_calls, 2)
        self.assertTrue(any("hicache/storage-backend/clear" in url for url in calls))
        self.assertNotIn("do-not-persist", json.dumps(record))

    def test_mixed_direct_target_clears_each_runtime_once(self) -> None:
        clear_calls: list[str] = []

        def urlopen(request: object, timeout: float) -> FakeResponse:
            del timeout
            url = request.full_url
            if request.get_method() == "POST":
                clear_calls.append(url)
                return FakeResponse(200)
            if "vllm" in url and url.endswith("/version"):
                return FakeResponse(200, b'{"version":"0.24.0"}')
            if url.endswith("/version"):
                return FakeResponse(404)
            return FakeResponse(
                200,
                b'{"version":"0.5.16","model_path":"test-model",'
                b'"enable_hierarchical_cache":false,"internal_states":[]}',
            )

        target = direct_target(
            "http://vllm:8000/v1/chat/completions",
            "http://sglang:30000/v1/chat/completions",
        )
        with patch(
            "endpoint_benchmark.cache_control.urllib.request.urlopen",
            side_effect=urlopen,
        ), contextlib.redirect_stderr(io.StringIO()):
            record = clear_caches(target, 8)

        self.assertEqual(
            {item["runtime"] for item in record["instances"]}, {"vllm", "sglang"}
        )
        self.assertEqual(len(clear_calls), 2)

    def test_unknown_runtime_fails_without_leaking_headers(self) -> None:
        secret = "Bearer do-not-log"

        def urlopen(request: object, timeout: float) -> FakeResponse:
            del request, timeout
            return FakeResponse(200, json.dumps({"echo": secret}).encode())

        stderr = io.StringIO()
        with patch(
            "endpoint_benchmark.cache_control.urllib.request.urlopen",
            side_effect=urlopen,
        ), contextlib.redirect_stderr(stderr), self.assertRaisesRegex(
            CacheControlError, "unknown runtime"
        ) as raised:
            clear_caches(
                direct_target(
                    "http://user:password@host:8000/v1",
                    headers=(("Authorization", secret),),
                ),
                1,
            )

        self.assertNotIn(secret, stderr.getvalue())
        self.assertNotIn(secret, str(raised.exception))

    def test_vllm_missing_dev_route_fails_immediately(self) -> None:
        def urlopen(request: object, timeout: float) -> FakeResponse:
            del timeout
            if request.full_url.endswith("/version"):
                return FakeResponse(200, b'{"version":"0.24.0"}')
            return FakeResponse(404)

        with patch(
            "endpoint_benchmark.cache_control.urllib.request.urlopen",
            side_effect=urlopen,
        ), contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(
            CacheControlError, "VLLM_SERVER_DEV_MODE=1"
        ):
            clear_caches(direct_target("http://host:8000/v1"), 1)

    def test_dynamo_discovers_once_and_clears_every_instance(self) -> None:
        client = FakeDynamoClient()
        runtime = FakeDynamoRuntime(client)
        target = TargetConfig(
            inference_endpoints=("http://frontend:8000/v1/chat/completions",),
            cache_sources=(DynamoCacheSource("benchmark", ("VllmWorker",)),),
            clear_retry_interval_s=0,
        )
        with patch(
            "endpoint_benchmark.cache_control._dynamo_runtime_class",
            return_value=lambda *args: runtime,
        ), patch(
            "endpoint_benchmark.cache_control._dynamo_version",
            return_value="1.3.0.post1",
        ), contextlib.redirect_stderr(io.StringIO()):
            record = clear_caches(target, 2)

        self.assertEqual(client.discovery_calls, 1)
        self.assertEqual(client.calls, {11: 2, 12: 1})
        self.assertEqual(client.consumed, {11: 2, 12: 1})
        self.assertEqual(
            {item["instance_id"] for item in record["instances"]}, {11, 12}
        )
        self.assertTrue(runtime.shutdown_called)

    def test_dynamo_fails_closed_when_discovery_is_empty(self) -> None:
        client = FakeDynamoClient()
        client.instance_ids = []
        runtime = FakeDynamoRuntime(client)
        target = TargetConfig(
            inference_endpoints=("http://frontend:8000/v1/chat/completions",),
            cache_sources=(DynamoCacheSource("benchmark", ("VllmWorker",)),),
        )
        with patch(
            "endpoint_benchmark.cache_control._dynamo_runtime_class",
            return_value=lambda *args: runtime,
        ), self.assertRaisesRegex(CacheControlError, "found no instances"):
            clear_caches(target, 2)

        self.assertEqual(client.discovery_calls, 1)
        self.assertTrue(runtime.shutdown_called)


class FakeDynamoClient:
    def __init__(self) -> None:
        self.discovery_calls = 0
        self.instance_ids = [11, 12]
        self.calls: dict[int, int] = {}
        self.consumed: dict[int, int] = {}

    async def wait_for_instances(self) -> list[int]:
        self.discovery_calls += 1
        return self.instance_ids

    async def direct(
        self, request: dict[str, object], instance_id: int, annotated: bool
    ) -> AsyncIterator[dict[str, str]]:
        self.assert_request(request, annotated)
        self.calls[instance_id] = self.calls.get(instance_id, 0) + 1
        status = (
            "error"
            if instance_id == 11 and self.calls[instance_id] == 1
            else "success"
        )

        async def responses() -> AsyncIterator[dict[str, str]]:
            yield {"status": status}
            self.consumed[instance_id] = self.consumed.get(instance_id, 0) + 1

        return responses()

    @staticmethod
    def assert_request(request: dict[str, object], annotated: bool) -> None:
        if request or annotated:
            raise AssertionError("Dynamo clear must send an empty, unannotated request")


class FakeDynamoEndpoint:
    def __init__(self, client: FakeDynamoClient) -> None:
        self.client_value = client

    async def client(self) -> FakeDynamoClient:
        return self.client_value


class FakeDynamoRuntime:
    def __init__(self, client: FakeDynamoClient) -> None:
        self.client_value = client
        self.shutdown_called = False

    def endpoint(self, path: str) -> FakeDynamoEndpoint:
        if path != "dyn://benchmark.VllmWorker.clear_kv_blocks":
            raise AssertionError(path)
        return FakeDynamoEndpoint(self.client_value)

    def shutdown(self) -> None:
        self.shutdown_called = True


if __name__ == "__main__":
    unittest.main()
