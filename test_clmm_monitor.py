import unittest
from unittest.mock import Mock, patch

import clmm_monitor as monitor


class MonitorTests(unittest.TestCase):
    def test_boundaries(self):
        config = monitor.Config()
        for price, expected in [(0.28, "OUT_BELOW"), (config.lower, "NEAR_LOWER"),
                                (0.33, "IN_RANGE"), (0.37, "NEAR_UPPER"),
                                (config.upper, "OUT_ABOVE")]:
            self.assertEqual(monitor.classify(price, config), expected)

    def test_invalid_config(self):
        for kwargs in [dict(lower=0.4), dict(upper=float('nan')), dict(near_pct=100)]:
            with self.assertRaises((ValueError, RuntimeError)):
                monitor.Config(**kwargs)

    def test_retry_persistence_degradation_and_recovery(self):
        store = monitor.Store(":memory:")
        self.addCleanup(store.db.close)
        telegram = Mock(token="secret")
        config = monitor.Config()
        metrics = dict(price=0.33, apr=400, volume=8e6, tvl=2e6, fees=20000)
        fetch = Mock(return_value=metrics)
        cycle = lambda: monitor.process_cycle(store, config, telegram, fetch)
        cycle()
        cycle()
        self.assertEqual(telegram.send.call_count, 1)
        metrics = dict(metrics, price=0.4)
        fetch.return_value = metrics
        telegram.send.side_effect = RuntimeError("secret")
        cycle()
        telegram.send.side_effect = None
        cycle()
        self.assertEqual(telegram.send.call_count, 3)
        cycle()
        self.assertEqual(telegram.send.call_count, 3)
        fetch.side_effect = RuntimeError("offline")
        for _ in range(4):
            cycle()
        self.assertEqual(telegram.send.call_count, 4)
        fetch.side_effect = None
        cycle()
        self.assertIn("API_RECOVERED", telegram.send.call_args.args[0])
        self.assertEqual(telegram.send.call_count, 5)

    @patch("clmm_monitor.requests.get")
    def test_parser_and_orientation(self, get):
        pool = dict(id=monitor.POOL_ID, type="Concentrated",
                    mintA=dict(address=monitor.USDC), mintB=dict(address=monitor.STONK),
                    price=4, tvl=100, day=dict(apr=20, volume=200, volumeFee=1))
        get.return_value.json.return_value = dict(success=True, data=[pool])
        self.assertEqual(monitor.fetch_metrics()["price"], 0.25)
        pool["price"] = float('inf')
        with self.assertRaises(RuntimeError):
            monitor.fetch_metrics()
        pool["id"] = "wrong"
        with self.assertRaises(ValueError):
            monitor.fetch_metrics()


if __name__ == "__main__":
    unittest.main()
