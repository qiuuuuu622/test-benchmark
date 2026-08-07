from __future__ import annotations

import http.client
import json
import unittest
from unittest.mock import patch

from endpoint_benchmark.transport import StreamingHttpClient


class FakeResponse:
    status = 200

    def __init__(self) -> None:
        chunks = [
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "hello"}}]},
            {"choices": [{"delta": {"content": " world"}}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        ]
        lines = [f"data: {json.dumps(chunk)}\n".encode() for chunk in chunks]
        lines.append(b"data: [DONE]\n")
        self._lines = iter(lines)

    def readline(self) -> bytes:
        return next(self._lines, b"")

    def read(self, amount: int | None = None) -> bytes:
        del amount
        return b""


class ErrorResponse(FakeResponse):
    status = 400

    def read(self, amount: int | None = None) -> bytes:
        del amount
        return b"echo Bearer response-secret"


class FakeConnection:
    def request(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    def getresponse(self) -> FakeResponse:
        return FakeResponse()

    def close(self) -> None:
        pass


class BrokenSendConnection(FakeConnection):
    def request(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise BrokenPipeError("stale keep-alive")


class BrokenResponseConnection(FakeConnection):
    def getresponse(self) -> FakeResponse:
        raise http.client.RemoteDisconnected("stale keep-alive")


class TransportTest(unittest.TestCase):
    def test_first_output_is_recorded_while_reading_stream(self) -> None:
        client = StreamingHttpClient()
        clock = iter((0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0))
        with (
            patch.object(client, "_connection", return_value=FakeConnection()),
            patch("endpoint_benchmark.transport.now_ms", side_effect=clock),
        ):
            result = client.post(
                "http://example.test/v1/chat/completions",
                {"messages": [], "stream": True},
                {},
                10,
                True,
            )

        self.assertEqual(result.start_ms, 0.0)
        self.assertEqual(result.first_output_ms, 20.0)
        self.assertEqual(result.end_ms, 60.0)
        self.assertEqual(result.chunk_arrival_ms, [20.0, 30.0])
        self.assertEqual(result.usage["completion_tokens"], 2)

    def test_reused_stale_connection_retries_once_before_response(self) -> None:
        client = StreamingHttpClient("reuse")
        connections = iter((BrokenSendConnection(), FakeConnection()))
        with (
            patch.object(client, "_connection", side_effect=connections),
            patch.object(client, "_discard_connection"),
        ):
            result = client.post(
                "http://example.test/v1/chat/completions",
                {"messages": [], "stream": True},
                {},
                10,
                False,
            )

        self.assertIsNone(result.error)
        self.assertEqual(result.retry_count, 1)

    def test_response_failure_does_not_replay_post(self) -> None:
        client = StreamingHttpClient("reuse")
        connections = iter((BrokenResponseConnection(), FakeConnection()))
        with (
            patch.object(
                client, "_connection", side_effect=connections
            ) as connection_factory,
            patch.object(client, "_discard_connection"),
        ):
            result = client.post(
                "http://example.test/v1/chat/completions",
                {"messages": [], "stream": True},
                {},
                10,
                False,
            )

        self.assertEqual(result.error["type"], "connection")
        self.assertEqual(result.retry_count, 0)
        self.assertEqual(connection_factory.call_count, 1)

    def test_http_error_redacts_configured_header_values(self) -> None:
        client = StreamingHttpClient()
        connection = FakeConnection()
        with patch.object(
            connection, "getresponse", return_value=ErrorResponse()
        ), patch.object(client, "_connection", return_value=connection):
            result = client.post(
                "http://example.test/v1/chat/completions",
                {"messages": [], "stream": True},
                {"Authorization": "Bearer response-secret"},
                10,
                False,
            )

        self.assertIsNotNone(result.error)
        self.assertNotIn("response-secret", result.error["message"])


if __name__ == "__main__":
    unittest.main()
