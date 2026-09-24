# unity-ship-kit

A self-hosted Unity Cloud Build for a Mac. QA types `/build` in Discord and gets an installable APK link (and a QR code) a few minutes later, or a TestFlight build on iOS. Builds run in the background in separate clones, so whoever works on that Mac is never disturbed.

- **Builds land in:** a Google Shared Drive (anyone with the link can download, auto-deleted after 30 days)
- **Runs on:** any Mac with Unity + Xcode, as a launchd agent (`com.shipkit.bot`)
- **Nightly QA builds**, Play internal-track upload, TestFlight upload, build queue, abort, cleanup

Made by Luka Pikula at [Oox](https://ooxlimited.com/). MIT licensed.

## Quick start

You need a Mac, a Discord server, a Google Play service account and (for iOS) an App Store Connect API key.

**macOS only.** Needs Python 3.10+. Tested on macOS 26 with Xcode 26 and Unity 6 (6000.0.x); older Unity versions may work but aren't tested.

```bash
git clone https://github.com/Kumici13/unity-ship-kit.git && cd unity-ship-kit
./setup.sh              # installs Python deps + bundletool, creates config.env and projects.json, lists what's missing
# fill in config.env (see Setup below) and add your games to projects.json
./setup.sh              # re-run until every check is ✅
./setup.sh --install    # starts the bot now and on every login
```

Then type `/build game:<your game>` in your build channel. The first build of a game takes 10–30 min (clone + asset import), later ones a few minutes.

---

## For QA: using the bot

All commands work only in the build channel. Anyone who can see the channel can use every command.

### `/build`: build a game

```
/build game:my-game
/build game:my-game cheats:True
/build game:my-game branch:feature/new-shop dev:True note:"shop fix"
/build game:other-game platform:both format:aab
```

| Option | Default | What it does |
|---|---|---|
| `game` | (required) | Pick from the list |
| `platform` | `android` | `android`, `ios` or `both` |
| `format` | `apk` | `apk` installs directly on a phone; `aab` is only for the Play Store |
| `branch` | game's main branch | Autocompletes from GitHub, most recently changed first |
| `dev` | off | Development build (profiler, debug logs) |
| `cheats` | off | Turns on the game's cheat/debug menu |
| `note` | none | Short label shown in the message and the file name |
| `clean` | off | Wipes the import cache first. Slow; only use it when a build is broken for no reason |
| `force` | off | Rebuild even if this exact commit was already built |

**What you'll see:** one message that updates itself.

```
🔨 my-game android apk · master · cheats — #12 by @you
⚙️ Unity Android: IL2CPP (slowest step) (3 min) · ~2 min left
```

When it finishes:

```
✅ 0.31 (28) · master@a897756f · 4 min
🤖 v0.31 · my-game-0.31-28-master-cheats.apk (85 MB)
📝 3 new commit(s) since last build:
• Fix tutorial save (Alice)
• Cargo drop animation (Bob)
[📱 Install] [💾 Drive] [🔁 Rebuild]   + QR code
```

- **📱 Install**: tap it on an Android phone; the APK downloads, then tap it to install. The first time, Android asks you to allow installs from your browser.
- **QR code**: scan it with your phone camera when you're on a PC. Same thing as Install.
- **💾 Drive**: normal Google Drive page, for downloading on a PC.
- **🔁 Rebuild**: same settings, latest commit.
- **📤 Upload to Play (internal)**: only on `format:aab` release builds (no dev, no cheats), because Play doesn't accept APKs. Sends that exact bundle to the Play Console internal track as a draft.

**If nothing changed since the last build**, the bot doesn't build again. It reposts the existing build (♻️) in about 2 seconds. Use `force:True` if you really need a fresh one.

**If it fails**, the message shows ❌ and the reason, and a thread opens under it with the error and the full log attached.

### Other commands

| Command | What it does |
|---|---|
| `/latest game:<game>` | Newest finished build (buttons + QR), visible only to you |
| `/status` | What's building, what's queued, free disk space |
| `/builds` | Last 10 builds with links |
| `/abort` | Stop the build that's running |
| `/cancel` | Remove a queued build |

### Good to know

- **One build at a time.** Others wait in a queue and show their position. The queue survives bot restarts.
- **Build numbers are unique per game** and always go up, even for builds that never reach a store. Two APKs never share a number, so "build 215" always means one specific build.
- **The first build of a game is slow** (10–30 min: it clones the repo and imports all assets). After that, builds take a few minutes.
- **Only pushed commits are built.** The bot builds what's on GitHub, never someone's local changes.
- **Release builds are made Play-safe.** If a game's custom `AndroidManifest.xml` hard-codes `android:debuggable="true"`, the bot removes it in its build copy for release builds, and verification rejects anything still debuggable. Dev builds stay debuggable.
- **iOS** goes straight to TestFlight (internal testers). The message shows processing, then ready. There's no Drive link for iOS.
- **Nightly:** every night at 03:00, each game's main branch gets a **QA build**: dev + cheats APK, plus a TestFlight build for games with iOS set up. Games with no new commits since the last nightly are skipped. Messages say "by 🌙 nightly".

---

## For devs: how it works

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
| `bot.py` | Discord bot: commands, queue, messages, buttons, nightly + cleanup timers |
| `pipeline.py` | One job: clone → number → Unity → verify → Drive / TestFlight. Also `upload` (Play) and `prune` (Drive cleanup) |
| `ship_android.py` | Play API, AAB verification, APK from AAB. Also usable directly from the CLI (below) |
| `ship.py` | Shared helpers (config, logging, git, ASC API) + legacy single-project iOS CLI |
| `setup.sh` | First-time setup + health check (`--install` starts the bot) |
| `requirements.txt` | Python packages |
| `projects.json` | The games (see below; start from `projects.example.json`) |
| `tools/nightly_report.py` | One-screen summary of recent builds and why they failed |
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

### Setup (one-time)

`./setup.sh` does steps 1–2 and 7 and checks the rest. The details:

1. **Unity CLI:** `curl -fsSL https://public-cdn.cloud.unity3d.com/hub/prod/cli/install.sh | UNITY_CLI_CHANNEL=beta bash`
2. **bundletool:** `mkdir -p tools && curl -sL -o tools/bundletool.jar https://github.com/google/bundletool/releases/download/1.18.1/bundletool-all-1.18.1.jar`
3. **config.env:** copy `config.env.example` and fill it in:
   - `PLAY_SERVICE_ACCOUNT`: Play Console service account JSON (Release Manager on every app). The same account uploads to Drive.
   - `KEYSTORE_<NAME>_PATH/PASS`
   - `DISCORD_TOKEN`, `DISCORD_GUILD_ID`, `BUILD_CHANNEL_ID`, `OWNER_DISCORD_ID`
   - `DRIVE_SHARED_DRIVE_ID`: the ID (from its URL) of a Shared Drive used only for builds
   - Optional: `ALLOWED_ROLE_ID` / `UPLOAD_ROLE_ID`, `DRIVE_SHARING`, `IOS_EXEMPT_ENCRYPTION` (see Security model)
   - `ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_KEY_PATH`, `TEAM_ID` (iOS only)
4. **Google Drive:** in the service account's GCP project, enable the Drive API. Create a Shared Drive and add the service account email as **Content manager**. Workspace must allow sharing Shared Drive files outside the org.
5. **Discord:** invite the bot with
   `https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot+applications.commands&permissions=309237763072`
   (View Channels, Send Messages, Send in Threads, Create Public Threads, Embed Links, Attach Files, Read History).
6. **iOS:**
   - App Store Connect API key (App Manager role) at `~/.appstoreconnect/private_keys/AuthKey_<ID>.p8`. It's used for TestFlight numbers and status.
   - Xcode → Settings → Accounts signed in with an **Account Holder/Admin** Apple ID. Signing and upload go through that account, because an App Manager key is refused cloud signing.
   - An **Apple Distribution** certificate for the team in the login keychain (Xcode → Manage Certificates → + Apple Distribution).
7. **Start the bot:**
   ```bash
   sed -e "s|__KIT_DIR__|$PWD|g" -e "s|__HOME__|$HOME|g" -e "s|__PYTHON__|$(command -v python3)|g" \
       launchd/com.shipkit.bot.plist > ~/Library/LaunchAgents/com.shipkit.bot.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.shipkit.bot.plist
   ```

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
python3 pipeline.py prune      # trash Drive builds older than 30 days
python3 pipeline.py prune '{"dry_run":true}'   # only list what would be trashed
```

The older direct CLIs still work, but they build **in your working repo** (they refuse a dirty tree and check out the branch there):

```bash
python3 ship_android.py --list
python3 ship_android.py my-game --no-upload --apk   # build + verify + APK
python3 ship_android.py my-game                       # build + upload to Play internal
```

---

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
| `DRIVE FAILED` | Drive API off, service account not on the Shared Drive, or external sharing blocked |
| `PLAY FAILED` / `UPLOAD FAILED` | Service account lacks access, or Play already has a higher version code (rebuild) |
| `XCODE FAILED` / `ASC FAILED` | Signing, provisioning or App Store Connect key problem |
| `CRASHED` / `SCRIPT ERROR` / `BOT ERROR` | Bug in the pipeline or bot. Check the log |

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for known Unity/Xcode/Play/Drive gotchas.

## Security model

**Read this before installing.** A build runs the game project's Editor code (build scripts, `[InitializeOnLoad]`, packages) from whatever branch is requested, on a Mac that holds your signing keys, a Play service account and an App Store Connect key. **Anyone who can push a branch to a configured repo, or trigger a build, can run code with access to all of those.** Set it up accordingly:

- **Dedicated macOS user** for the bot, with nothing else of value in it.
- **Only trusted people** push to the game repos and sit in the build channel. Set `ALLOWED_ROLE_ID` so only members with that role can use commands and buttons, and `UPLOAD_ROLE_ID` for 📤 Upload to Play.
- **Least-privilege service account:** Release Manager only on the apps the bot ships, Content manager only on the bot's Shared Drive.
- **A dedicated Shared Drive** for builds. Cleanup only touches the per-game folders the bot creates, but don't share the drive with anything else anyway.
- `config.env`, keystores, `*.p8`, service-account JSONs, `projects.json` and logs are gitignored — never commit them. `setup.sh` makes `config.env` owner-only (`chmod 600`); the bot warns if it isn't.
- Keystore passwords reach Unity through environment variables and bundletool through a private temp file, never command-line args (visible in `ps`). Credentials embedded in git URLs are redacted from logs, which are posted to Discord on failure.
- The Discord token grants full bot control; treat it like a password.
- **Drive links:** `DRIVE_SHARING=anyone` (default) lets anyone with the link download — convenient for testers' personal accounts, but links can be forwarded. Use `domain` (with `DRIVE_DOMAIN`) or `none` for unreleased games. Builds are trashed after 30 days.
- **iOS export compliance** is your legal declaration: the bot only marks builds as exempt when you set `IOS_EXEMPT_ENCRYPTION=true`.
