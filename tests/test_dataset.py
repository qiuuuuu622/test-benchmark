from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from endpoint_benchmark.dataset import load_dataset
from endpoint_benchmark.models import LoadConfig


class DatasetTest(unittest.TestCase):
    def test_normalizes_legacy_token_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "messages": [{"role": "user", "content": "hi"}],
                        "target_tokens": 32,
                        "prompt_tokens_est": 4,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cases, warnings = load_dataset(path, LoadConfig(concurrency=(1,)))

        self.assertEqual(cases[0].request["max_output_tokens"], 32)
        self.assertEqual(cases[0].metadata["prompt_tokens_estimate"], 4)
        self.assertIn("normalized legacy field target_tokens", warnings)

    def test_rejects_empty_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.jsonl"
            path.write_text('{"messages": []}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-empty messages"):
                load_dataset(path, LoadConfig(concurrency=(1,)))

    def test_normalizes_legacy_tools_and_images(self) -> None:
        tool = {"type": "function", "function": {"name": "lookup"}}
        image = "data:image/png;base64,abc"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "messages": [
                            {"role": "user", "content": "before<image>after"}
                        ],
                        "tools": [tool],
                        "images": [image],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cases, warnings = load_dataset(path, LoadConfig(concurrency=(1,)))

        self.assertEqual(cases[0].request["tools"], [tool])
        self.assertEqual(
            cases[0].messages[0]["content"],
            [
                {"type": "text", "text": "before"},
                {"type": "image_url", "image_url": {"url": image}},
                {"type": "text", "text": "after"},
            ],
        )
        self.assertIn("normalized legacy field tools", warnings)
        self.assertIn("normalized legacy field images", warnings)


if __name__ == "__main__":
    unittest.main()
