---
name: nightly-triage
description: Review ShipKit build results (nightly or any recent job), find why builds failed, fix them and verify with a real rebuild. Use when the user asks about last night's builds, failed builds, "review builds", "why did X fail", or wants a fix verified by building.
---

# Nightly triage

## 1. Summary (one call)

    python3 tools/nightly_report.py            # nightly jobs, last 24 h
    python3 tools/nightly_report.py --last 10  # any jobs

It prints bot busy/idle, one line per job, and the key error lines plus the log path for each failure. Exit 1 = something failed.
Only open `WORK_DIR/logs/<id>.log` or `WORK_DIR/artifacts/<id>/unity-*.log` if the summary lines don't explain the failure.

## 2. Classify

| Symptom | Where the fix goes |
|---|---|
| `CS0246`/`CS0103` only in dev/cheats builds | Game repo. Code under a cheat define (`cheat_defines` in projects.json) is only compiled by nightly/cheat builds, so a package API rename stays hidden until one runs. Compare the game's usage against the package source in `WORK_DIR/work/<game>/Library/PackageCache/<pkg>@*/` |
| `Invalid Pre-Release Train` / `CFBundleShortVersionString … must contain a higher version` | Game repo: bump `bundleVersion` in `ProjectSettings/ProjectSettings.asset`. The version is already approved on the App Store. Numbering is the user's call, so ask |
| `Release bundle is marked debuggable` | Game repo's `Assets/Plugins/Android/AndroidManifest.xml` |
| `BAD CONFIG` / `BAD ARG` | `projects.json` / `config.env` in this repo |
| Anything in the pipeline itself (Python traceback, wrong args) | This repo; see TROUBLESHOOTING.md first |

Game repos live at `repo_path` in projects.json. Builds use their own clone in `WORK_DIR/work/<game>` (reset to origin each run), so fixes must be pushed before a rebuild sees them.

## 3. Fix in the game repo

- `git fetch`, then check the tree is clean and HEAD == origin before touching anything.
- Ask the user once: branch + PR, or push to the build branch. Nightly builds the configured `branch`, so a PR only fixes tonight's build once it's merged.
- C# gotcha: inside `namespace Game` (or any parent namespace), a bare `Foo.Bar()` resolves to namespace `Game.Foo` if one exists, not to the class `Foo`. Fully qualify it.

## 4. Verify with a real build

1. `python3 tools/nightly_report.py --last 1`: must say `bot idle`. The bot and a manual run share `WORK_DIR/work/<game>`.
2. Run in the background (10–30 min), with a `verify-*` id so it never collides with bot job ids:

       python3 pipeline.py build '{"id":"verify-<n>","game":"<game>","platform":"android|ios|both","format":"apk","branch":"<branch>","dev":true,"cheats":true}'

   Use the failing job's platform and flags. Only rebuild the platform that failed (e.g. `ios` when Android already delivered).
3. Pass = the final `@@RESULT` line has `"ok": true`. Note that a verify build uploads to Drive/TestFlight just like a real one.
4. One compile error often hides the next. Expect to iterate and report each round honestly.

Report: which jobs failed, root cause, what changed where (commit/PR), and what was verified by an actual build vs. not verified (e.g. on-device behaviour).
