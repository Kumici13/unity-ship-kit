"""python3 -m unittest discover tests"""
import json
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


class Promote(unittest.TestCase):
    """play_promote copies internal's completed release to production, or refuses."""

    def run_promote(self, internal, production, expect_code=None):
        puts = []

        def http(method, url, token, body=None, **kw):
            if method == "PUT":
                puts.append((url, json.loads(body)))
                return {}
            return {"releases": internal if url.endswith("/internal") else production}

        with mock.patch.multiple(sa, play_token=mock.DEFAULT, play_edit_insert=mock.DEFAULT,
                                 play_commit=mock.DEFAULT, play_edit_delete=mock.DEFAULT,
                                 section=mock.DEFAULT, log=mock.DEFAULT, _http=http,
                                 die=mock.Mock(side_effect=SystemExit(1))):
            sa.play_promote({"key": "g", "package_name": "p"}, {"PLAY_SERVICE_ACCOUNT": "x"},
                            expect_code=expect_code)
        return puts

    REL = {"name": "1.2 (42)", "versionCodes": ["42"], "status": "completed",
           "releaseNotes": [{"language": "en-US", "text": "hi"}]}

    def test_copies_internal_release_to_production(self):
        puts = self.run_promote([self.REL], [], expect_code=42)
        self.assertEqual(len(puts), 1)
        url, body = puts[0]
        self.assertTrue(url.endswith("/tracks/production"))
        self.assertEqual(body["releases"], [{**self.REL, "status": "completed"}])

    def test_refuses(self):
        for internal, production, code in (([], [], None),                 # nothing on internal
                                           ([self.REL], [self.REL], None),  # already live
                                           ([self.REL], [], 41)):           # internal moved on
            with self.subTest(code=code), self.assertRaises(SystemExit):
                self.run_promote(internal, production, code)


if __name__ == "__main__":
    unittest.main()


class ShippedVersion(unittest.TestCase):
    """Build 58 of a game went to Play as 0.63 after 0.64: the name came from a reset tree."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = Path(tempfile.mkdtemp()) / "shipped.json"
        patch = mock.patch.object(sa, "SHIPPED_VERSIONS", self.tmp)
        patch.start()
        self.addCleanup(patch.stop)

    def test_remembers_a_release_play_no_longer_lists(self):
        sa.record_shipped("g", "0.64")
        self.assertEqual(sa.shipped_version("g", "0.63"), "0.64")

    def test_play_wins_when_newer_and_record_never_goes_back(self):
        sa.record_shipped("g", "0.64")
        sa.record_shipped("g", "0.63")
        self.assertEqual(sa.shipped_version("g", "0.70"), "0.70")
        self.assertEqual(sa.shipped_version("g", None), "0.64")
        self.assertIsNone(sa.shipped_version("other", None))


class GameEntry(unittest.TestCase):
    SETTINGS = ("  productName: X\n  applicationIdentifier:\n    Android: com.example.game\n"
                "    Standalone: com.Unity-Technologies.blank\n    iPhone: {ios}\n  buildNumber:\n")

    def test_android_only_when_ios_is_template(self):
        e = ship.game_entry(self.SETTINGS.format(ios="com.Unity-Technologies.blank"), "~/r", "main", "oox", "k")
        self.assertEqual((e["package_name"], e["platforms"], e["nightly"]), ("com.example.game", ["android"], False))
        self.assertNotIn("bundle_id", e)

    def test_ios_bundle_id(self):
        e = ship.game_entry(self.SETTINGS.format(ios="com.example.gameios"), "~/r", "main", "oox", "k")
        self.assertEqual(e["bundle_id"], "com.example.gameios")
        self.assertNotIn("platforms", e)

    def test_no_android_id_refused(self):
        with self.assertRaises(ValueError):
            ship.game_entry("  productName: X\n", "~/r", "main", "oox", "k")


class CommitVersionBack(unittest.TestCase):
    def test_pushes_to_origin_and_leaves_the_checkout_alone(self):
        import os, subprocess, tempfile
        from pathlib import Path
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env):
            git = lambda *a, cwd=tmp: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout
            git("init", "-q", "--bare", "remote.git")
            repo = Path(tmp) / "repo"
            settings = repo / "ProjectSettings" / "ProjectSettings.asset"
            settings.parent.mkdir(parents=True)
            settings.write_text("  bundleVersion: 0.63\n  AndroidBundleVersionCode: 54\n")
            git("init", "-q", "-b", "main", cwd=repo)
            git("add", "-A", cwd=repo)
            git("commit", "-qm", "settings", cwd=repo)
            git("remote", "add", "origin", str(Path(tmp) / "remote.git"), cwd=repo)
            git("push", "-q", "origin", "main", cwd=repo)
            # The user is elsewhere: another branch, unsaved edits in the same file.
            git("checkout", "-q", "-b", "feature", cwd=repo)
            settings.write_text("  bundleVersion: 0.63\n  AndroidBundleVersionCode: 54\n  wip: 1\n")
            app = {"repo_path": str(repo), "branch": "main", "key": "t"}
            sa.commit_version_back(app, "0.70", 69)
            on_origin = git("show", "main:ProjectSettings/ProjectSettings.asset", cwd=Path(tmp) / "remote.git")
            self.assertIn("bundleVersion: 0.70", on_origin)
            self.assertIn("AndroidBundleVersionCode: 69", on_origin)
            self.assertIn("wip: 1", settings.read_text())
            self.assertEqual(git("branch", "--show-current", cwd=repo).strip(), "feature")
            sa.commit_version_back(app, "0.69", 70)  # older name: never moves backwards
            self.assertIn("bundleVersion: 0.70", git("show", "main:ProjectSettings/ProjectSettings.asset", cwd=Path(tmp) / "remote.git"))
            self.assertEqual(git("worktree", "list", cwd=repo).count("\n"), 1)  # temp worktree removed


class IosSettingsWrite(unittest.TestCase):
    def test_version_and_iphone_build(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "ProjectSettings" / "ProjectSettings.asset"
            f.parent.mkdir()
            f.write_text("  bundleVersion: 0.63\n  buildNumber:\n    Standalone: 0\n    iPhone: 12\n    tvOS: 0\n")
            ship.write_project_settings(tmp, "0.71", 13)
            self.assertEqual(ship.read_project_settings(tmp), ("0.71", 13))
