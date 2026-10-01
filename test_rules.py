"""Season two's rules are written in three places that must agree: RULES in
season2/hill.sh (what a new hill is made with), RULES_SUM there (what every
run checks hill.toml against) and season2/hill.toml (the copy people read).

  CW=/path/to/cw python3 -m unittest test_rules

Without cw (CW or `cw` on PATH) the tests are skipped.
"""
import hashlib
import os
import re
import shlex
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


@unittest.skipUnless(os.path.exists(CW), "needs cw")
class SeasonTwoRules(unittest.TestCase):
    def setUp(self):
        self.script = read(os.path.join(HERE, "season2", "hill.sh"))
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def var(self, name):
        value = re.search(r'^%s="?([^"\n]*)"?$' % name, self.script, re.M).group(1)
        return re.sub(r"\$(\w+)", lambda m: self.var(m.group(1)), value)

    def made(self):
        hill = os.path.join(self.tmp, "hill")
        subprocess.run([CW, "hill", "init", hill] + shlex.split(self.var("RULES")), check=True, capture_output=True)
        return read(os.path.join(hill, "hill.toml"), "rb")

    def test_a_new_hill_has_the_pinned_rules(self):
        self.assertEqual(hashlib.sha256(self.made()).hexdigest(), self.var("RULES_SUM"))

    def test_the_published_copy_is_the_pinned_rules(self):
        self.assertEqual(read(os.path.join(HERE, "season2", "hill.toml"), "rb"), self.made())

    def test_matches_are_placed_at_random(self):
        self.assertIn(b'placement = "random"', self.made())


if __name__ == "__main__":
    unittest.main()
