"""Endpoint pool routing."""

from __future__ import annotations

import threading


class RoundRobinRouter:
    """Thread-safe deterministic round-robin router."""

    def __init__(self, endpoints: tuple[str, ...]) -> None:
        if not endpoints:
            raise ValueError("at least one endpoint is required")
        self._endpoints = endpoints
        self._next = 0
        self._lock = threading.Lock()
        self._dispatch_count = [0] * len(endpoints)

    def next(self) -> tuple[int, str]:
        """Return the next endpoint index and URL."""
        with self._lock:
            index = self._next
            self._next = (self._next + 1) % len(self._endpoints)
            self._dispatch_count[index] += 1
        return index, self._endpoints[index]

    @property
    def dispatch_count(self) -> dict[str, int]:
        """Return dispatch counts using stable endpoint identifiers."""
        with self._lock:
            return {
                f"endpoint-{index}": count
                for index, count in enumerate(self._dispatch_count)
            }
