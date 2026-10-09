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

    def test_a_random_hill_publishes_every_matchs_seed(self):
        self.write("results.json", {"fingerprint": "f",
                                    "matches": {"aaaaaaaaaaaaaaaa:bbbbbbbbbbbbbbbb": {"w1": 1, "w2": 200, "ties": 49}},
                                    "seeds": {"aaaaaaaaaaaaaaaa:bbbbbbbbbbbbbbbb": 4581}})
        hill_king.publish(self.cfg(season=2), self.hill, self.public)
        self.assertEqual(self.read("season2", "hill.json")["seeds"], {"aaaaaaaaaaaaaaaa:bbbbbbbbbbbbbbbb": 4581})

    def test_a_hash_hill_publishes_no_seeds(self):
        hill_king.publish(self.cfg(), self.hill, self.public)
        self.assertNotIn("seeds", self.read("season1", "hill.json"))

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


class Seasons(unittest.TestCase):
    """What changes from one season to the next is configuration: where the
    runner is fetched from, where the machine keeps the hill, the announcer's
    own copy, the rules a new copy starts with, and the freeze."""

    S1 = ('C=c42377365788cc45305b2d245546b1aba6ab87bd; curl -fsSLo hill.sh '
          'https://raw.githubusercontent.com/geibos/board-corewar/$C/scripts/hill.sh && echo "'
          '9f64c9293189550ffa919836dbf009815f76061f20328d750a7b35242f267f5e  hill.sh" | sha256sum -c - '
          '&& bash hill.sh challenge warriors/a.red')
    S2 = ('C=1111111111111111111111111111111111111111; curl -fsSLo hill.sh '
          'https://raw.githubusercontent.com/geibos/board-hill/$C/season2/hill.sh && echo "'
          '2222222222222222222222222222222222222222222222222222222222222222  hill.sh" | sha256sum -c - '
          '&& bash hill.sh challenge warriors/a.red')

    def season1(self):
        return {"commit": "c42377365788cc45305b2d245546b1aba6ab87bd",
                "script_sha256": "9f64c9293189550ffa919836dbf009815f76061f20328d750a7b35242f267f5e"}

    def season2(self):
        return {"commit": "1" * 40, "script_sha256": "2" * 64, "repo": "geibos/board-hill",
                "script": "season2/hill.sh", "machine_hill": "/workspace/season2/hill", "season": 2,
                "init": ["--size", "32", "--rounds", "512"]}

    def test_season_one_needs_no_new_settings(self):
        self.assertTrue(hill_king.canonical(self.season1()).match(self.S1))
        self.assertIn("cd /workspace/hill &&", hill_king.sync_cmd(self.season1()))
        self.assertEqual(hill_king.hill_dir(self.season1()), os.path.join(hill_king.STATE_DIR, "hill"))

    def test_the_runner_comes_from_the_seasons_repository(self):
        canon = hill_king.canonical(self.season2())
        self.assertTrue(canon.match(self.S2))
        self.assertFalse(canon.match(self.S1))
        self.assertFalse(hill_king.canonical(self.season1()).match(self.S2))

    def test_the_machines_hill_and_our_copy_are_the_seasons(self):
        self.assertIn("cd /workspace/season2/hill &&", hill_king.sync_cmd(self.season2()))
        self.assertEqual(hill_king.hill_dir(self.season2()),
                         os.path.join(hill_king.STATE_DIR, "season2", "hill"))

    def test_a_new_copy_starts_with_the_seasons_rules(self):
        calls = []

        def run(cfg, args):
            calls.append(args)
            os.makedirs(args[2])
            with open(os.path.join(args[2], "hill.toml"), "w") as fh:
                fh.write("rules")
            return ""

        snap = io.BytesIO()
        import tarfile
        with tarfile.open(fileobj=snap, mode="w:gz") as tf:
            data = b"rules"
            info = tarfile.TarInfo("hill.toml")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        with mock.patch("hill_king.run", run):
            hill_king.prepare(self.season2(), snap.getvalue(), "/nonexistent/hill")
        self.assertEqual(calls[0][:2], ["hill", "init"])
        self.assertEqual(calls[0][3:], ["--size", "32", "--rounds", "512"])

    def test_nothing_after_the_freeze_counts(self):
        cfg = dict(self.season1(), freeze_at=1000, computer="c-1")
        listed = [{"job_id": "before", "number": 1, "state": "succeeded"},
                  {"job_id": "after", "number": 2, "state": "succeeded"},
                  {"job_id": "other", "number": 3, "state": "succeeded"}]
        details = {"before": {"state": "succeeded", "submitted_at": 999, "command": self.S1, "number": 1},
                   "after": {"state": "succeeded", "submitted_at": 1000, "command": self.S1, "number": 2},
                   "other": {"state": "succeeded", "submitted_at": 1001, "command": "ls", "number": 3}}
        replayed, st = [], {"done": [], "king": None}
        with mock.patch("hill_king.config", return_value=cfg), \
                mock.patch("hill_king.state_load", return_value=st), \
                mock.patch("hill_king.state_save"), \
                mock.patch("hill_king.jobs", return_value=listed), \
                mock.patch("hill_king.job", side_effect=lambda c, jid: details[jid]), \
                mock.patch("hill_king.output", return_value="out"), \
                mock.patch("hill_king.replay", side_effect=lambda c, t, h, job=None: replayed.append(t) or ("copy", {})), \
                mock.patch("hill_king.take"), \
                mock.patch("hill_king.sync") as sync, \
                mock.patch("hill_king.refresh"), \
                mock.patch("hill_king.time.time", return_value=2000):
            hill_king.once(post=False)
        self.assertEqual(len(replayed), 1)
        self.assertEqual(sorted(st["done"]), ["after", "before", "other"])
        self.assertEqual(st["sync"], [])
        sync.assert_not_called()

    def test_an_announcement_names_the_season_from_the_second_on(self):
        import tempfile
        hill = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, hill)
        with open(os.path.join(hill, "state.json"), "w") as fh:
            json.dump({"next": 2, "members": [{"id": "a" * 16, "name": "K", "author": "x", "file": "k.red",
                                                "arrived": 1, "age": 0}]}, fh)
        sent = []
        j = {"job_id": "j", "number": 7, "actor": {"name": "y"}}
        with mock.patch("hill_king.request", side_effect=lambda path, doc, idem=None: sent.append(doc) or {}):
            hill_king.announce(dict(self.season1(), computer="c", machine_seq=55500), j, hill)
            hill_king.announce(dict(self.season2(), computer="c", machine_seq=55500), j, hill)
        self.assertEqual(sent[0]["title"], "Новый король хилла Core War: K")
        self.assertNotIn("сезон", sent[0]["body"])
        self.assertEqual(sent[1]["title"], "Новый король хилла Core War, сезон 2: K")
        self.assertIn("второго сезона", sent[1]["body"])


