"""JSONL dataset loading and normalization."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from endpoint_benchmark.models import LoadConfig, RequestCase

_MAX_TOKEN_ALIASES = ("target_tokens", "max_tokens", "max_completion_tokens")


def load_dataset(path: Path, load: LoadConfig) -> tuple[list[RequestCase], list[str]]:
    """Load and normalize request cases from a JSONL file."""
    cases: list[RequestCase] = []
    warnings: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}: {exc}") from exc
            cases.append(_normalize_row(row, line_number, warnings))

    if not cases:
        raise ValueError(f"no request cases found in {path}")
    if load.shuffle:
        random.Random(load.seed).shuffle(cases)
    if load.num_prompts is not None:
        cases = cases[: load.num_prompts]
    return cases, sorted(warnings)


def _normalize_row(
    row: Any,
    line_number: int,
    warnings: set[str],
) -> RequestCase:
    if not isinstance(row, dict):
        raise ValueError(f"dataset line {line_number} must be a JSON object")
    messages = row.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError(f"dataset line {line_number} requires non-empty messages")

    request = dict(row.get("request") or {})
    if not isinstance(row.get("request", {}), dict):
        raise ValueError(f"dataset line {line_number} request must be an object")

    if "max_output_tokens" not in request:
        for alias in _MAX_TOKEN_ALIASES:
            if alias in row:
                request["max_output_tokens"] = row[alias]
                warnings.add(f"normalized legacy field {alias}")
                break

    metadata = dict(row.get("metadata") or {})
    if "prompt_tokens_est" in row and "prompt_tokens_estimate" not in metadata:
        metadata["prompt_tokens_estimate"] = row["prompt_tokens_est"]
        warnings.add("normalized legacy field prompt_tokens_est")

    return RequestCase(
        request_id=str(row.get("id", line_number - 1)),
        messages=messages,
        request=request,
        metadata=metadata,
    )
