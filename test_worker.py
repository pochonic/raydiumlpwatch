import unittest
from unittest.mock import Mock, patch

from main import TelegramClient, run_monitors


class FakeStop:
    def __init__(self):
        self.now = 0

    def is_set(self):
        return self.now > 300

    def wait(self, seconds):
        self.now += seconds


class WorkerTests(unittest.TestCase):
    def test_independent_intervals_and_failure_isolation(self):
        stop = FakeStop()
        calls = {name: [] for name in ("CLMM", "SOL", "Backyard")}

        def run(name):
            calls[name].append(stop.now)
            if name == "CLMM":
                raise RuntimeError("RPC unavailable")

        jobs = [(name, 60 if name == "CLMM" else 300,
                 lambda name=name: run(name)) for name in calls]
        run_monitors(jobs, stop, Mock(token=None), clock=lambda: stop.now)
        self.assertEqual(calls["CLMM"], [0, 60, 120, 180, 240, 300])
        self.assertEqual(calls["SOL"], [0, 300])
        self.assertEqual(calls["Backyard"], [0, 300])

    @patch("main.requests.post")
    def test_only_title_is_bold_and_html_is_escaped(self, post):
        post.return_value.json.return_value = {"ok": True}
        client = TelegramClient()
        client.token, client.chat_id = "test", "123"
        client.send("Raydium CLMM STONK/USDC\nPrecio < 1 & TVL > 0")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertEqual(payload["text"], "<b>Raydium CLMM STONK/USDC</b>\nPrecio &lt; 1 &amp; TVL &gt; 0")


if __name__ == "__main__":
    unittest.main()