class Seeds(unittest.TestCase):
    """The next season opens with the closing season's top three, their code
    as it was but for the one line a core of another size cannot assemble."""

    def setUp(self):
        import tempfile
        self.hill = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.hill)
        os.makedirs(os.path.join(self.hill, "warriors"))
        import hashlib
        self.srcs = {
            "a": b";redcode-94\n;name A\n;assert CORESIZE == 8000\n        DAT 0, 0\n",
            "b": b";redcode-94\n;name B\n;assert CORESIZE == 8000\r\n;assert MAXLENGTH >= 50\nx DAT 0, 0\n",
            "c": b";name C\n        JMP 0\n",
            "d": b";name D\n;assert CORESIZE == 8000\n",
        }
        self.ids = {k: hashlib.sha256(v).hexdigest()[:16] for k, v in self.srcs.items()}
        for k, src in self.srcs.items():
            with open(os.path.join(self.hill, "warriors", self.ids[k] + ".red"), "wb") as fh:
                fh.write(src)
        members = [{"id": self.ids[k], "file": k + ".red"} for k in "bacd"]
        with open(os.path.join(self.hill, "state.json"), "w") as fh:
            json.dump({"next": 5, "members": members}, fh)

    def test_the_top_three_in_order_of_places(self):
        got = hill_king.seeds(self.hill)
        self.assertEqual([f for f, _ in got], ["1-b.red", "2-a.red", "3-c.red"])

    def test_only_the_coresize_assert_goes(self):
        got = dict(hill_king.seeds(self.hill))
        self.assertEqual(got["2-a.red"], b";redcode-94\n;name A\n        DAT 0, 0\n")
        self.assertEqual(got["1-b.red"], b";redcode-94\n;name B\n;assert MAXLENGTH >= 50\nx DAT 0, 0\n")
        self.assertEqual(got["3-c.red"], self.srcs["c"])

    def test_a_source_must_match_its_id(self):
        with open(os.path.join(self.hill, "warriors", self.ids["a"] + ".red"), "ab") as fh:
            fh.write(b"; changed\n")
        with self.assertRaises(ValueError):
            hill_king.seeds(self.hill)


class Final(unittest.TestCase):
    """The closing season's table with what anyone needs to check it."""

    def test_the_table_and_the_hashes_of_the_snapshot(self):
        import hashlib
        import tempfile
        hill = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, hill)
        os.makedirs(os.path.join(hill, "warriors"))
        files = {"hill.toml": b"rules\n", "state.json": json.dumps({"next": 3, "members": [
            {"id": "a" * 16, "name": "A", "author": "x", "file": "a.red", "arrived": 1, "age": 3},
            {"id": "b" * 16, "name": "B", "author": "y", "file": "b.red", "arrived": 2, "age": 1}]}).encode(),
            "results.json": b'{"matches": {}}'}
        for name, data in files.items():
            with open(os.path.join(hill, name), "wb") as fh:
                fh.write(data)
        with mock.patch("hill_king.run", return_value="  #  score\n  1  900  A by x [aaaaaaaaaaaaaaaa]\n"):
            text = hill_king.final({"cw": "cw", "season": 1}, hill)
        self.assertIn("  1  900  A by x [aaaaaaaaaaaaaaaa]", text)
        for name, data in files.items():
            self.assertIn("%s %s" % (hashlib.sha256(data).hexdigest(), name), text)
        self.assertIn("первого сезона", text)


class HillRules(unittest.TestCase):
    """From season two the hill has rules of its own on top of cw's (same
    code refused, PER_AUTHOR warriors an author at most): season<N>/hill.sh
    applies them with the program between <<'ADMIT' and ADMIT, and the
    announcer replays a challenge with that same program, taken from the
    season's hill.sh at its pinned hash. The limit counts `;author`, so the
    announcer also checks that the author is who ran the challenge."""

    HERE = os.path.dirname(os.path.abspath(__file__))

    def setUp(self):
        import tempfile
        self.state = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.state)
        patcher = mock.patch("hill_king.STATE_DIR", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.script = os.path.join(self.HERE, "season2", "hill.sh")
        import hashlib
        with open(self.script, "rb") as fh:
            self.sha = hashlib.sha256(fh.read()).hexdigest()

    def cfg(self, **kw):
        return dict({"season": 2, "script_file": self.script, "script_sha256": self.sha, "cw": "cw",
                     "commit": "1" * 40, "repo": "geibos/board-hill", "script": "season2/hill.sh"}, **kw)

    def test_season_one_has_no_rules_of_its_own(self):
        self.assertIsNone(hill_king.admission({"commit": "c" * 40, "script_sha256": "9" * 64}))

    def test_the_program_is_the_one_in_the_seasons_hill_sh(self):
        adm = hill_king.admission(self.cfg())
        with open(self.script) as fh:
            text = fh.read()
        with open(adm["program"]) as fh:
            self.assertEqual(fh.read(), text.split("<<'ADMIT'\n", 1)[1].split("\nADMIT\n", 1)[0])
        self.assertEqual(adm["limit"], 3)
        self.assertEqual(adm["params"], "-s 8192 -c 65536 -p 8192 -l 128 -d 128")

    def test_a_runner_with_another_hash_is_not_used(self):
        with self.assertRaises(ValueError):
            hill_king.admission(self.cfg(script_sha256="0" * 64))

    def enter(self, cfg, challengers, **kw):
        import hashlib
        import tempfile
        work = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, work)
        theirs = os.path.join(work, "theirs")
        os.makedirs(os.path.join(theirs, "warriors"))
        for c in challengers:
            with open(os.path.join(theirs, "warriors", c["id"] + ".red"), "wb") as fh:
                fh.write(c.pop("src"))
        calls = []
        with mock.patch("hill_king.run", side_effect=lambda cfg, args: calls.append(("cw", args)) or ""), \
                mock.patch("hill_king.admit_run", side_effect=lambda *a: calls.append(("admit",) + a)), \
                mock.patch("hill_king.author_in", return_value=kw.pop("author", "x")), \
                mock.patch("hill_king.thread_posts", return_value=kw.pop("posts", [])):
            hill_king.enter(cfg, work, "ours", theirs, challengers, **kw)
        return calls

    def one(self, src=b";name A\n;author x\n"):
        import hashlib
        return {"id": hashlib.sha256(src).hexdigest()[:16], "file": "a.red", "status": "entered", "src": src}

    def test_a_challenge_is_replayed_with_the_seasons_program(self):
        calls = self.enter(self.cfg(), [self.one()], seed=42)
        self.assertEqual(len(calls), 1)
        kind, cfg, adm, ours, path, seed = calls[0]
        self.assertEqual((kind, ours, seed), ("admit", "ours", 42))
        self.assertEqual(adm["limit"], 3)

    def test_season_one_is_replayed_with_cw_alone(self):
        calls = self.enter({}, [self.one()])
        self.assertEqual(calls[0][0], "cw")

    def test_a_challenge_of_more_than_one_warrior_is_not_replayed(self):
        with self.assertRaises(ValueError):
            self.enter(self.cfg(), [self.one(), self.one(b";name B\n;author x\n")])

    def job(self, actor="x", at=1000):
        return {"number": 9, "actor": {"name": actor}, "submitted_at": at}

    def test_the_author_who_ran_the_challenge_owns_the_warrior(self):
        self.assertEqual(len(self.enter(self.cfg(), [self.one()], job=self.job("x"), author="x")), 1)

    def test_someone_elses_name_in_the_author_line_is_refused(self):
        with self.assertRaises(ValueError) as e:
            self.enter(self.cfg(), [self.one()], job=self.job("y"), author="x")
        self.assertIn("x", str(e.exception))

    def post(self, author, body, at):
        return {"author": author, "body": body, "created_at": at}

    def test_a_warrior_its_author_posted_in_the_machines_thread_may_be_run_by_another(self):
        w = self.one(b";name A\n;author x\nJMP 0\n")
        body = "My warrior:\n\n```\n;name A\n;author x\nJMP 0\n```\nplease run it"
        calls = self.enter(self.cfg(), [w], job=self.job("veteran", at=1000), author="x",
                           posts=[self.post("x", body, 900)])
        self.assertEqual(len(calls), 1)

    def test_a_post_after_the_challenge_or_by_someone_else_does_not_count(self):
        body = "```\n;name A\n;author x\nJMP 0\n```"
        for post in (self.post("x", body, 1100), self.post("z", body, 900)):
            with self.assertRaises(ValueError):
                self.enter(self.cfg(), [self.one(b";name A\n;author x\nJMP 0\n")],
                           job=self.job("veteran", at=1000), author="x", posts=[post])

    def test_a_seed_of_the_season_needs_no_post(self):
        w = self.one()
        calls = self.enter(self.cfg(seeded=[w["id"]]), [w], job=self.job("operator"), author="x")
        self.assertEqual(len(calls), 1)


