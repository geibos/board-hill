"""How season two's hill.sh lets a warrior in (the program between <<'ADMIT'
and ADMIT), run as it is written there, on small hills with a local cw:
a warrior whose assembled code is on the hill already is refused, and an
author over the per-author limit loses their own weakest warrior instead of
pushing someone else's off.

  CW=/path/to/cw python3 -m unittest test_admit

Without cw (CW or `cw` on PATH) the tests are skipped.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CW = os.environ.get("CW") or shutil.which("cw") or os.path.expanduser(
    "~/projects/board-corewar/target/release/cw")


def read(path, mode="r"):
    with open(path, mode) as fh:
        return fh.read()


def program(season="season2"):
    text = read(os.path.join(HERE, season, "hill.sh"))
    return text.split("<<'ADMIT'\n", 1)[1].split("\nADMIT\n", 1)[0]


def dwarf(step, name, author):
    return (";redcode-94\n;name %s\n;author %s\n"
            "        ADD.AB  #%d, bomb\n"
            "        MOV.I   bomb, @bomb\n"
            "        JMP     -2\n"
            "bomb    DAT.F   #0, #0\n" % (name, author, step))


def dead(name, author, n):
    """Dies on its first move: the weakest warrior there is. n tells the code apart."""
    return ";redcode-94\n;name %s\n;author %s\n        DAT.F   #%d, #%d\n" % (name, author, n, n)


@unittest.skipUnless(os.path.exists(CW), "needs cw")
class Admit(unittest.TestCase):
    SEASON = "season2"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.script = os.path.join(self.tmp, "admit.py")
        with open(self.script, "w") as fh:
            fh.write(program(self.SEASON))

    def warrior(self, text, name):
        path = os.path.join(self.tmp, name + ".red")
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def hill(self, name, size, *files, placement=None):
        h = os.path.join(self.tmp, name)
        subprocess.run([CW, "hill", "init", h, "--size", str(size), "--rounds", "20"]
                       + (["--placement", placement] if placement else []), check=True, capture_output=True)
        for f in files:
            subprocess.run([CW, "hill", "challenge", h, f], check=True, capture_output=True)
        return h

    def copy(self, h, name):
        dest = os.path.join(self.tmp, name)
        shutil.copytree(h, dest, ignore=shutil.ignore_patterns(".lock"))
        return dest

    def admit(self, h, f, k, seed=None):
        env = dict(os.environ)
        env.pop("HILL_SEED", None)
        if seed is not None:
            env["HILL_SEED"] = str(seed)
        return subprocess.run(["python3", self.script, CW, h, str(k), "", f],
                              capture_output=True, text=True, env=env)

    def members(self, h):
        return [m["id"] for m in json.loads(read(os.path.join(h, "state.json")))["members"]]

    def authors(self, h):
        return [m["author"] for m in json.loads(read(os.path.join(h, "state.json")))["members"]]

    def verify(self, h):
        return subprocess.run([CW, "hill", "verify", h], capture_output=True, text=True)

    def full_hill(self):
        """A full hill of 3: two dwarfs by A and, the weakest, a dead warrior by B."""
        a1 = self.warrior(dwarf(3039, "A1", "A"), "a1")
        a2 = self.warrior(dwarf(2365, "A2", "A"), "a2")
        b1 = self.warrior(dead("B1", "B", 0), "b1")
        return self.hill("full", 3, a1, a2, b1)

    def test_the_same_code_under_another_name_is_refused(self):
        h = self.hill("h", 3, self.warrior(dwarf(3039, "Kiln", "A"), "kiln"))
        before = read(os.path.join(h, "state.json"))
        r = self.admit(h, self.warrior(dwarf(3039, "Another Name", "B"), "copy"), 3)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("same code as Kiln", r.stderr)
        self.assertEqual(read(os.path.join(h, "state.json")), before)

    def test_under_the_limit_it_is_an_ordinary_challenge(self):
        h = self.full_hill()
        plain = self.copy(h, "plain")
        f = self.warrior(dwarf(97, "C1", "C"), "c1")
        subprocess.run([CW, "hill", "challenge", plain, f], check=True, capture_output=True)
        r = self.admit(h, f, 2)
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in ("state.json", "results.json"):
            self.assertEqual(read(os.path.join(h, name)), read(os.path.join(plain, name)))

    def test_over_the_limit_the_authors_weakest_leaves_not_someone_elses(self):
        h = self.full_hill()
        b1 = self.members(h)[self.authors(h).index("B")]
        f = self.warrior(dwarf(1547, "A3", "A"), "a3")
        plain = self.copy(h, "plain")
        subprocess.run([CW, "hill", "challenge", plain, f], check=True, capture_output=True)
        self.assertNotIn(b1, self.members(plain), "the test needs B's warrior to be what cw pushes off")
        r = self.admit(h, f, 2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(b1, self.members(h))
        self.assertEqual(sorted(self.authors(h)), ["A", "A", "B"])
        report = json.loads(r.stdout)
        gone = [g for g in report["pushed_off"] if g["author"] == "A"]
        self.assertEqual(len(gone), 1)
        self.assertNotIn(gone[0]["id"], self.members(h))
        self.assertIn("per author", gone[0]["reason"])

    def test_the_challenger_itself_leaves_when_it_is_the_authors_weakest(self):
        h = self.full_hill()
        before = sorted(self.members(h))
        r = self.admit(h, self.warrior(dead("A-weak", "A", 1), "aw"), 2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(self.members(h)), before)
        report = json.loads(r.stdout)
        self.assertEqual(report["challengers"][0]["status"], "pushed_off")

    def test_after_a_displacement_the_hill_verifies(self):
        h = self.full_hill()
        self.assertEqual(self.admit(h, self.warrior(dwarf(1547, "A3", "A"), "a3"), 2).returncode, 0)
        v = self.verify(h)
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)

    def test_the_history_line_names_who_left_and_why(self):
        h = self.full_hill()
        self.admit(h, self.warrior(dwarf(1547, "A3", "A"), "a3"), 2)
        last = json.loads(read(os.path.join(h, "history.jsonl")).splitlines()[-1])
        self.assertEqual([g["author"] for g in last["pushed_off"] if "per author" in g["reason"]], ["A"])

    def test_a_limit_of_zero_is_an_ordinary_challenge(self):
        h = self.full_hill()
        plain = self.copy(h, "plain")
        f = self.warrior(dwarf(1547, "A3", "A"), "a3")
        subprocess.run([CW, "hill", "challenge", plain, f], check=True, capture_output=True)
        self.assertEqual(self.admit(h, f, 0).returncode, 0)
        for name in ("state.json", "results.json"):
            self.assertEqual(read(os.path.join(h, name)), read(os.path.join(plain, name)))

    def test_on_a_random_hill_the_drawn_seed_replays_the_displacement(self):
        a1 = self.warrior(dwarf(3039, "A1", "A"), "a1")
        a2 = self.warrior(dwarf(2365, "A2", "A"), "a2")
        b1 = self.warrior(dead("B1", "B", 0), "b1")
        h = self.hill("rnd", 3, a1, a2, b1, placement="random")
        again = self.copy(h, "again")
        f = self.warrior(dwarf(1547, "A3", "A"), "a3")
        r = self.admit(h, f, 2)
        self.assertEqual(r.returncode, 0, r.stderr)
        seed = json.loads(r.stdout)["seed"]
        self.assertIsNotNone(seed)
        self.assertEqual(self.admit(again, f, 2, seed=seed).returncode, 0)
        for name in ("state.json", "results.json"):
            self.assertEqual(read(os.path.join(h, name)), read(os.path.join(again, name)))
        self.assertEqual(self.verify(h).returncode, 0)


@unittest.skipUnless(os.path.exists(CW), "needs cw")
class SeasonThreeAdmit(Admit):
    """Season three's program: everything above, and a new version of a
    warrior (the same ;name by the same author) replaces the old one."""
    SEASON = "season3"

    def versions(self):
        """A hill of 3 with A's "X", B's "Y" and a dead warrior by C."""
        x = self.warrior(dwarf(3039, "X", "A"), "x1")
        y = self.warrior(dwarf(2365, "Y", "B"), "y1")
        c = self.warrior(dead("C1", "C", 0), "c1")
        return self.hill("versions", 3, x, y, c)

    def id_of(self, h, name, author):
        state = json.loads(read(os.path.join(h, "state.json")))
        return [m["id"] for m in state["members"] if m["name"] == name and m["author"] == author]

    def test_a_new_version_replaces_the_old_one(self):
        h = self.versions()
        old = self.id_of(h, "X", "A")
        r = self.admit(h, self.warrior(dwarf(1547, "X", "A"), "x2"), 3)
        self.assertEqual(r.returncode, 0, r.stderr)
        now = self.id_of(h, "X", "A")
        self.assertEqual(len(now), 1)
        self.assertNotEqual(now, old)
        self.assertNotIn(old[0], self.members(h))
        gone = [g for g in json.loads(r.stdout)["pushed_off"] if g["id"] == old[0]]
        self.assertEqual(len(gone), 1)
        self.assertEqual(gone[0]["reason"], "replaced by a new version")

    def test_the_rest_of_the_hill_stays(self):
        h = self.versions()
        others = [i for i in self.members(h) if i not in self.id_of(h, "X", "A")]
        self.admit(h, self.warrior(dwarf(1547, "X", "A"), "x2"), 3)
        for i in others:
            self.assertIn(i, self.members(h))

    def test_a_weaker_new_version_still_replaces_the_old_one(self):
        h = self.versions()
        old = self.id_of(h, "X", "A")
        r = self.admit(h, self.warrior(dead("X", "A", 7), "xdead"), 3)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(old[0], self.members(h))
        self.assertEqual(len(self.id_of(h, "X", "A")), 1)

    def test_the_same_name_by_another_author_replaces_nothing(self):
        h = self.versions()
        old = self.id_of(h, "X", "A")
        r = self.admit(h, self.warrior(dwarf(1547, "X", "B"), "xb"), 3)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.id_of(h, "X", "A"), old)
        self.assertFalse([g for g in json.loads(r.stdout)["pushed_off"] if g.get("reason") == "replaced by a new version"])

    def test_after_a_replacement_the_hill_verifies(self):
        h = self.versions()
        self.assertEqual(self.admit(h, self.warrior(dwarf(1547, "X", "A"), "x2"), 3).returncode, 0)
        v = self.verify(h)
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)

    def test_the_history_line_names_the_replaced_version(self):
        h = self.versions()
        old = self.id_of(h, "X", "A")
        self.admit(h, self.warrior(dwarf(1547, "X", "A"), "x2"), 3)
        last = json.loads(read(os.path.join(h, "history.jsonl")).splitlines()[-1])
        self.assertEqual([g["id"] for g in last["pushed_off"] if g["reason"] == "replaced by a new version"], old)


if __name__ == "__main__":
    unittest.main()
