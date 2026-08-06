from __future__ import annotations

import unittest

from endpoint_benchmark.aggregation import summarize_requests
from endpoint_benchmark.models import RequestResult


def result(index: int, ttft: float, output_tokens: int | None) -> RequestResult:
    return RequestResult(
        request_id=str(index),
        index=index,
        endpoint_index=index % 2,
        success=True,
        status_code=200,
        error=None,
        input_tokens=10 if output_tokens is not None else None,
        output_tokens=output_tokens,
        e2e_latency_ms=ttft + 100,
        ttft_ms=ttft,
        tpot_ms=5.0 if output_tokens is not None else None,
        started_at_offset_ms=0,
        finished_at_offset_ms=100,
        finish_reason="stop",
        inter_chunk_latencies_ms=[2.0, 4.0],
    )


class AggregationTest(unittest.TestCase):
    def test_calculates_one_distribution_for_entire_pool(self) -> None:
        summary = summarize_requests(
            concurrency=2,
            requested=4,
            results=[
                result(0, 10, 20),
                result(1, 20, 20),
                result(2, 30, 20),
                result(3, 40, 20),
            ],
            duration_s=2.0,
            dispatch_count={"endpoint-0": 2, "endpoint-1": 2},
            server_metrics=None,
        )

        self.assertEqual(summary["ttft_ms"]["samples"], 4)
        self.assertEqual(summary["ttft_ms"]["p50"], 25.0)
        self.assertEqual(summary["throughput"]["output_tokens_per_second"], 40)
        self.assertEqual(summary["inter_chunk_latency_ms"]["samples"], 8)
        self.assertTrue(summary["valid"])

    def test_missing_usage_makes_token_throughput_incomplete(self) -> None:
        summary = summarize_requests(
            concurrency=1,
            requested=2,
            results=[result(0, 10, 20), result(1, 20, None)],
            duration_s=1.0,
            dispatch_count={"endpoint-0": 2},
            server_metrics=None,
        )

        self.assertFalse(summary["throughput"]["token_counts_complete"])
        self.assertIsNone(summary["throughput"]["output_tokens_per_second"])

    def test_marks_pool_invalid_below_success_rate_threshold(self) -> None:
        failed = result(1, 20, 20)
        failed.success = False
        failed.error = {"type": "http_error", "message": "bad request"}
        summary = summarize_requests(
            concurrency=1,
            requested=2,
            results=[result(0, 10, 20), failed],
            duration_s=1.0,
            dispatch_count={"endpoint-0": 2},
            server_metrics=None,
            min_success_rate=1.0,
        )

        self.assertEqual(summary["success_rate"], 0.5)
        self.assertFalse(summary["valid"])


if __name__ == "__main__":
    unittest.main()