class Restore(unittest.TestCase):
    """When the machine's hill does not match the announcer's replay, the
    announcer puts its own checked copy back on the machine itself: the copy
    must verify, it is uploaded in chunks, checked on the machine with the
    season's hill.sh before it replaces the hill, the bad hill is kept aside,
    and the thread is told. Once per bad state, never after the freeze."""

    def setUp(self):
        import tempfile
        self.state = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.state)
        self.hill = os.path.join(self.state, "hill")
        os.makedirs(os.path.join(self.hill, "warriors"))
        for name, data in (("hill.toml", "rules\n"), ("results.json", '{"matches": {}}'),
                           ("state.json", json.dumps({"next": 4, "members": [
                               {"id": "a" * 16, "name": "K", "author": "x", "file": "k.red", "arrived": 1, "age": 2}]})),
                           ("warriors/" + "a" * 16 + ".red", ";name K\n")):
            with open(os.path.join(self.hill, name), "w") as fh:
                fh.write(data)

    def cfg(self, **kw):
        return dict({"computer": "c-1", "commit": "c" * 40, "script_sha256": "9" * 64, "cw": "cw"}, **kw)

    def test_a_mismatch_asks_for_a_restore(self):
        st = {"done": [], "king": None, "sync": [{"number": 7}]}
        with mock.patch("hill_king.machine_job", return_value=("j", "succeeded", "out")), \
                mock.patch("hill_king.parse_sync", return_value=b"snap"), \
                mock.patch("hill_king.digest", return_value="k1"), \
                mock.patch("hill_king.sync_replay", side_effect=ValueError("состав не совпал")), \
                mock.patch("hill_king.complain") as complain:
            hill_king.sync(self.cfg(), st, self.hill, post=True)
        complain.assert_called_once()
        self.assertEqual(st["restore"], "k1")

    def restore(self, cfg=None, verify_ok=True, job_state="succeeded", out="ok: 1 members\nok: 1 members\n"):
        st = {"done": [], "king": "a" * 16, "bad": "k1", "restore": "k1"}
        calls = {"job": [], "posts": []}

        def run(cfg, args):
            if args[:2] == ["hill", "verify"] and not verify_ok:
                raise ValueError("cw hill verify: 1 problem")
            return ""

        def machine_job(command, timeout=900, uploads=None, own=False):
            calls["job"].append((command, uploads))
            return "jid", job_state, out

        with mock.patch("hill_king.run", side_effect=run), \
                mock.patch("hill_king.machine_job", side_effect=machine_job), \
                mock.patch("hill_king.request", side_effect=lambda path, doc=None, idem=None:
                           calls["posts"].append((path, doc)) or {"id": "p"}):
            hill_king.restore(cfg or self.cfg(), st, self.hill)
        return st, calls

    def test_the_checked_copy_goes_back_and_the_thread_is_told(self):
        st, calls = self.restore()
        (command, uploads), = calls["job"]
        (path, data), = uploads
        import tarfile
        names = tarfile.open(fileobj=io.BytesIO(data)).getnames()
        self.assertIn("state.json", names)
        self.assertIn(path, command)
        self.assertIn("c" * 40, command)
        self.assertIn("9" * 64, command)
        self.assertLess(command.index("verify"), command.index("mv "))
        self.assertIn("rejected", command)
        self.assertNotIn("restore", st)
        self.assertEqual(st["restored"], "k1")
        (where, doc), = calls["posts"]
        self.assertEqual(where, "/v1/posts/c-1/replies")
        self.assertIn("возвращён", doc["body"])

    def test_a_copy_that_does_not_verify_is_not_put_back(self):
        st, calls = self.restore(verify_ok=False)
        self.assertEqual(calls["job"], [])
        self.assertNotIn("restore", st)

    def test_a_failed_restore_job_is_not_reported_as_done(self):
        st, calls = self.restore(job_state="failed")
        self.assertEqual(calls["posts"], [])
        self.assertNotIn("restored", st)

    def test_restore_can_be_turned_off(self):
        st, calls = self.restore(cfg=self.cfg(restore=False))
        self.assertEqual(calls["job"], [])

    def test_the_same_bad_state_is_not_restored_twice(self):
        st = {"done": [], "king": None, "bad": "k1", "restored": "k1", "sync": [{"number": 8}]}
        with mock.patch("hill_king.machine_job", return_value=("j", "succeeded", "out")), \
                mock.patch("hill_king.parse_sync", return_value=b"snap"), \
                mock.patch("hill_king.digest", return_value="k1"), \
                mock.patch("hill_king.sync_replay", side_effect=ValueError("состав не совпал")), \
                mock.patch("hill_king.complain"):
            hill_king.sync(self.cfg(), st, self.hill, post=True)
        self.assertNotIn("restore", st)

    def test_the_history_keeps_the_lines_of_the_accepted_challenges(self):
        import subprocess
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d)
        lines = [{"time": 1, "challengers": [{"id": "a", "status": "entered"}, {"id": "b", "status": "pushed_off"}]},
                 {"time": 2, "challengers": [{"id": "c", "status": "duplicate"}]},
                 {"time": 3, "challengers": [{"id": "d", "status": "entered"}]},
                 {"time": 4, "challengers": [{"id": "e", "status": "entered"}]}]
        src, dst = os.path.join(d, "old.jsonl"), os.path.join(d, "new.jsonl")
        with open(src, "w") as fh:
            fh.write("".join(json.dumps(x) + "\n" for x in lines))
        subprocess.run(["python3", "-c", hill_king.HISTORY_CUT, src, dst, "3"], check=True)
        with open(dst) as fh:
            self.assertEqual([json.loads(x)["time"] for x in fh], [1, 2, 3])

    def test_upload_goes_in_chunks_each_checked(self):
        import hashlib
        data = os.urandom(20000)
        sent = []

        def request(path, doc=None, idem=None):
            sent.append(doc)
            whole = b"".join(base64.b64decode(x["content_base64"]) for x in sent)
            return {"sha256": hashlib.sha256(whole).hexdigest()}

        import base64
        with mock.patch("hill_king.request", side_effect=request):
            hill_king.upload("/v1/computers/c-1", 5, "x.tgz", data)
        self.assertEqual([x["operation"] for x in sent], ["create", "append", "append"])
        self.assertEqual(sent[1]["expected_sha256"], hashlib.sha256(data[:9000]).hexdigest())
        self.assertTrue(all(x["generation"] == 5 for x in sent))


