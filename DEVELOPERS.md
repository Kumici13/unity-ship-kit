# unity-ship-kit for developers

How it works, configuration reference and operating the bot. Setup: [SETUP.md](SETUP.md).

## How it works

```
Discord /build ─► bot.py ─ queue (~/ShipKit/builds.json)
                     │
                     └─► pipeline.py build   (caffeinate + nice, own process group)
                           1. clone/fetch ~/ShipKit/work/<game>, reset to origin/<branch>
                              (only Library/ survives between builds)
                           2. build number = max(Play/TestFlight, last issued, repo) + 1
                           3. unity build (Unity CLI) → ShipKit.Android / ShipKit.IOS
                           4. Android: verify AAB → universal APK → Drive (anyone with link)
                              iOS:     xcodebuild archive → TestFlight → wait for processing
                           5. prints @@RESULT {...} → bot edits the Discord message
```

- **Game repos in `~/Documents/GitHub` are never modified.** They're only read for their GitHub URL and for `extra_files`. If your editor has the game open, the build still runs (separate clone); the owner just gets an FYI ping.
- **Unity version** comes from each project's `ProjectVersion.txt`. The Unity CLI installs a missing editor, and `pipeline.py` adds missing Android/iOS modules.
- **Version changes are never committed back.** Version, build number, cheat defines and signing are set only inside the batch Unity process.

### Files

