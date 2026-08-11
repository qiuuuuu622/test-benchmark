from __future__ import annotations

import copy
import re
import unittest

from endpoint_benchmark.isolated_miss import (
    isolate_messages,
    new_identity,
    validate_cases,
)
from endpoint_benchmark.models import RequestCase


class IsolatedMissTest(unittest.TestCase):
    @staticmethod
    def _case(request_id: str, content: list[dict]) -> RequestCase:
        return RequestCase(
            request_id=request_id,
            messages=[{"role": "user", "content": content}],
            request={},
            metadata={},
        )

    def test_identity_is_random_16_hex(self) -> None:
        identities = {new_identity() for _ in range(8)}

        self.assertEqual(len(identities), 8)
        self.assertTrue(
            all(re.fullmatch(r"[0-9a-f]{16}", value) for value in identities)
        )

    def test_prefers_system_text_and_preserves_the_source(self) -> None:
        messages = [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "rules"},
                    {"type": "text", "text": "more rules"},
                ],
            },
            {"role": "user", "content": "question"},
        ]
        original = copy.deepcopy(messages)

        isolated, strategy = isolate_messages(messages, "0123456789abcdef")

        self.assertEqual(strategy, "none")
        self.assertEqual(messages, original)
        self.assertEqual(
            isolated[0]["content"][0]["text"],
            "[rid:0123456789abcdef]\n\nrules",
        )
        self.assertEqual(isolated[0]["content"][1]["text"], "more rules")
        self.assertEqual(isolated[1]["content"], "question")

    def test_falls_back_to_user_text(self) -> None:
        isolated, strategy = isolate_messages(
            [
                {"role": "system", "content": []},
                {"role": "user", "content": "question"},
            ],
            "fedcba9876543210",
        )

        self.assertEqual(strategy, "none")
        self.assertRegex(
            isolated[1]["content"],
            re.compile(r"^\[rid:[0-9a-f]{16}\]\n\nquestion$"),
        )

    def test_vllm_uuid_isolated_without_changing_image_url(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                        "uuid": "{isolated_miss_id}",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,BBBB"},
                        "uuid": "prefix-{isolated_miss_id}",
                    },
                ],
            }
        ]
        original = copy.deepcopy(messages)

        isolated, strategy = isolate_messages(messages, "0123456789abcdef")

        text, first, second = isolated[0]["content"]
        self.assertEqual(text, {"type": "text", "text": "[rid:0123456789abcdef]\n\n"})
        self.assertEqual(first["uuid"], "0123456789abcdef-0")
        self.assertEqual(second["uuid"], "prefix-0123456789abcdef-1")
        self.assertEqual(first["image_url"]["url"], "data:image/png;base64,AAAA")
        self.assertEqual(strategy, "vllm_uuid")
        self.assertEqual(messages, original)

    def test_sglang_url_isolated_without_uuid(self) -> None:
        isolated, strategy = isolate_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "http://media.test/{isolated_miss_id}/a.png"
                            },
                        }
                    ],
                }
            ],
            "fedcba9876543210",
        )

        image = isolated[0]["content"][1]
        self.assertEqual(
            image["image_url"]["url"],
            "http://media.test/fedcba9876543210-0/a.png",
        )
        self.assertNotIn("uuid", image)
        self.assertEqual(strategy, "sglang_unique_content")

    def test_validation_rejects_invalid_media_and_mixed_strategies(self) -> None:
        invalid = {
            "missing": [
                self._case(
                    "missing",
                    [
                        {
                            "type": "image_url",
                            "image_url": {"url": "http://media.test/a.png"},
                        }
                    ],
                )
            ],
            "both": [
                self._case(
                    "both",
                    [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "http://media.test/{isolated_miss_id}/a.png"
                            },
                            "uuid": "{isolated_miss_id}",
                        }
                    ],
                )
            ],
            "unsupported": [
                self._case(
                    "audio",
                    [{"type": "input_audio", "input_audio": {"data": "AAAA"}}],
                )
            ],
            "mixed": [
                self._case(
                    "vllm",
                    [
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAAA"},
                            "uuid": "{isolated_miss_id}",
                        }
                    ],
                ),
                self._case(
                    "sglang",
                    [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "http://media.test/{isolated_miss_id}/a.png"
                            },
                        }
                    ],
                ),
            ],
        }

        for name, cases in invalid.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_cases(cases)

    def test_text_only_cases_can_coexist_with_one_media_strategy(self) -> None:
        cases = [
            RequestCase("text", [{"role": "user", "content": "hello"}], {}, {}),
            self._case(
                "image",
                [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                        "uuid": "{isolated_miss_id}",
                    }
                ],
            ),
        ]

        self.assertEqual(validate_cases(cases), "vllm_uuid")


if __name__ == "__main__":
    unittest.main()