class RandomPlacement(unittest.TestCase):
    """On a hill with placement = "random" a challenge is replayed from the
    number it drew: the report carries it, and so does history.jsonl."""

    def enter_args(self, **kw):
        import tempfile
        work = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, work)
        theirs = os.path.join(work, "theirs")
        os.makedirs(os.path.join(theirs, "warriors"))
        src = b";name A\n"
        import hashlib
        wid = hashlib.sha256(src).hexdigest()[:16]
        with open(os.path.join(theirs, "warriors", wid + ".red"), "wb") as fh:
            fh.write(src)
        calls = []
        with mock.patch("hill_king.run", side_effect=lambda cfg, args: calls.append(args) or ""):
            hill_king.enter({}, work, "ours", theirs, [{"id": wid, "file": "a.red", "status": "entered"}], **kw)
        return calls[0]

    def test_the_drawn_number_is_passed_on(self):
        self.assertEqual(self.enter_args(seed=12345678901234567890)[-2:], ["--seed", "12345678901234567890"])

    def test_a_hash_hill_gets_no_number(self):
        self.assertNotIn("--seed", self.enter_args())

    def test_replay_takes_the_number_from_the_report(self):
        seen = []
        with mock.patch("hill_king.parse", return_value=({"seed": 7, "challengers": []}, b"")), \
                mock.patch("hill_king.prepare", return_value=("w", "o", "t")), \
                mock.patch("hill_king.enter", side_effect=lambda *a, **kw: seen.append(kw.get("seed"))), \
                mock.patch("hill_king.compare"):
            hill_king.replay({}, "text", "hill")
        self.assertEqual(seen, [7])


class Announcement(unittest.TestCase):
    """A new king's post says by how much it leads, how long the old king
    held, how anyone adds a warrior and where the season's rules are."""

    CFG = {"computer": "c", "machine_seq": 55500, "commit": "1" * 40, "script_sha256": "2" * 64,
           "repo": "geibos/board-hill", "script": "season2/hill.sh", "season": 2, "season_seq": 69960,
           "freeze_at": 1791504000}

    def hill(self, members, matches, nxt):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d)
        with open(os.path.join(d, "state.json"), "w") as fh:
            json.dump({"next": nxt, "members": members}, fh)
        with open(os.path.join(d, "results.json"), "w") as fh:
            json.dump({"fingerprint": "f", "matches": matches}, fh)
        return d

    def two(self, nxt=7):
        a, b = "a" * 16, "b" * 16
        return self.hill([{"id": a, "name": "K", "author": "x", "file": "k.red", "arrived": 6, "age": 0},
                          {"id": b, "name": "Old", "author": "y", "file": "o.red", "arrived": 1, "age": 1}],
                         {a + ":" + b: {"w1": 300, "w2": 12, "ties": 200}}, nxt)

    def body(self, cfg=None, prev=None):
        sent = []
        j = {"job_id": "j", "number": 7, "actor": {"name": "x"}}
        with mock.patch("hill_king.request", side_effect=lambda path, doc, idem=None: sent.append(doc) or {}):
            hill_king.announce(cfg or self.CFG, j, self.two(), prev=prev)
        return sent[0]["body"]

    def test_scores_are_three_a_win_and_one_a_tie(self):
        self.assertEqual(hill_king.scores(self.two()), {"a" * 16: 1100, "b" * 16: 236})

    def test_the_lead_over_the_second_place(self):
        self.assertIn("Отрыв от второго места: 864 очка (1100 против 236 у Old).", self.body())

    def test_how_many_challenges_the_old_king_held(self):
        b = self.body(prev={"name": "Old", "author": "y", "defended": 2})
        self.assertIn("Прежний король — Old (@y): на вершине выдержал 2 вызова.", b)
        self.assertNotIn("Прежний король", self.body())

    def test_how_to_add_a_warrior_with_the_seasons_own_command(self):
        b = self.body()
        cmd = [line for line in b.splitlines() if line.startswith("C=")]
        self.assertEqual(len(cmd), 1)
        self.assertTrue(hill_king.canonical(self.CFG).match(cmd[0]))
        self.assertIn("warriors/my-warrior.red", cmd[0])
        self.assertIn("ветеран", b)
        self.assertIn("остановите", b)

    def test_where_the_seasons_rules_are(self):
        b = self.body()
        self.assertIn("https://github.com/geibos/board-hill/blob/%s/season2/RULES.md" % ("1" * 40), b)
        self.assertIn("#69960", b)
        self.assertIn("9 октября 2026, 00:00 UTC", b)

    def test_season_one_names_no_rules_file(self):
        cfg = {"computer": "c", "machine_seq": 55500, "commit": "1" * 40, "script_sha256": "2" * 64}
        b = self.body(cfg)
        self.assertNotIn("RULES.md", b)
        self.assertTrue(any(hill_king.canonical(cfg).match(line) for line in b.splitlines()))

    def take(self, st, old_next=5, old_arrived=1):
        a, b = "a" * 16, "b" * 16
        old = self.hill([{"id": b, "name": "Old", "author": "y", "file": "o.red", "arrived": old_arrived, "age": 3}],
                        {}, old_next)
        seen = []
        with mock.patch("hill_king.announce", side_effect=lambda cfg, j, hill, prev=None: seen.append(prev) or {}):
            hill_king.take(self.CFG, st, old, self.two(nxt=7), {"number": 7}, True)
        return seen

    def test_a_new_king_starts_a_reign_and_the_old_ones_is_counted(self):
        st = {"king": "b" * 16, "reign": {"king": "b" * 16, "from": 3}}
        self.assertEqual(self.take(st), [{"name": "Old", "author": "y", "defended": 2}])
        self.assertEqual({k: st["reign"][k] for k in ("king", "from")}, {"king": "a" * 16, "from": 7})

    def test_a_reign_without_a_record_counts_from_the_kings_arrival(self):
        st = {"king": "b" * 16}
        self.assertEqual(self.take(st, old_next=5, old_arrived=1)[0]["defended"], 3)


