# unity-ship-kit

**Your own Unity Cloud Build, on a Mac you already have.** Someone types `/build` in Discord. A few minutes later there's an Android APK to install (tap the link or scan the QR code), or the build is on TestFlight for iPhone. No build server to rent and no minutes to pay for.

<p align="center"><img src="docs/demo.jpg" width="420" alt="A finished build in Discord: version, branch, Install and Drive buttons, and a QR code"></p>

- 🛠️ **Build any branch** from Discord: Android APK/AAB or iOS, with dev build and cheat menu toggles.
- 📱 **Install in one tap.** APKs land on Google Drive with an Install link and a QR code. iOS goes to TestFlight.
- 🌙 **Nightly QA builds** of every game, skipped when nothing changed.
- 🔢 **Build numbers that never clash**, checked against Google Play and TestFlight.
- 📤 **Upload to Google Play** (internal track) with one button, optional.
- 🧵 **Clear failures:** the reason in the message, the error lines and full log in a thread.
- 💻 **Doesn't get in the way.** Builds run in the background in their own copy of the game, so the Mac can be someone's work computer.

Made by Luka Pikula at [OOX Limited](https://ooxlimited.com/). MIT licensed.

**Contents:** [Get started](#get-started) · [Using the bot](#using-the-bot) · [FAQ](#faq) · [Security](#security-model) · [Setup guide](SETUP.md) · [For developers](DEVELOPERS.md)

## Get started

**Setup takes about an hour** the first time, mostly clicking through the Discord, Google and Apple websites. Follow **[SETUP.md](SETUP.md)** step by step, or let an AI walk you through it.

### What you need

| | You need |
|---|---|
| ✅ Always | A **Mac** (Apple silicon or Intel) that stays on. It can be the Mac someone works on every day: builds run in the background |
| ✅ Always | **Unity 6** games in **GitHub** repos, and a Unity account (Unity Personal is fine) |
| ✅ Always | A **Discord server** where you're allowed to add a bot |
| 🤖 Android | A **Google account** for the build links: a normal Gmail works, or a Google Workspace Shared Drive |
| 🤖 Android | The game's **keystore** (the `.keystore` file that signs it) and its passwords |
| 🤖 Android, optional | A **Google Play developer account**, only for the 📤 Upload to Play button |
| 🍏 iOS, optional | **Xcode** and an **Apple Developer account** (Account Holder or Admin). iOS builds go to TestFlight, so skip this if you only need Android |

Only iOS games? Skip everything Google. Only Android games? Skip everything Apple. No Play upload? Skip the Play parts.

**macOS only.** Tested on macOS 26 with Xcode 26 and Unity 6 (6000.0.x). Doesn't run on Windows or Linux.

### Set it up with an AI

Using [Claude Code](https://claude.com/claude-code) or another AI coding assistant that can run Terminal commands? Open it in an empty folder and paste this:

```text
Help me set up unity-ship-kit (https://github.com/Kumici13/unity-ship-kit) on this Mac.
Clone it into ~/unity-ship-kit, read README.md and SETUP.md, then follow SETUP.md with me.

- Ask me first which platforms I build (Android, iOS or both), whether I want Play upload,
  and whether builds should go to a normal Google Drive or a Workspace Shared Drive.
  Skip the steps I don't need.
- Run the Terminal steps yourself. For website steps (Discord, Google, Apple), tell me exactly
  what to click, one step at a time, and wait until I paste back the value you need.
- Write the values into config.env and projects.json yourself. Never print, commit or upload
  secrets (tokens, passwords, keys), and never change files inside my game repos.
- For projects.json, read each game's ProjectSettings/ProjectSettings.asset to find its package
  name and bundle ID, and ask me about anything you can't find.
- Run ./setup.sh after each step and fix what it reports. When it says
  "All required checks passed.", run ./setup.sh --install and tell me to type /build in Discord.
```

### Or by hand

```bash
git clone https://github.com/Kumici13/unity-ship-kit.git && cd unity-ship-kit
./setup.sh              # installs tools, creates config.env + projects.json, lists what's missing
```

Then follow [SETUP.md](SETUP.md) from Step 3.

---

## Using the bot

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

## FAQ

**Does it work on Windows or Linux?** No. It needs macOS: iOS builds need Xcode, and the bot runs as a macOS background service. Android-only studios still need a Mac for now.

**Will it slow down my Mac while I work?** Builds run at low priority in a separate copy of the game, so your open Unity project and files are never touched. A build still uses a lot of CPU for a few minutes; you'll notice it in heavy tasks, not in normal work.

**How much disk space?** About 100 GB with several games, mostly Unity's import cache. Each game's first build creates it; later builds reuse it. `/status` shows free space.

**Where do builds go, and for how long?** Android: Google Drive, deleted after 30 days. iOS: TestFlight (Apple keeps them 90 days). The last builds are also in `~/ShipKit/artifacts` on the Mac for 30 days.

**Who can download a build?** By default anyone with the link, so testers can use personal Gmail accounts. For unreleased games you can limit links to your company domain: `DRIVE_SHARING=domain`.

**Does it change my game repos?** Never. It builds from its own clone of what's pushed to GitHub. Version numbers, cheat defines and signing are applied only inside the build.

**Does the Mac need to stay on?** Yes, and logged in. [Power loss, reboots, sleep](DEVELOPERS.md#power-loss-reboots-sleep) lists the settings that make it come back by itself after a power cut.

**Something failed. Now what?** Read the ❌ reason and the thread under the message, then [Error categories](DEVELOPERS.md#error-categories) and [TROUBLESHOOTING.md](TROUBLESHOOTING.md). With [Claude Code](https://claude.com/claude-code), the included `nightly-triage` skill reviews failed builds for you.

---

## Security model

**Read this before installing.** A build runs the game project's Editor code (build scripts, `[InitializeOnLoad]`, packages) from whatever branch is requested, on a Mac that holds your signing keys, a Play service account and an App Store Connect key. **Anyone who can push a branch to a configured repo, or trigger a build, can run code with access to all of those.** Set it up accordingly:

- **Dedicated macOS user** for the bot, with nothing else of value in it.
- **Only trusted people** push to the game repos and sit in the build channel. Set `ALLOWED_ROLE_ID` so only members with that role can use commands and buttons, and `UPLOAD_ROLE_ID` for 📤 Upload to Play.
- **Least-privilege service account:** Release Manager only on the apps the bot ships, Content manager only on the bot's Shared Drive. With `DRIVE_MODE=personal` the bot's Google sign-in (`drive-token.json`, owner-only) can only see files the bot created.
- **A dedicated Shared Drive** (or Google account) for builds. Cleanup only touches the per-game folders the bot creates, but don't share it with anything else anyway.
- `config.env`, keystores, `*.p8`, service-account JSONs, `projects.json` and logs are gitignored — never commit them. `setup.sh` makes `config.env` owner-only (`chmod 600`); the bot warns if it isn't.
- Keystore passwords reach Unity through environment variables and bundletool through a private temp file, never command-line args (visible in `ps`). Credentials embedded in git URLs are redacted from logs, which are posted to Discord on failure.
- The Discord token grants full bot control; treat it like a password.
- **Drive links:** `DRIVE_SHARING=anyone` (default) lets anyone with the link download — convenient for testers' personal accounts, but links can be forwarded. Use `domain` (with `DRIVE_DOMAIN`) or `none` for unreleased games. Builds are removed after 30 days.
- **iOS export compliance** is your legal declaration: the bot only marks builds as exempt when you set `IOS_EXEMPT_ENCRYPTION=true`.