| File | Purpose |
|---|---|
| `SETUP.md` / `DEVELOPERS.md` | Step-by-step setup / this reference |
| `bot.py` | Discord bot: commands, queue, messages, buttons, nightly + cleanup timers |
| `pipeline.py` | One job: clone → number → Unity → verify → Drive / TestFlight. Also `upload` (Play) and `prune` (Drive cleanup) |
| `ship_android.py` | Play API, AAB verification, APK from AAB. Also usable directly from the CLI (below) |
| `ship.py` | Shared helpers (config, logging, git, ASC API) + legacy single-project iOS CLI |
| `setup.sh` | First-time setup + health check (`--install` starts the bot) |
| `requirements.txt` | Python packages |
| `projects.json` | The games (see below; start from `projects.example.json`) |
| `tools/nightly_report.py` | One-screen summary of recent builds and why they failed |
| `tools/drive_login.py` | One-time Google sign-in for `DRIVE_MODE=personal` (builds on a normal Google Drive) |
| `.claude/skills/nightly-triage` | [Claude Code](https://claude.com/claude-code) skill: review failed builds, fix, verify with a rebuild |
| `unity-side/ShipKit.cs` | Build entry points, copied into each clone per build. Never commit it into a game repo |
| `unity-side/BuildScript.cs` | Legacy entry point for `ship.py` |
| `launchd/com.shipkit.bot.plist` | Keeps the bot running (login + crash restart) |
| `config.env` | Secrets and IDs. **Gitignored, never commit.** Template: `config.env.example` |

State on the Mac (not in git): `~/ShipKit/` holds `work/` (clones), `artifacts/<build id>/` (AAB/APK, kept 30 days), `logs/<build id>.log`, `builds.json` (queue + history) and `counters.json` (build numbers).

### Adding a game

Add an entry to `projects.json` (first time: `cp projects.example.json projects.json`). The bot reads it live, so no restart is needed.

```json
"my-game": {
  "repo_path": "~/Documents/GitHub/my-game",
  "branch": "main",
  "package_name": "com.example.mygame",
  "keystore": "main",
  "key_alias": "my key",
  "cheat_defines": ["CHEATS"],
  "platforms": ["android"]
}
```

| Key | Required | Notes |
|---|---|---|
| `repo_path` | ✓ | Local clone; the build clone is made from its `origin` URL |
| `branch` | ✓ | Default branch for `/build` and nightly |
| `package_name` | ✓ | Android application ID |
| `keystore` / `key_alias` | ✓ | `keystore: "main"` → `KEYSTORE_MAIN_PATH/PASS` in config.env. List aliases: `keytool -list -keystore <path>` |
| `cheat_defines` | for `cheats:True` | Scripting defines the game's cheat menu is behind (`#if CHEATS`) |
| `platforms` | | Default `["android","ios"]` |
| `bundle_id` | for iOS | iOS bundle ID (must exist in App Store Connect) |
| `team_id`, `asc_prefix` | | App under another Apple team: `"asc_prefix": "CLIENT"` → `CLIENT_KEY_ID/ISSUER_ID/KEY_PATH` |
| `self_bumping` | | Project's own build preprocessor bumps version +0.01 and code +1; the bot compensates. Needs an `X.YY` version |
| `target_sdk` | | Android targetSdk (default 36; Play requires 35+) |
| `extra_files` | | Gitignored files the build needs, copied from `repo_path` (e.g. `Assets/google-services.json`) |
| `prebuild_method` | | Static C# method run before the player build (e.g. an Addressables build) |
| `nightly` | | `false` to skip nightly builds (default: dev + cheats APK, and iOS when `bundle_id` is set) |
| `ios_min_version` | | Minimum iOS for this game (default `IOS_MIN_VERSION`, 15.0) |
| `uses_encryption` | | `true` if the game ships its own crypto: never declared exempt, even with `IOS_EXEMPT_ENCRYPTION=true` |

**Before the first Play upload of a new app,** decide its upload key. Whatever key signs the first upload becomes its upload key (Play Console → App signing → Upload key certificate). With Play App Signing that's recoverable through Play support, but pick on purpose.

### Operating it

```bash
launchctl kickstart -k gui/$(id -u)/com.shipkit.bot   # restart (after editing bot.py)
launchctl bootout gui/$(id -u)/com.shipkit.bot        # stop
tail -f ~/Library/Logs/shipbot.log                    # bot log
tail -f ~/ShipKit/logs/<build id>.log                 # one build's log
```

### Power loss, reboots, sleep

| Setting | Why |
|---|---|
| `pmset autorestart 1` (on) | Mac boots by itself after a power cut |
| **Automatic login** (System Settings → Users & Groups → Automatically log in as the bot's user) | The bot runs in the user session, so without a login it stays offline after any reboot. Requires FileVault off |
| Bot runs under `caffeinate -i` (in the plist) | With a short `pmset sleep`, without this the Mac idle-sleeps between builds, the bot drops off Discord and nightly builds are missed. The display can still sleep |
| Xcode → Settings → Accounts stays signed in | iOS signing and upload use it |

A power cut mid-build marks that build ⚠️ interrupted (🔁 Rebuild button). Queued builds resume, and the next build resets the clone. If Unity then fails strangely, rebuild with `clean:True`.

Changes to `pipeline.py`, `ship*.py` and `projects.json` apply from the next build without a restart. Changes to `bot.py` need the restart above. Restarting mid-build marks that build ⚠️ interrupted; queued builds carry on.

### Running a build without Discord

```bash
python3 pipeline.py build '{"id":"manual1","game":"my-game","platform":"android","format":"apk","branch":"main","dev":false,"cheats":true}'
python3 pipeline.py prune      # remove Drive builds older than 30 days
python3 pipeline.py prune '{"dry_run":true}'   # only list what would be removed
```

The older direct CLIs still work, but they build **in your working repo** (they refuse a dirty tree and check out the branch there):

```bash
python3 ship_android.py --list
python3 ship_android.py my-game --no-upload --apk   # build + verify + APK
python3 ship_android.py my-game                       # build + upload to Play internal
```

## Error categories

Shown on a failed build message (❌ **CATEGORY**) with details in the thread.

| Category | Usual cause |
|---|---|
| `BAD BRANCH` | Branch not on GitHub (similar names suggested). Push it first |
| `BAD ARG` / `BAD CONFIG` | Unknown game, missing `projects.json` field, missing config.env key, keystore not found |
| `GIT FAILED` | Clone/fetch/LFS failed (network, auth) |
| `UNITY FAILED` | Compile errors or build exception. The thread shows the error lines; full Unity log attached |
| `VERIFY FAILED` | Built AAB has the wrong package, target SDK < 35, wrong version code, bad signature, or a missing application class |
| `APK FAILED` | bundletool couldn't make the APK |
| `DRIVE FAILED` | Drive API off, service account not on the Shared Drive, external sharing blocked, or (personal Drive) the sign-in expired: run `tools/drive_login.py` again |
| `PLAY FAILED` / `UPLOAD FAILED` | Service account lacks access, or Play already has a higher version code (rebuild) |
| `XCODE FAILED` / `ASC FAILED` | Signing, provisioning or App Store Connect key problem |
| `CRASHED` / `SCRIPT ERROR` / `BOT ERROR` | Bug in the pipeline or bot. Check the log |

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for known Unity/Xcode/Play/Drive gotchas.