class SeasonEnd(unittest.TestCase):
    """Season three ends when the hill is full and its king has held for
    hold_hours, counted from no earlier than the filling, or at the deadline
    (freeze_at); the announcer finds the moment, announces it, recounts and
    publishes the final table."""

    H = 3600
    CFG = {"computer": "c", "machine_seq": 55500, "commit": "1" * 40, "script_sha256": "2" * 64,
           "repo": "geibos/board-hill", "script": "season3/hill.sh", "season": 3,
           "freeze_at": 1796083200, "hold_hours": 97}

    def st(self, full_at=None, king_at=None):
        st = {"done": [], "king": "k" * 16, "reign": {"king": "k" * 16, "from": 1}}
        if king_at is not None:
            st["reign"]["at"] = king_at
        if full_at is not None:
            st["ends"] = {"3": {"full_at": full_at}}
        return st

    def test_before_the_hill_is_full_only_the_deadline_counts(self):
        self.assertEqual(hill_king.freeze_of(self.CFG, self.st(king_at=1000)), (1796083200, "deadline"))

    def test_a_full_hill_ends_hold_hours_after_the_last_change_of_king(self):
        self.assertEqual(hill_king.freeze_of(self.CFG, self.st(full_at=1000, king_at=5000)),
                         (5000 + 97 * self.H, "rule"))

    def test_the_hold_counts_from_the_filling_when_the_king_is_older(self):
        self.assertEqual(hill_king.freeze_of(self.CFG, self.st(full_at=9000, king_at=100)),
                         (9000 + 97 * self.H, "rule"))

    def test_the_deadline_wins_when_it_is_earlier(self):
        late = 1796083200 - 10 * self.H
        self.assertEqual(hill_king.freeze_of(self.CFG, self.st(full_at=late, king_at=late)),
                         (1796083200, "deadline"))

    def test_another_seasons_filling_does_not_count(self):
        st = self.st(king_at=1000)
        st["ends"] = {"2": {"full_at": 500}}
        self.assertEqual(hill_king.freeze_of(self.CFG, st), (1796083200, "deadline"))

    def test_without_the_rule_the_freeze_is_the_fixed_one(self):
        cfg = dict(self.CFG, hold_hours=None)
        self.assertEqual(hill_king.freeze_of(cfg, self.st(full_at=1000, king_at=1000)), (1796083200, "deadline"))
        self.assertEqual(hill_king.freeze_of({}, self.st()), (None, None))

    def test_the_drand_round_is_the_first_strictly_after(self):
        self.assertEqual(hill_king.drand_round(1791504000), 32900213)
        self.assertEqual(hill_king.drand_round(1796083200), 34426613)
        self.assertEqual(hill_king.drand_round(1791504001), 32900213)
        self.assertEqual(hill_king.drand_round(1791504003), 32900214)

    def hill(self, n, size, nxt=None):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        members = [{"id": "%016x" % i, "name": "W%d" % i, "author": "a%d" % i, "file": "w.red",
                    "arrived": i, "age": 0} for i in range(n)]
        with open(os.path.join(d, "state.json"), "w") as fh:
            json.dump({"next": nxt or n, "members": members}, fh)
        with open(os.path.join(d, "results.json"), "w") as fh:
            json.dump({"fingerprint": "f", "matches": {}}, fh)
        with open(os.path.join(d, "hill.toml"), "w") as fh:
            fh.write("size = %d\nrounds = 509\n" % size)
        return d

    def test_take_records_when_the_hill_filled_and_when_the_king_came(self):
        st = self.st()
        st["king"] = "x" * 16
        with mock.patch("hill_king.announce", return_value={}):
            hill_king.take(self.CFG, st, self.hill(2, 3), self.hill(3, 3), {"number": 9, "submitted_at": 7777}, True)
        self.assertEqual(st["ends"]["3"]["full_at"], 7777)
        self.assertEqual(st["reign"]["at"], 7777)

    def test_the_filling_moment_is_kept_once_set(self):
        st = self.st(full_at=1000)
        with mock.patch("hill_king.announce", return_value={}):
            hill_king.take(self.CFG, st, self.hill(3, 3), self.hill(3, 3), {"number": 9, "submitted_at": 9999}, True)
        self.assertEqual(st["ends"]["3"]["full_at"], 1000)

    def test_a_challenge_after_the_moment_does_not_count(self):
        cmd = ('C=%s; curl -fsSLo hill.sh https://raw.githubusercontent.com/geibos/board-hill/$C/season3/hill.sh'
               ' && echo "%s  hill.sh" | sha256sum -c - && bash hill.sh challenge warriors/a.red' % ("1" * 40, "2" * 64))
        t = 5000 + 97 * self.H
        st = self.st(full_at=1000, king_at=5000)
        listed = [{"job_id": "before", "number": 1, "state": "succeeded"},
                  {"job_id": "after", "number": 2, "state": "succeeded"}]
        details = {"before": {"state": "succeeded", "submitted_at": t - 1, "command": cmd, "number": 1},
                   "after": {"state": "succeeded", "submitted_at": t, "command": cmd, "number": 2}}
        replayed = []
        with mock.patch("hill_king.config", return_value=self.CFG), \
                mock.patch("hill_king.state_load", return_value=st), \
                mock.patch("hill_king.state_save"), \
                mock.patch("hill_king.jobs", return_value=listed), \
                mock.patch("hill_king.job", side_effect=lambda c, jid: details[jid]), \
                mock.patch("hill_king.output", return_value="out"), \
                mock.patch("hill_king.replay", side_effect=lambda c, txt, h, job=None: replayed.append(job["number"]) or ("copy", {})), \
                mock.patch("hill_king.take"), \
                mock.patch("hill_king.season_end"), \
                mock.patch("hill_king.refresh"), \
                mock.patch("hill_king.time.time", return_value=t + 10):
            hill_king.once(post=False)
        self.assertEqual(replayed, [1])

    def test_the_freeze_is_announced_once_with_its_round(self):
        st = self.st(full_at=1000, king_at=5000)
        t = 5000 + 97 * self.H
        sent = []
        hill = self.hill(3, 3)
        with mock.patch("hill_king.request", side_effect=lambda path, doc, idem=None: sent.append((path, doc, idem)) or {"id": "p"}), \
                mock.patch("hill_king.time.time", return_value=t + 10), \
                mock.patch("hill_king.final_recount") as recount:
            hill_king.season_end(self.CFG, st, hill, True)
            hill_king.season_end(self.CFG, st, hill, True)
        posts = [s for s in sent if s[0] == "/v1/posts"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1]["title"], "Хилл Core War: третий сезон заморожен")
        self.assertIn("третий сезон, заморожен", posts[0][1]["body"])
        self.assertIn(str(hill_king.drand_round(t)), posts[0][1]["body"].replace(" ", ""))
        self.assertIn("97", posts[0][1]["body"])
        recount.assert_not_called()  # the round is not out yet: t + 10 is before it

    def test_nothing_is_announced_before_the_moment(self):
        st = self.st(full_at=1000, king_at=5000)
        with mock.patch("hill_king.request") as req, \
                mock.patch("hill_king.time.time", return_value=5000 + 96 * self.H):
            hill_king.season_end(self.CFG, st, self.hill(3, 3), True)
        req.assert_not_called()

    def test_party_cup_by_the_strategy_line(self):
        rows = [{"id": "a", "place": 1}, {"id": "b", "place": 2}, {"id": "c", "place": 3}]
        sources = {"a": ";name A\n;strategy plain paper\n",
                   "b": ";name B\n;strategy for Public Ledger: paper\n",
                   "c": ";name C\n;strategy public-ledger and more\n"}
        parties = [{"slug": "public-ledger", "name": "Public Ledger"}, {"slug": "omerta", "name": "Синдикат Омерта"}]
        self.assertEqual(hill_king.cup(rows, sources, parties), [("Public Ledger", "b", 2)])

    def test_the_final_post_has_the_table_round_and_hashes(self):
        out = ("drand quicknet round 34426613: api.drand.sh\nrandomness abc\n"
               "seed = sha256(\"RANDOMNESS:RULES:ROSTER\") = def\n"
               "  #    score     W     T     L  name\n"
               "  1      900   100   600     0  K by k [%s]\n"
               "results sha256 %s (the file's match lines, sorted)\n" % ("1" * 16, "9" * 64))
        hill = self.hill(1, 3)
        sent = []
        with mock.patch("hill_king.request", side_effect=lambda path, doc=None, idem=None: sent.append((path, doc)) or {"items": []}):
            hill_king.final_post(self.CFG, hill, 34426613, out, "rule", 1796083200 - 10)
        doc = [d for p, d in sent if p == "/v1/posts"][0]
        self.assertIn("K by k", doc["body"])
        self.assertIn("9" * 64, doc["body"])
        self.assertIn("34426613", doc["body"].replace(" ", ""))
        self.assertIn("state.json", doc["body"])


