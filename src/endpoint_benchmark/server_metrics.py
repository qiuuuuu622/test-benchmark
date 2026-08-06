"""Optional Prometheus counter collection from one logical metrics URL."""

from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass
from typing import Any

_SAMPLE_RE = re.compile(
    r"^(?P<name>[^\s{]+)(?:\{(?P<labels>.*)\})?\s+(?P<value>\S+)"
)
_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


@dataclass(frozen=True)
class MetricsSnapshot:
    """Normalized values from one metrics response."""

    totals: dict[str, float]
    per_position: dict[str, dict[int, float]]


def fetch_metrics(url: str, timeout_s: float = 5.0) -> MetricsSnapshot:
    """Fetch and parse Prometheus text metrics."""
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        text = response.read().decode("utf-8")
    return parse_prometheus(text)


def parse_prometheus(text: str) -> MetricsSnapshot:
    """Parse samples and combine label series within one logical source."""
    totals: dict[str, float] = {}
    per_position: dict[str, dict[int, float]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_RE.match(line)
        if match is None:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        name = match.group("name")
        labels = dict(_LABEL_RE.findall(match.group("labels") or ""))
        if "position" in labels:
            try:
                position = int(labels["position"])
            except ValueError:
                continue
            positions = per_position.setdefault(name, {})
            positions[position] = positions.get(position, 0.0) + value
        else:
            totals[name] = totals.get(name, 0.0) + value
    return MetricsSnapshot(totals=totals, per_position=per_position)


def calculate_metrics_delta(
    url: str,
    before: MetricsSnapshot,
    after: MetricsSnapshot,
) -> dict[str, Any]:
    """Calculate supported server counter deltas and speculative rate."""
    accepted_name = "vllm:spec_decode_num_accepted_tokens_total"
    draft_name = "vllm:spec_decode_num_draft_tokens_total"
    accepted = _counter_delta(before, after, accepted_name)
    draft = _counter_delta(before, after, draft_name)
    available = accepted is not None and draft is not None
    rate = accepted / draft if available and draft and draft > 0 else None

    position_name = "vllm:spec_decode_num_accepted_tokens_per_pos_total"
    before_positions = before.per_position.get(position_name, {})
    after_positions = after.per_position.get(position_name, {})
    position_delta = {
        str(position): after_positions.get(position, 0.0)
        - before_positions.get(position, 0.0)
        for position in sorted(before_positions.keys() | after_positions.keys())
    }
    if any(value < 0 for value in position_delta.values()):
        position_delta = {}

    return {
        "available": available,
        "url": url,
        "accepted_tokens": int(accepted) if accepted is not None else None,
        "draft_tokens": int(draft) if draft is not None else None,
        "acceptance_rate": rate,
        "accepted_tokens_per_position": position_delta,
    }


def _counter_delta(
    before: MetricsSnapshot,
    after: MetricsSnapshot,
    name: str,
) -> float | None:
    if name not in before.totals or name not in after.totals:
        return None
    delta = after.totals[name] - before.totals[name]
    return delta if delta >= 0 else None
