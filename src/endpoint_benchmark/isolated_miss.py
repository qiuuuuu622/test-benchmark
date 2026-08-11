"""Per-request cache identity isolation."""

from __future__ import annotations

import copy
import secrets
from collections.abc import Iterable
from typing import Any

from endpoint_benchmark.models import RequestCase

_PLACEHOLDER = "{isolated_miss_id}"


def new_identity() -> str:
    """Return one random 16-hex logical-request identity."""
    return secrets.token_hex(8)


def isolate_messages(
    messages: list[dict[str, Any]], identity: str
) -> tuple[list[dict[str, Any]], str]:
    """Deep-copy and isolate one request's text and declared image identities."""
    isolated = copy.deepcopy(messages)
    media_strategy = _isolate_images(isolated, identity)
    _inject_text_marker(isolated, f"[rid:{identity}]\n\n")
    return isolated, media_strategy


def validate_cases(cases: Iterable[RequestCase]) -> str:
    """Validate every case before I/O and return the target media strategy."""
    media_strategy = "none"
    for case in cases:
        try:
            _, case_strategy = isolate_messages(case.messages, "0" * 16)
        except ValueError as exc:
            raise ValueError(f"isolated miss request {case.request_id}: {exc}") from exc
        if case_strategy == "none":
            continue
        if media_strategy not in {"none", case_strategy}:
            raise ValueError(
                "isolated miss requires one media strategy per target; "
                f"found {media_strategy} and {case_strategy}"
            )
        media_strategy = case_strategy
    return media_strategy


def _isolate_images(messages: list[dict[str, Any]], identity: str) -> str:
    strategy = "none"
    media_index = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                raise ValueError("content parts must be objects")
            part_type = part.get("type")
            if part_type == "text":
                continue
            if part_type != "image_url":
                raise ValueError(f"unsupported media content type: {part_type!r}")

            image_url = part.get("image_url")
            if not isinstance(image_url, dict) or not isinstance(
                image_url.get("url"), str
            ):
                raise ValueError("image_url content requires a string URL")
            url = image_url["url"]
            uuid_value = part.get("uuid")
            has_uuid_placeholder = (
                isinstance(uuid_value, str) and _PLACEHOLDER in uuid_value
            )
            has_url_placeholder = _PLACEHOLDER in url
            if has_uuid_placeholder == has_url_placeholder:
                detail = (
                    "mixed UUID and URL placeholders"
                    if has_uuid_placeholder
                    else "missing UUID or unique-content URL placeholder"
                )
                raise ValueError(f"image {media_index} has {detail}")

            item_strategy = (
                "vllm_uuid" if has_uuid_placeholder else "sglang_unique_content"
            )
            if strategy not in {"none", item_strategy}:
                raise ValueError("request mixes vLLM and SGLang media strategies")
            if item_strategy == "sglang_unique_content" and "uuid" in part:
                raise ValueError("SGLang unique-content images cannot include UUID")

            replacement = f"{identity}-{media_index}"
            if has_uuid_placeholder:
                part["uuid"] = uuid_value.replace(_PLACEHOLDER, replacement)
            else:
                image_url["url"] = url.replace(_PLACEHOLDER, replacement)
            strategy = item_strategy
            media_index += 1
    return strategy


def _inject_text_marker(messages: list[dict[str, Any]], marker: str) -> None:
    for role in ("system", "user"):
        if _prefix_first_text(messages, role, marker):
            return
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content.insert(0, {"type": "text", "text": marker})
            return
    raise ValueError("requires a system or user text insertion point")


def _prefix_first_text(messages: list[dict[str, Any]], role: str, marker: str) -> bool:
    for message in messages:
        if message.get("role") != role:
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = marker + content
            return True
        if not isinstance(content, list):
            continue
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ):
                part["text"] = marker + part["text"]
                return True
    return False