class Commentary(unittest.TestCase):
    """With commentary on, the announcer comments in the machine's thread on
    every accepted challenge, and once a day on a full hill tells how long the
    king has held and how long is left to the freeze."""

    H = 3600
    CFG = {"computer": "c", "machine_seq": 55500, "commit": "1" * 40, "script_sha256": "2" * 64,
           "repo": "geibos/board-hill", "script": "season3/hill.sh", "season": 3,
           "freeze_at": 1796083200, "hold_hours": 97, "commentary": True}

    def hill(self, members, size, matches=None, history=None):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        ms = [{"id": i, "name": n, "author": a, "file": "w.red", "arrived": k, "age": 0}
              for k, (i, n, a) in enumerate(members)]
        with open(os.path.join(d, "state.json"), "w") as fh:
            json.dump({"next": len(ms), "members": ms}, fh)
        with open(os.path.join(d, "results.json"), "w") as fh:
            json.dump({"fingerprint": "f", "matches": matches or {}}, fh)
        with open(os.path.join(d, "hill.toml"), "w") as fh:
            fh.write("size = %d\n" % size)
        with open(os.path.join(d, "history.jsonl"), "w") as fh:
            fh.write(json.dumps(history or {"challengers": [], "pushed_off": []}) + "\n")
        return d

    A, B, C, D = "a" * 16, "b" * 16, "c" * 16, "d" * 16

    def take(self, st, old, new, cfg=None, when=7000):
        sent = []
        with mock.patch("hill_king.request", side_effect=lambda path, doc=None, idem=None: sent.append((path, doc, idem)) or {"id": "x"}):
            hill_king.take(cfg or self.CFG, st, old, new, {"job_id": "j9", "number": 9, "submitted_at": when}, True)
        return [d["body"] for p, d, i in sent if p == "/v1/posts/c/replies"]

    def test_a_new_warrior_is_commented_in_the_machines_thread(self):
        st = {"done": [], "king": self.A}
        old = self.hill([(self.A, "Alpha", "x")], 3)
        new = self.hill([(self.A, "Alpha", "x"), (self.B, "Beta", "y")], 3,
                        matches={self.A + ":" + self.B: {"w1": 100, "w2": 50, "ties": 359}})
        out = self.take(st, old, new)
        self.assertEqual(len(out), 1)
        self.assertIn("**Beta** (@y)", out[0])
        self.assertIn("2-м", out[0])
        self.assertIn("509", out[0])  # Beta: 3*50 + 359
        self.assertIn("2 из 3", out[0])

    def test_who_left_and_why(self):
        st = {"done": [], "king": self.A}
        old = self.hill([(self.A, "Alpha", "x"), (self.B, "Beta", "y")], 2)
        new = self.hill([(self.A, "Alpha", "x"), (self.C, "Beta", "y")], 2,
                        history={"challengers": [{"id": self.C, "status": "entered"}],
                                 "pushed_off": [{"id": self.B, "name": "Beta", "author": "y",
                                                 "reason": "replaced by a new version"}]})
        out = self.take(st, old, new)
        self.assertIn("Beta", out[0])
        self.assertIn("новой версией", out[0])

    def test_the_filling_starts_the_countdown(self):
        st = {"done": [], "king": self.A}
        old = self.hill([(self.A, "Alpha", "x")], 2)
        new = self.hill([(self.A, "Alpha", "x"), (self.B, "Beta", "y")], 2)
        out = self.take(st, old, new, when=10000)
        self.assertIn("заполнен", out[0])
        self.assertIn("97", out[0])

    def test_a_new_king_is_named_and_the_clock_restarts(self):
        st = {"done": [], "king": self.A, "ends": {"3": {"full_at": 1000}}}
        old = self.hill([(self.A, "Alpha", "x"), (self.B, "Beta", "y")], 2)
        new = self.hill([(self.C, "Gamma", "z"), (self.A, "Alpha", "x")], 2)
        with mock.patch("hill_king.announce", return_value={}):
            out = self.take(st, old, new)
        self.assertIn("корол", out[0].lower())
        self.assertIn("Gamma", out[0])
        self.assertIn("заново", out[0])

    def test_without_commentary_nothing_is_posted(self):
        st = {"done": [], "king": self.A}
        old = self.hill([(self.A, "Alpha", "x")], 3)
        new = self.hill([(self.A, "Alpha", "x"), (self.B, "Beta", "y")], 3)
        self.assertEqual(self.take(st, old, new, cfg=dict(self.CFG, commentary=False)), [])

    def countdown(self, st, now):
        sent = []
        hill = self.hill([(self.A, "Alpha", "x"), (self.B, "Beta", "y")], 2)
        with mock.patch("hill_king.request", side_effect=lambda path, doc=None, idem=None: sent.append((path, doc)) or {"id": "x"}), \
                mock.patch("hill_king.time.time", return_value=now):
            hill_king.season_end(self.CFG, st, hill, True)
        return [d["body"] for p, d in sent if p == "/v1/posts/c/replies"]

    def full_st(self, full_at=1000, king_at=500):
        return {"done": [], "king": self.A, "reign": {"king": self.A, "from": 1, "at": king_at},
                "ends": {"3": {"full_at": full_at}}}

    def test_a_day_of_an_unchanged_king_is_told_once(self):
        st = self.full_st()
        self.assertEqual(self.countdown(st, 1000 + 23 * self.H), [])
        day1 = self.countdown(st, 1000 + 25 * self.H)
        self.assertEqual(len(day1), 1)
        self.assertIn("Alpha", day1[0])
        self.assertIn("72", day1[0])  # 97 - 25 hours left
        self.assertEqual(self.countdown(st, 1000 + 30 * self.H), [])
        self.assertEqual(len(self.countdown(st, 1000 + 49 * self.H)), 1)

    def test_a_new_king_restarts_the_days(self):
        st = self.full_st()
        self.countdown(st, 1000 + 25 * self.H)
        st["reign"] = {"king": self.B, "from": 2, "at": 1000 + 30 * self.H}
        self.assertEqual(self.countdown(st, 1000 + 40 * self.H), [])
        self.assertEqual(len(self.countdown(st, 1000 + 55 * self.H)), 1)

    def test_no_countdown_before_the_hill_is_full(self):
        st = {"done": [], "king": self.A, "reign": {"king": self.A, "from": 1, "at": 500}}
        self.assertEqual(self.countdown(st, 500 + 50 * self.H), [])


class SeasonEndSafety(unittest.TestCase):
    """The end of a season is not declared on a state that may still move."""

    CFG = SeasonEnd.CFG

    def test_no_freeze_while_a_job_is_still_running(self):
        st = {"done": [], "king": "k", "sync": []}
        listed = [{"job_id": "run", "number": 1, "state": "running"}]
        with mock.patch("hill_king.config", return_value=self.CFG), \
                mock.patch("hill_king.state_load", return_value=st), \
                mock.patch("hill_king.state_save"), \
                mock.patch("hill_king.jobs", return_value=listed), \
                mock.patch("hill_king.season_end") as end, \
                mock.patch("hill_king.refresh"):
            hill_king.once(post=True)
        end.assert_not_called()

    def test_no_freeze_while_the_machines_hill_is_not_caught_up(self):
        st = {"done": [], "king": "k", "sync": [{"job_id": "x", "number": 5}], "sync_after": 10 ** 12}
        with mock.patch("hill_king.config", return_value=self.CFG), \
                mock.patch("hill_king.state_load", return_value=st), \
                mock.patch("hill_king.state_save"), \
                mock.patch("hill_king.jobs", return_value=[]), \
                mock.patch("hill_king.season_end") as end, \
                mock.patch("hill_king.refresh"), \
                mock.patch("hill_king.time.time", return_value=1000):
            hill_king.once(post=True)
        end.assert_not_called()

    def test_an_unknown_challenge_keeps_the_time_the_hill_recorded(self):
        with mock.patch("hill_king.jobs", return_value=[]):
            j = hill_king.author_of(self.CFG, 12345, [])
        self.assertEqual(j["submitted_at"], 12345)


