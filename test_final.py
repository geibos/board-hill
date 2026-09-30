"""The final recount in season2/hill.sh, run as it is written there (the
program between <<'FINAL' and FINAL), on a small hill with a local cw.

  CW=/path/to/cw python3 -m unittest test_final

Without cw (CW or `cw` on PATH) the tests are skipped.
"""
import hashlib
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
TESTDATA = os.path.expanduser("~/projects/board-corewar/testdata")
RANDOMNESS = "e4677d12f0a8927406cc67d03e853acc66ff620259a94b644d3ffe4e00597a51"


def read(path, mode="r"):
    with open(path, mode) as fh:
        return fh.read()


def program():
    text = read(os.path.join(HERE, "season2", "hill.sh"))
    return text.split("<<'FINAL'\n", 1)[1].split("\nFINAL\n", 1)[0]


@unittest.skipUnless(os.path.exists(CW) and os.path.isdir(TESTDATA), "needs cw and board-corewar's testdata")
class FinalRecount(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.hill = os.path.join(self.tmp, "hill")
        subprocess.run([CW, "hill", "init", self.hill, "--rounds", "20"], check=True, capture_output=True)
        subprocess.run([CW, "hill", "challenge", self.hill] + [os.path.join(TESTDATA, f) for f in
                                                              ("dwarf.red", "imp.red", "edge.red")],
                       check=True, capture_output=True)
        self.script = os.path.join(self.tmp, "final.py")
        with open(self.script, "w") as fh:
            fh.write(program())

    def run_final(self, save, limit="0", jobs="1", randomness=RANDOMNESS):
        env = dict(os.environ, FINAL_RANDOMNESS=randomness)
        return subprocess.run(["python3", self.script, self.hill, CW, "32900213", "unused", "3", jobs, limit, save],
                              capture_output=True, text=True, env=env)

    def seed(self):
        rules = hashlib.sha256(read(os.path.join(self.hill, "hill.toml"), "rb")).hexdigest()
        ids = sorted(m["id"] for m in json.loads(read(os.path.join(self.hill, "state.json")))["members"])
        roster = hashlib.sha256("\n".join(ids).encode()).hexdigest()
        return hashlib.sha256(("%s:%s:%s" % (RANDOMNESS, rules, roster)).encode()).hexdigest(), ids

    def test_every_match_is_the_pair_with_its_documented_seed(self):
        save = os.path.join(self.tmp, "final.txt")
        r = self.run_final(save)
        self.assertEqual(r.returncode, 0, r.stderr)
        seed, ids = self.seed()
        lines = read(save).split("\n")[1:-1]
        self.assertEqual(len(lines), 3 * 3)
        for line in lines:
            a, b, k, s, w1, w2, t = line.split()
            self.assertLess(a, b)
            d = hashlib.sha256(("%s:%s:%s:%s" % (seed, a, b, k)).encode()).digest()
            self.assertEqual(int(s), int.from_bytes(d[:8], "big") % (8001 - 200))
            pair = json.loads(subprocess.run(
                [CW, "pair", os.path.join(self.hill, "warriors", a + ".red"),
                 os.path.join(self.hill, "warriors", b + ".red"), "--rounds", "20", "--seed", s, "--json"],
                check=True, capture_output=True, text=True).stdout)["result"]
            self.assertEqual((int(w1), int(w2), int(t)), (pair["w1"], pair["w2"], pair["ties"]))
        self.assertIn("given in FINAL_RANDOMNESS, not fetched: a test", r.stdout)
        self.assertIn("seed = sha256(\"RANDOMNESS:RULES:ROSTER\") = %s" % seed, r.stdout)

    def test_the_table_adds_up_the_file(self):
        save = os.path.join(self.tmp, "final.txt")
        out = self.run_final(save).stdout
        rows = re.findall(r"^\s*(\d+)\s+(-?\d+)\s+(\d+)\s+(\d+)\s+(\d+)  .* \[([0-9a-f]{16})\]$", out, re.M)
        self.assertEqual(len(rows), 3)
        totals = {}
        for line in read(save).split("\n")[1:-1]:
            a, b, _, _, w1, w2, t = line.split()
            for who, won, lost in ((a, int(w1), int(w2)), (b, int(w2), int(w1))):
                w, tt, l = totals.get(who, (0, 0, 0))
                totals[who] = (w + won, tt + int(t), l + lost)
        scores = []
        for place, score, w, t, l, wid in rows:
            self.assertEqual(totals[wid], (int(w), int(t), int(l)))
            self.assertEqual(int(score), 3 * int(w) + int(t))
            scores.append(int(score))
        self.assertEqual(scores, sorted(scores, reverse=True))

    def results_sha(self, out):
        return re.search(r"^results sha256 ([0-9a-f]{64})", out, re.M).group(1)

    def test_a_stopped_recount_goes_on_to_the_same_result(self):
        whole = self.run_final(os.path.join(self.tmp, "whole.txt"))
        parts = os.path.join(self.tmp, "parts.txt")
        first = self.run_final(parts, limit="0.000001")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("not finished", first.stdout)
        self.assertNotIn("results sha256", first.stdout)
        rest = self.run_final(parts)
        self.assertEqual(rest.returncode, 0, rest.stderr)
        self.assertEqual(self.results_sha(rest.stdout), self.results_sha(whole.stdout))

    def test_a_file_for_another_recount_is_not_continued(self):
        save = os.path.join(self.tmp, "final.txt")
        self.run_final(save, limit="0.000001")
        other = self.run_final(save, randomness="00" * 32)
        self.assertEqual(other.returncode, 2)
        self.assertIn("another recount", other.stderr)

    def test_more_jobs_the_same_result(self):
        one = self.run_final(os.path.join(self.tmp, "one.txt"))
        four = self.run_final(os.path.join(self.tmp, "four.txt"), jobs="4")
        self.assertEqual(self.results_sha(one.stdout), self.results_sha(four.stdout))


if __name__ == "__main__":
    unittest.main()
