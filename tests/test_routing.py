from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor

from endpoint_benchmark.routing import RoundRobinRouter


class RoutingTest(unittest.TestCase):
    def test_round_robin_is_balanced_under_concurrency(self) -> None:
        router = RoundRobinRouter(("a", "b", "c"))
        with ThreadPoolExecutor(max_workers=12) as executor:
            list(executor.map(lambda _: router.next(), range(300)))

        self.assertEqual(
            router.dispatch_count,
            {"endpoint-0": 100, "endpoint-1": 100, "endpoint-2": 100},
        )


if __name__ == "__main__":
    unittest.main()
