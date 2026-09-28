"""python3 -m unittest test_hill_king"""
import io
import json
import unittest
import urllib.error
from unittest import mock

import board
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


class MachineJob(unittest.TestCase):
    """The owner's rule (28.09): stop the machine first, then release control.
    Released first, it keeps running until the idle watchdog, and only its
    holder, creator or operator may stop it."""

    def setUp(self):
        self.calls = []
        self.fail_stop = None
        patches = [
            mock.patch("hill_king.config", return_value={"computer": "c"}),
            mock.patch("hill_king.request", self.request),
            mock.patch("hill_king.job", return_value={"state": "succeeded"}),
            mock.patch("hill_king.output", return_value="out"),
            mock.patch("hill_king.time.sleep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def request(self, path, payload=None, idem=None):
        action = (payload or {}).get("action")
        self.calls.append((path.rsplit("/", 1)[-1], action))
        if path.endswith("/control") and action == "acquire":
            return {"generation": 7}
        if path.endswith("/jobs"):
            return {"job": {"job_id": "j"}}
        if path.endswith("/lifecycle") and action == "stop" and self.fail_stop:
            raise self.fail_stop
        return {"computer": {"runtime": {"state": "running"}}}

    def order(self):
        return [c for c in self.calls if c in (("lifecycle", "stop"), ("control", "release"))]

    def test_the_machine_is_stopped_before_control_is_released(self):
        self.assertEqual(hill_king.machine_job("true"), ("j", "succeeded", "out"))
        self.assertEqual(self.order(), [("lifecycle", "stop"), ("control", "release")])
        self.assertEqual(self.calls[-1], ("control", "release"))

    def test_a_failed_job_still_stops_then_releases(self):
        with mock.patch("hill_king.job", side_effect=board.BoardError("JOB_NOT_FOUND: gone")):
            with self.assertRaises(board.BoardError):
                hill_king.machine_job("true")
        self.assertEqual(self.order(), [("lifecycle", "stop"), ("control", "release")])

    def test_a_refused_stop_does_not_keep_control(self):
        self.fail_stop = board.BoardError("JOBS_RUNNING: a job is running")
        hill_king.machine_job("true")
        self.assertEqual(self.order(), [("lifecycle", "stop"), ("control", "release")])


if __name__ == "__main__":
    unittest.main()
