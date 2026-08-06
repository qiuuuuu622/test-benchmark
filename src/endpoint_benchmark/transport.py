"""Standard-library streaming HTTP transport."""

from __future__ import annotations

import http.client
import json
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit


def now_ms() -> float:
    """Return a monotonic high-resolution timestamp in milliseconds."""
    return time.perf_counter() * 1000.0


@dataclass
class TransportResult:
    """Raw observations from one streaming request."""

    status_code: int | None
    error: dict[str, str] | None
    start_ms: float
    first_output_ms: float | None
    end_ms: float
    usage: dict[str, Any] | None
    finish_reason: str | None
    chunk_arrival_ms: list[float]


class StreamingHttpClient:
    """OpenAI-compatible SSE client with optional per-thread connection reuse."""

    def __init__(self, connection_mode: str = "reuse") -> None:
        self._connection_mode = connection_mode
        self._local = threading.local()
        self._connections: set[http.client.HTTPConnection] = set()
        self._connections_lock = threading.Lock()

    def post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout_s: float,
        record_chunk_timestamps: bool,
    ) -> TransportResult:
        """Send one request and record observable streaming events."""
        parsed = urlsplit(endpoint)
        start_ms = now_ms()
        first_output_ms: float | None = None
        usage: dict[str, Any] | None = None
        finish_reason: str | None = None
        arrivals: list[float] = []
        saw_done = False
        connection: http.client.HTTPConnection | None = None
        try:
            connection = self._connection(parsed, timeout_s)
            request_headers = {
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
                **headers,
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            connection.request(
                "POST",
                _request_target(parsed),
                body=body,
                headers=request_headers,
            )
            response = connection.getresponse()
            if response.status != 200:
                message = response.read(2000).decode("utf-8", errors="replace")
                self._discard_connection(parsed)
                return TransportResult(
                    status_code=response.status,
                    error={"type": "http_error", "message": message},
                    start_ms=start_ms,
                    first_output_ms=None,
                    end_ms=now_ms(),
                    usage=None,
                    finish_reason=None,
                    chunk_arrival_ms=[],
                )

            while True:
                raw = response.readline()
                if not raw:
                    break
                received_ms = now_ms()
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data_text = line[5:].strip()
                if data_text == "[DONE]":
                    saw_done = True
                    response.read()
                    break
                try:
                    chunk = json.loads(data_text)
                except json.JSONDecodeError as exc:
                    self._discard_connection(parsed)
                    return TransportResult(
                        status_code=response.status,
                        error={"type": "invalid_json", "message": str(exc)},
                        start_ms=start_ms,
                        first_output_ms=first_output_ms,
                        end_ms=now_ms(),
                        usage=usage,
                        finish_reason=finish_reason,
                        chunk_arrival_ms=arrivals,
                    )
                if chunk.get("usage") is not None:
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if choices:
                    choice = choices[0]
                    if choice.get("finish_reason") is not None:
                        finish_reason = choice["finish_reason"]
                    delta = choice.get("delta") or {}
                    if delta.get("content") or delta.get("tool_calls"):
                        if first_output_ms is None:
                            first_output_ms = received_ms
                        if record_chunk_timestamps:
                            arrivals.append(received_ms)

            error = None
            if not saw_done:
                error = {
                    "type": "truncated_stream",
                    "message": "SSE stream ended before [DONE]",
                }
                self._discard_connection(parsed)
            return TransportResult(
                status_code=response.status,
                error=error,
                start_ms=start_ms,
                first_output_ms=first_output_ms,
                end_ms=now_ms(),
                usage=usage,
                finish_reason=finish_reason,
                chunk_arrival_ms=arrivals,
            )
        except TimeoutError as exc:
            self._discard_connection(parsed)
            return _transport_error("timeout", exc, start_ms)
        except (OSError, http.client.HTTPException) as exc:
            self._discard_connection(parsed)
            return _transport_error("connection", exc, start_ms)
        finally:
            if self._connection_mode == "new" and connection is not None:
                connection.close()

    def close(self) -> None:
        """Close all connections created by this client."""
        with self._connections_lock:
            connections = tuple(self._connections)
            self._connections.clear()
        for connection in connections:
            connection.close()
        local_connections = getattr(self._local, "connections", None)
        if local_connections is not None:
            local_connections.clear()

    def _connection(
        self,
        parsed: SplitResult,
        timeout_s: float,
    ) -> http.client.HTTPConnection:
        if self._connection_mode == "new":
            return _new_connection(parsed, timeout_s)
        connections = getattr(self._local, "connections", None)
        if connections is None:
            connections = {}
            self._local.connections = connections
        key = (parsed.scheme, parsed.hostname, parsed.port)
        connection = connections.get(key)
        if connection is None:
            connection = _new_connection(parsed, timeout_s)
            connections[key] = connection
            with self._connections_lock:
                self._connections.add(connection)
        return connection

    def _discard_connection(self, parsed: SplitResult) -> None:
        connections = getattr(self._local, "connections", None)
        if not connections:
            return
        key = (parsed.scheme, parsed.hostname, parsed.port)
        connection = connections.pop(key, None)
        if connection is not None:
            with self._connections_lock:
                self._connections.discard(connection)
            connection.close()


def _new_connection(
    parsed: SplitResult,
    timeout_s: float,
) -> http.client.HTTPConnection:
    if parsed.hostname is None:
        raise ValueError("endpoint has no hostname")
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port,
            timeout=timeout_s,
            context=ssl.create_default_context(),
        )
    return http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout_s)


def _request_target(parsed: SplitResult) -> str:
    path = parsed.path or "/"
    return f"{path}?{parsed.query}" if parsed.query else path


def _transport_error(
    error_type: str,
    exc: BaseException,
    start_ms: float,
) -> TransportResult:
    return TransportResult(
        status_code=None,
        error={"type": error_type, "message": str(exc)},
        start_ms=start_ms,
        first_output_ms=None,
        end_ms=now_ms(),
        usage=None,
        finish_reason=None,
        chunk_arrival_ms=[],
    )
