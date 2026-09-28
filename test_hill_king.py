"""python3 -m unittest test_hill_king"""
import http.client
import io
import json
import os
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

    def test_a_stop_cut_off_mid_answer_does_not_keep_control(self):
        # The road to the board drops connections mid-body, and a broken body
        # is not JSON: neither is a URLError, and neither may skip the release.
        for failure in (http.client.IncompleteRead(b"{"), json.JSONDecodeError("x", "{", 1)):
            self.calls.clear()
            self.fail_stop = failure
            self.assertEqual(hill_king.machine_job("true"), ("j", "succeeded", "out"))
            self.assertEqual(self.order(), [("lifecycle", "stop"), ("control", "release")])

    def test_even_an_unforeseen_stop_failure_releases_control(self):
        self.fail_stop = RuntimeError("unforeseen")
        with self.assertRaises(RuntimeError):
            hill_king.machine_job("true")
        self.assertEqual(self.order(), [("lifecycle", "stop"), ("control", "release")])


class Publish(unittest.TestCase):
    """What the mirror's viewer reads: the announcer's own, replayed copy of
    the hill, and every warrior of the season with its name."""

    TOML = (
        'size = 20\nrounds = 250\ntie_break = "older"\n\n'
        "[params]\ncore_size = 8000\ncycles = 80000\nprocesses = 8000\nlength = 100\ndistance = 100\n\n"
        "[points]\nwin = 3\ntie = 1\nloss = 0\n"
    )

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp)
        self.hill = os.path.join(self.tmp, "hill")
        self.public = os.path.join(self.tmp, "public")
        os.makedirs(os.path.join(self.hill, "warriors"))
        self.srcs = {"aaaaaaaaaaaaaaaa": b";name A\n", "bbbbbbbbbbbbbbbb": b";name B\n", "cccccccccccccccc": b";name Gone\n"}
        for wid, src in self.srcs.items():
            with open(os.path.join(self.hill, "warriors", wid + ".red"), "wb") as fh:
                fh.write(src)
        members = [{"id": "bbbbbbbbbbbbbbbb", "name": "B", "author": "y", "file": "b.red", "arrived": 2, "age": 0},
                   {"id": "aaaaaaaaaaaaaaaa", "name": "A", "author": "x", "file": "a.red", "arrived": 1, "age": 1}]
        self.write("state.json", {"next": 4, "members": members})
        self.write("results.json", {"fingerprint": "f", "matches": {"aaaaaaaaaaaaaaaa:bbbbbbbbbbbbbbbb": {"w1": 1, "w2": 200, "ties": 49}}})
        with open(os.path.join(self.hill, "hill.toml"), "w") as fh:
            fh.write(self.TOML)
        self.listed = []
        p = mock.patch("hill_king.run", self.run_cw)
        p.start()
        self.addCleanup(p.stop)

    def write(self, name, doc):
        with open(os.path.join(self.hill, name), "w") as fh:
            json.dump(doc, fh)

    def run_cw(self, cfg, args):
        self.assertEqual(args[:2], ["list", "--json"])
        path = args[-1]
        self.listed.append(os.path.basename(path))
        name = open(path).read().split(";name ", 1)[1].strip()
        return json.dumps({"warrior": {"file": path, "name": name, "author": "au-" + name, "length": 1}, "start": 0, "code": []})

    def cfg(self, **kw):
        return dict({"computer": "c-1", "machine_seq": 55500, "cw": "cw"}, **kw)

    def read(self, *path):
        with open(os.path.join(self.public, *path)) as fh:
            return json.load(fh)

    def test_the_hill_and_every_warrior_of_the_season_are_published(self):
        hill_king.publish(self.cfg(), self.hill, self.public)
        doc = self.read("season1", "hill.json")
        self.assertEqual(doc["season"], 1)
        self.assertEqual(doc["computer"], "c-1")
        self.assertEqual(doc["rules"]["params"]["core_size"], 8000)
        self.assertEqual(doc["rules"]["points"], {"win": 3, "tie": 1, "loss": 0})
        self.assertEqual([m["id"] for m in doc["members"]], ["bbbbbbbbbbbbbbbb", "aaaaaaaaaaaaaaaa"])
        self.assertEqual(doc["results"]["aaaaaaaaaaaaaaaa:bbbbbbbbbbbbbbbb"]["w2"], 200)
        names = {w["id"]: (w["name"], w["author"]) for w in doc["warriors"]}
        self.assertEqual(names["cccccccccccccccc"], ("Gone", "au-Gone"))
        for wid, src in self.srcs.items():
            with open(os.path.join(self.public, "season1", "warriors", wid + ".red"), "rb") as fh:
                self.assertEqual(fh.read(), src)

    def test_the_index_names_the_seasons_machine_and_king(self):
        hill_king.publish(self.cfg(), self.hill, self.public)
        idx = self.read("index.json")
        self.assertEqual(len(idx["seasons"]), 1)
        s = idx["seasons"][0]
        self.assertEqual((s["season"], s["computer"], s["machine_seq"], s["path"]), (1, "c-1", 55500, "season1/"))
        self.assertEqual(s["king"]["name"], "B")

    def test_a_new_season_keeps_the_old_one(self):
        hill_king.publish(self.cfg(), self.hill, self.public)
        hill_king.publish(self.cfg(season=2, computer="c-2", machine_seq=70000), self.hill, self.public)
        idx = self.read("index.json")
        self.assertEqual([(s["season"], s["computer"]) for s in idx["seasons"]], [(1, "c-1"), (2, "c-2")])
        self.assertTrue(os.path.exists(os.path.join(self.public, "season1", "hill.json")))

    def test_names_are_listed_once_per_warrior(self):
        hill_king.publish(self.cfg(), self.hill, self.public)
        self.assertEqual(len(self.listed), 3)
        hill_king.publish(self.cfg(), self.hill, self.public)
        self.assertEqual(len(self.listed), 3, "known ids reuse their names")

    def test_a_failed_swap_keeps_the_season_that_was(self):
        hill_king.publish(self.cfg(), self.hill, self.public)
        real = os.rename

        def rename(src, dst):
            if os.path.basename(dst) == "season1" and os.path.basename(src).startswith(".season1-"):
                raise OSError("disk trouble")
            return real(src, dst)

        with mock.patch("hill_king.os.rename", rename):
            with self.assertRaises(OSError):
                hill_king.publish(self.cfg(), self.hill, self.public)
        self.assertEqual(self.read("season1", "hill.json")["season"], 1)
        self.assertEqual(sorted(os.listdir(self.public)), ["index.json", "season1"], "no leftovers")

    def test_everything_is_readable_by_the_web_server(self):
        hill_king.publish(self.cfg(), self.hill, self.public)
        for root, dirs, files in os.walk(self.public):
            for n in dirs + files:
                mode = os.stat(os.path.join(root, n)).st_mode
                self.assertTrue(mode & 0o004, "%s is not world-readable" % n)
        self.assertEqual(sorted(os.listdir(self.public)), ["index.json", "season1"], "no leftovers")


if __name__ == "__main__":
    unittest.main()
