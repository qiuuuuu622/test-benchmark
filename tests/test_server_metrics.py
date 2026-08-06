from __future__ import annotations

import unittest

from endpoint_benchmark.server_metrics import (
    calculate_metrics_delta,
    parse_prometheus,
)


class ServerMetricsTest(unittest.TestCase):
    def test_calculates_delta_from_one_logical_metrics_url(self) -> None:
        before = parse_prometheus(
            """
vllm:spec_decode_num_accepted_tokens_total 100
vllm:spec_decode_num_draft_tokens_total 200
vllm:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 80
"""
        )
        after = parse_prometheus(
            """
vllm:spec_decode_num_accepted_tokens_total 175
vllm:spec_decode_num_draft_tokens_total 300
vllm:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 140
"""
        )

        result = calculate_metrics_delta("http://metrics/metrics", before, after)

        self.assertEqual(result["accepted_tokens"], 75)
        self.assertEqual(result["draft_tokens"], 100)
        self.assertEqual(result["acceptance_rate"], 0.75)
        self.assertEqual(result["accepted_tokens_per_position"], {"0": 60.0})


if __name__ == "__main__":
    unittest.main()
