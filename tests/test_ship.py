"""python3 -m unittest discover tests"""
import unittest
from unittest import mock

import ship
import ship_android as sa


class Redact(unittest.TestCase):
    def test_strips_url_credentials(self):
        self.assertEqual(ship.redact("git clone https://u:ghp_x@github.com/o/r.git"),
                         "git clone https://***@github.com/o/r.git")

    def test_leaves_plain_text(self):
        s = "https://github.com/o/r.git mail a@b.com"
        self.assertEqual(ship.redact(s), s)


class BranchName(unittest.TestCase):
    def test_rejects_option_looking_names(self):
        # Must be refused before any git fetch/ls-remote sees the name.
        with mock.patch.object(ship, "run", side_effect=AssertionError("reached git")):
            for bad in ("-upload-pack=touch x", "a..b", "a b"):
                with self.assertRaises(SystemExit):
                    ship.validate_branch(".", bad)


class SelfBump(unittest.TestCase):
    def test_steps_back_one_hundredth(self):
        self.assertEqual(sa.self_bump_back({"key": "g"}, "1.10"), "1.09")

    def test_rejects_semver(self):
        with self.assertRaises(SystemExit):
            sa.self_bump_back({"key": "g"}, "1.2.3")


if __name__ == "__main__":
    unittest.main()