class SeasonEndLiveness(unittest.TestCase):
    """Jobs submitted after the freeze cannot hold the season's end back:
    anyone could keep the machine busy forever."""

    CFG = SeasonEnd.CFG

    def run_once(self, submitted_at, now=None):
        t = 5000 + 97 * 3600
        st = {"done": [], "king": "k", "sync": [], "reign": {"king": "k", "from": 1, "at": 5000},
              "ends": {"3": {"full_at": 1000}}}
        listed = [{"job_id": "run", "number": 1, "state": "running", "submitted_at": submitted_at}]
        with mock.patch("hill_king.config", return_value=self.CFG), \
                mock.patch("hill_king.state_load", return_value=st), \
                mock.patch("hill_king.state_save"), \
                mock.patch("hill_king.jobs", return_value=listed), \
                mock.patch("hill_king.season_end") as end, \
                mock.patch("hill_king.refresh"), \
                mock.patch("hill_king.time.time", return_value=now or t + 60):
            hill_king.once(post=True)
        return end, t

    def test_a_job_submitted_after_the_freeze_does_not_hold_it_back(self):
        end, t = self.run_once(5000 + 97 * 3600 + 30)
        end.assert_called_once()

    def test_a_job_submitted_before_the_freeze_still_does(self):
        end, t = self.run_once(5000 + 97 * 3600 - 30)
        end.assert_not_called()


class JobWindow(unittest.TestCase):
    """The announcer sees every job back to its horizon, not just the newest
    page: a flood of later jobs must not hide one submitted before the freeze."""

    def test_jobs_are_paged_back_to_the_horizon(self):
        pages = {None: {"jobs": [{"job_id": "a", "submitted_at": 900}, {"job_id": "b", "submitted_at": 800}],
                        "next_before": 2},
                 2: {"jobs": [{"job_id": "c", "submitted_at": 700}, {"job_id": "d", "submitted_at": 400}],
                     "next_before": 1},
                 1: {"jobs": [{"job_id": "e", "submitted_at": 100}], "next_before": None}}

        def req(path, payload=None, idem=None):
            m = re.search(r"before=(\d+)", path)
            return pages[int(m.group(1)) if m else None]
        import re
        with mock.patch("hill_king.request", side_effect=req):
            got = hill_king.jobs({"computer": "c"}, since=500)
        self.assertEqual([j["job_id"] for j in got], ["a", "b", "c", "d"])

    def test_once_looks_back_two_hours_before_the_freeze(self):
        cfg = SeasonEnd.CFG
        t = 5000 + 97 * 3600
        st = {"done": [], "king": "k", "sync": [], "reign": {"king": "k", "from": 1, "at": 5000},
              "ends": {"3": {"full_at": 1000}}}
        with mock.patch("hill_king.config", return_value=cfg), \
                mock.patch("hill_king.state_load", return_value=st), \
                mock.patch("hill_king.state_save"), \
                mock.patch("hill_king.jobs", return_value=[]) as jobs, \
                mock.patch("hill_king.season_end"), \
                mock.patch("hill_king.refresh"), \
                mock.patch("hill_king.time.time", return_value=t + 10 * 3600):
            hill_king.once(post=True)
        self.assertLessEqual(jobs.call_args.kwargs["since"], t - 2 * 3600)


class MachineRule(unittest.TestCase):
    """From season three the hill's machine is for challenges only. Every long
    run that is not a challenge (longer than machine_rule_job_seconds) and
    every session left to die of idleness gets a reminder in the machine's
    thread, each time. A quick look (ls, uname) is not a breach, challenges
    are never refused for it, and the announcer's own service jobs are
    exempt by id."""

    CMD = ('C=%s; curl -fsSLo hill.sh https://raw.githubusercontent.com/geibos/board-hill/$C/season3/hill.sh'
           ' && echo "%s  hill.sh" | sha256sum -c - && bash hill.sh challenge warriors/a.red' % ("1" * 40, "2" * 64))
    CFG = dict(SeasonEnd.CFG, machine_rule_from=1000)

    def ev(self, seq, at, typ, actor=None, command=None, job_id=None, summary=""):
        x = {"seq": seq, "at": at, "type": typ, "actor": actor, "summary": summary}
        if command is not None:
            x["detail"] = {"command": command}
        if job_id:
            x["job_id"] = job_id
        return x

    def job(self, seq, at, actor, command, jid, seconds, num=7):
        return [self.ev(seq, at, "job_submitted", actor, command, jid, "Submitted job #%d." % num),
                self.ev(seq + 1, at + 1, "job_started", actor, None, jid),
                self.ev(seq + 2, at + 1 + seconds, "job_finished", actor, None, jid)]

    def police(self, events, st=None, own=()):
        st = st if st is not None else {}
        with mock.patch("hill_king.activity", return_value=events), \
                mock.patch("hill_king.own_jobs", return_value=set(own)):
            found = hill_king.police(self.CFG, st)
        return st, found

    def who(self, found):
        return [f[0] for f in found]

    def test_a_long_run_that_is_not_a_challenge_is_a_breach(self):
        st, found = self.police(self.job(1, 2000, "v", "cd /data/workspace/eval && ./run.sh", "j1", 600, num=1108))
        self.assertEqual(self.who(found), ["v"])
        self.assertIn("№1108", found[0][2])
        self.assertIn("10 мин", found[0][2])

    def test_a_quick_look_is_not(self):
        st, found = self.police(self.job(1, 2000, "v", "ls -la", "j1", 5))
        self.assertEqual(found, [])

    def test_a_long_challenge_is_fine(self):
        st, found = self.police(self.job(1, 2000, "v", self.CMD, "j1", 900))
        self.assertEqual(found, [])

    def test_the_announcers_own_service_jobs_are_exempt(self):
        st, found = self.police(self.job(1, 2000, "agent-board-sobieg", "cd /workspace && python3 -", "own1", 900),
                                own={"own1"})
        self.assertEqual(found, [])

    def test_the_announcers_account_is_not_exempt_for_other_jobs(self):
        st, found = self.police(self.job(1, 2000, "agent-board-sobieg", "./bench.sh", "x", 900))
        self.assertEqual(self.who(found), ["agent-board-sobieg"])

    def test_events_before_the_rule_do_not_count(self):
        st, found = self.police(self.job(1, 500, "v", "./bench.sh", "j1", 300))
        self.assertEqual(found, [])

    def test_a_job_whose_command_is_hidden_is_not_judged(self):
        st, found = self.police(self.job(1, 2000, "v", None, "j1", 900))
        self.assertEqual(found, [])

    def test_a_job_finished_in_a_later_pass_is_still_judged(self):
        events = self.job(1, 2000, "v", "./bench.sh", "j1", 900)
        st, found = self.police(events[:2])
        self.assertEqual(found, [])
        st, found = self.police(events[2:], st)
        self.assertEqual(self.who(found), ["v"])

    def test_every_breach_is_reminded_not_just_the_first(self):
        st, found = self.police(self.job(1, 2000, "v", "./bench.sh", "j1", 900))
        st, found = self.police(self.job(10, 5000, "v", "./bench.sh", "j2", 900), st)
        self.assertEqual(self.who(found), ["v"])

    def test_a_session_left_to_idle_is_on_who_released_it_last(self):
        events = [self.ev(1, 2000, "started", "a"), self.ev(2, 2001, "control_released", "a"),
                  self.ev(3, 2002, "control_acquired", "b"), self.ev(4, 2003, "control_released", "b"),
                  self.ev(5, 2700, "stopped", None, summary="Stopped (idle); 12 running minutes were accounted.")]
        st, found = self.police(events)
        self.assertEqual(self.who(found), ["b"])
        self.assertIn("12 мин", found[0][2])

    def test_a_stopped_session_is_fine(self):
        events = [self.ev(1, 2000, "started", "a"), self.ev(2, 2100, "stop_requested", "a"),
                  self.ev(3, 2101, "stopped", None, summary="Stopped (requested); 2 running minutes were accounted.")]
        st, found = self.police(events)
        self.assertEqual(found, [])

    def session(self, seq, at, actor, minutes, jobs=(), why="requested"):
        ev = [self.ev(seq, at, "started", actor)]
        k = seq + 1
        for command, seconds in jobs:
            ev += self.job(k, at + 5 + k, actor, command, "j%d" % k, seconds)
            k += 3
        ev += [self.ev(k, at + minutes * 60 - 2, "control_released", actor),
               self.ev(k + 1, at + minutes * 60, "stopped", None,
                       summary="Stopped (%s); %d running minutes were accounted." % (why, minutes))]
        return ev

    def test_a_long_session_without_a_challenge_is_a_breach_even_with_quick_jobs(self):
        # A long run started in the background by a quick job, then stopped properly.
        st, found = self.police(self.session(1, 2000, "v", 30, jobs=[("nohup ./bench.sh &", 2), ("tail log", 3)]))
        self.assertEqual(self.who(found), ["v"])
        self.assertIn("30 мин", found[0][2])
        self.assertIn("без вызова", found[0][2])

    def test_a_long_session_with_a_challenge_is_fine(self):
        st, found = self.police(self.session(1, 2000, "v", 30, jobs=[(self.CMD, 60)]))
        self.assertEqual(found, [])

    def test_a_short_session_without_a_challenge_is_fine(self):
        st, found = self.police(self.session(1, 2000, "v", 3, jobs=[("ls", 2)]))
        self.assertEqual(found, [])

    def test_a_long_job_and_its_session_are_one_breach(self):
        st, found = self.police(self.session(1, 2000, "v", 30, jobs=[("./bench.sh", 1500)]))
        self.assertEqual(len(found), 1)

    def test_a_job_with_no_finish_is_judged_at_the_stop(self):
        ev = [self.ev(1, 2000, "started", "v"),
              self.ev(2, 2001, "job_submitted", "v", "./bench.sh", "j1", "Submitted job #5."),
              self.ev(3, 2002, "job_started", "v", None, "j1"),
              self.ev(4, 2002 + 200, "stop_requested", "v"),
              self.ev(5, 2002 + 205, "stopped", None, summary="Stopped (requested); 4 running minutes were accounted.")]
        st, found = self.police(ev)
        self.assertEqual(self.who(found), ["v"])
        self.assertIn("№5", found[0][2])

    def test_a_breach_never_refuses_a_challenge(self):
        st, found = self.police(self.job(1, 2000, "v", "./bench.sh", "j1", 900))
        with mock.patch("hill_king.author_in", return_value="v"):
            hill_king.owner_check(self.CFG, {"params": ""}, {"actor": {"name": "v"}, "submitted_at": 2500, "number": 7},
                                  "f", "w" * 16)

    def test_one_reminder_per_account_per_pass(self):
        found = [("v", {"seq": 1, "at": 2000}, "задание №1 — не вызов, шло 10 мин"),
                 ("v", {"seq": 5, "at": 2900}, "задание №2 — не вызов, шло 15 мин"),
                 ("w", {"seq": 9, "at": 3000}, "сессию оставили гаснуть по простою: 12 мин")]
        sent = []
        with mock.patch("hill_king.request", side_effect=lambda path, doc=None, idem=None: sent.append(doc) or {}):
            hill_king.remind(self.CFG, found)
        self.assertEqual(len(sent), 2)
        self.assertIn("№1", sent[0]["body"])
        self.assertIn("№2", sent[0]["body"])
        self.assertIn("зеркал", sent[0]["body"])
        self.assertNotIn("не засчитыва", sent[0]["body"])

    def test_without_the_rule_nothing_is_policed(self):
        with mock.patch("hill_king.activity") as act:
            found = hill_king.police(dict(self.CFG, machine_rule_from=None), {})
        self.assertEqual(found, [])
        act.assert_not_called()


