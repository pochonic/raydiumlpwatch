import base64
import hashlib
import unittest
from unittest.mock import Mock, patch

from solders.pubkey import Pubkey

import clmm_position as position
from clmm_monitor import Config, POOL_ID, STONK, USDC, snapshot_status


def account(kind, size):
    data = bytearray(size)
    data[:8] = hashlib.sha256(f"account:{kind}".encode()).digest()[:8]
    return data


def encoded(data):
    return dict(owner=position.PROGRAM, data=[base64.b64encode(data).decode(), "base64"])


def put(data, offset, value, size=16, signed=False):
    data[offset:offset + size] = value.to_bytes(size, "little", signed=signed)


class PositionTests(unittest.TestCase):
    def test_fee_growth_inside_outside_and_wraparound(self):
        q = position.Q64
        # Inside growth=100-20-30=50; last=40, liquidity=2, owed=3.
        self.assertEqual(position.pending_fee(100*q, 20*q, 30*q, 0, -10, 10, 40*q, 2, 3), 23)
        self.assertEqual(position.pending_fee(100*q, 20*q, 10*q, -11, -10, 10, 5*q, 2, 3), 13)
        self.assertEqual(position.pending_fee(100*q, 20*q, 30*q, 10, -10, 10, 5*q, 2, 3), 13)
        self.assertEqual(position.pending_fee(2*q, 0, 0, 0, -10, 10, position.MOD128-q, 2, 3), 9)

    def test_negative_tick_arrays(self):
        self.assertEqual(position.tick_start(-1, 10), -600)
        self.assertEqual(position.tick_start(-600, 10), -600)
        self.assertEqual(position.tick_start(-601, 10), -1200)

    def test_live_layout_and_consistent_snapshot(self):
        personal = account("PersonalPositionState", 281)
        personal[9:41] = bytes(Pubkey.from_string(position.DEFAULT_NFT))
        personal[41:73] = bytes(Pubkey.from_string(POOL_ID))
        put(personal, 73, -10, 4, True)
        put(personal, 77, 10, 4, True)
        put(personal, 81, 1000000000)
        pool = account("PoolState", 1544)
        pool[73:105] = bytes(Pubkey.from_string(STONK))
        pool[105:137] = bytes(Pubkey.from_string(USDC))
        pool[233:235] = bytes([9, 6])
        put(pool, 235, 10, 2)
        put(pool, 253, position.Q64)
        arrays = []
        for tick in (-10, 10):
            data = account("TickArrayState", 10240)
            data[8:40] = bytes(Pubkey.from_string(POOL_ID))
            start = position.tick_start(tick, 10)
            put(data, 40, start, 4, True)
            offset = 44 + (tick - start) // 10 * 168
            put(data, offset, tick, 4, True)
            put(data, offset + 20, 1000000000)
            arrays.append(encoded(data))
        client = position.PositionClient(POOL_ID, STONK, USDC)
        client.accounts = Mock(side_effect=[(100, [encoded(personal), encoded(pool)]),
                                           (101, [encoded(personal), encoded(pool)] + arrays)])
        result = client.fetch()
        self.assertEqual(result["slot"], 101)
        self.assertEqual(result["price"], 1000)
        self.assertAlmostEqual(result["value_usdc"], 0.999700069986, places=8)
        self.assertEqual(result["fee_stonk"], 0)
        self.assertEqual(client.accounts.call_args.args[1], 100)
        wrong = encoded(personal)
        wrong["owner"] = USDC
        with self.assertRaises(ValueError):
            position.position_data(wrong, position.DEFAULT_NFT, POOL_ID)
        with self.assertRaises(ValueError):
            position.position_data(None, position.DEFAULT_NFT, POOL_ID)
        with self.assertRaises(ValueError):
            position.position_data(encoded(personal), STONK, POOL_ID)

    def test_tick_state_precedence_and_empty_position(self):
        metrics = dict(price=0.4, position=dict(liquidity="1", tick_lower=0,
                       tick_upper=10, tick_current=9, lower=0.3, upper=0.4))
        self.assertEqual(snapshot_status(metrics, Config()), "NEAR_UPPER")
        metrics["position"]["tick_current"] = 10
        self.assertEqual(snapshot_status(metrics, Config()), "OUT_ABOVE")
        metrics["position"]["liquidity"] = "0"
        self.assertEqual(snapshot_status(metrics, Config()), "NO_LIQUIDITY")

    @patch("clmm_position.requests.post")
    def test_rpc_error_redacts_url(self, post):
        post.side_effect = position.requests.ConnectionError("https://rpc.example/secret")
        with self.assertRaises(RuntimeError) as error:
            position.PositionClient(POOL_ID, STONK, USDC).fetch()
        self.assertNotIn("secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
