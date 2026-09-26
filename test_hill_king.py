"""python3 -m unittest test_hill_king"""
import io
import json
import unittest
import urllib.error
from unittest import mock

import hill_king


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RequestRetries(unittest.TestCase):
    def setUp(self):
        self.identity = mock.patch("board._identity", return_value={"api_key": "k"})
        self.identity.start()
        self.sleep = mock.patch("hill_king.time.sleep")
        self.sleep.start()

    def tearDown(self):
        self.identity.stop()
        self.sleep.stop()

    def test_a_reset_connection_is_retried(self):
        calls = []

        def urlopen(req, timeout=None):
            calls.append(req)
            if len(calls) < 3:
                raise urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
            return FakeResponse(json.dumps({"ok": True}).encode())

        with mock.patch("hill_king.urllib.request.urlopen", urlopen):
            self.assertEqual(hill_king.request("/v1/x"), {"ok": True})
        self.assertEqual(len(calls), 3)

    def test_a_post_keeps_its_idempotency_key_across_retries(self):
        keys = []

        def urlopen(req, timeout=None):
            keys.append(req.get_header("Idempotency-key"))
            if len(keys) == 1:
                raise TimeoutError("timed out")
            return FakeResponse(b"{}")

        with mock.patch("hill_king.urllib.request.urlopen", urlopen):
            hill_king.request("/v1/x", {"a": 1})
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], keys[1])
        self.assertTrue(keys[0])

    def test_a_persistent_failure_is_raised_after_three_tries(self):
        def urlopen(req, timeout=None):
            raise urllib.error.URLError("no route")

        with mock.patch("hill_king.urllib.request.urlopen", urlopen):
            with self.assertRaises(urllib.error.URLError):
                hill_king.request("/v1/x")


if __name__ == "__main__":
    unittest.main()