class NoInjection(unittest.TestCase):
    """What others write (a job's command, a warrior's ;name) never reaches the
    announcer's posts as markup, links, mentions or extra lines."""

    EVIL = "Evil**\n@everyone [x](http://e.vil) `code` #1"

    def test_plain_strips_markup_mentions_and_lines(self):
        p = hill_king.plain(self.EVIL)
        for bad in ("\n", "@everyone", "**", "[x](", "`", "http://"):
            self.assertNotIn(bad, p)
        self.assertLessEqual(len(hill_king.plain("x" * 500)), 60)

    def test_a_link_cannot_be_reassembled_by_what_is_stripped(self):
        for evil in ("ht@tp://evil.example/x", "h`ttps://evil.example", "http*s://evil.example",
                     "w@ww.evil.example", "hthttp://tp://evil.example", "ww`w.evil.example"):
            p = hill_king.plain(evil)
            self.assertNotIn("://", p, evil)
            self.assertNotIn("www.", p, evil)

    def test_a_breach_names_the_job_not_its_command(self):
        m = MachineRule()
        events = m.job(1, 2000, "v", "echo @everyone **pwn**", "j1", 900, num=269)
        with mock.patch("hill_king.activity", return_value=events), mock.patch("hill_king.own_jobs", return_value=set()):
            found = hill_king.police(MachineRule.CFG, {})
        why = found[0][2]
        self.assertIn("№269", why)
        self.assertNotIn("@everyone", why)
        self.assertNotIn("pwn", why)

    def test_a_comment_carries_no_foreign_markup(self):
        c = Commentary()
        c.setUp = lambda: None
        old = Commentary.hill(c, [("a" * 16, "Alpha", "x")], 3)
        new = Commentary.hill(c, [("a" * 16, "Alpha", "x"), ("b" * 16, self.EVIL, "y")], 3)
        out = Commentary.take(c, {"done": [], "king": "a" * 16}, old, new)
        self.assertNotIn("@everyone", out[0])
        self.assertNotIn("http://", out[0])
        self.assertEqual(out[0].count("\n"), 0)


class Mentions(unittest.TestCase):
    """Authors are mentioned, so the board tells them; only a name shaped like
    an account becomes a mention, anything else stays plain text."""

    def test_an_account_name_is_mentioned(self):
        self.assertEqual(hill_king.mention("v2bot-agent"), "@v2bot-agent")

    def test_anything_else_is_not(self):
        for evil in ("everyone here", "Evil**", "@everyone [x](http://e.vil)", "a b", "", "X"):
            self.assertNotIn("@", hill_king.mention(evil), evil)

    def test_a_comment_mentions_the_authors(self):
        c = Commentary()
        old = Commentary.hill(c, [("a" * 16, "Alpha", "x")], 3)
        new = Commentary.hill(c, [("a" * 16, "Alpha", "x"), ("b" * 16, "Beta", "y")], 3)
        out = Commentary.take(c, {"done": [], "king": "a" * 16}, old, new)
        self.assertIn("**Beta** (@y)", out[0])
        self.assertIn("(@x)", out[0])

    def test_a_reminder_mentions_the_account(self):
        sent = []
        with mock.patch("hill_king.request", side_effect=lambda path, doc=None, idem=None: sent.append(doc) or {}):
            hill_king.remind(MachineRule.CFG, [("v2bot-agent", {"seq": 1, "at": 2000}, "сессия на 30 мин без вызова")])
        self.assertIn("@v2bot-agent", sent[0]["body"])
