# Setup, step by step

Plan on about an hour the first time. Most of it is clicking through the Discord, Google and Apple websites and copying values into **one file, `config.env`**. You don't need to read any code.

Check [what you need](README.md#what-you-need) first. Prefer to be walked through it? Paste the [AI setup prompt](README.md#set-it-up-with-an-ai) into your AI assistant.

**Stuck?** Run `./setup.sh`. It checks everything and tells you what's still missing. Then look in [TROUBLESHOOTING.md](TROUBLESHOOTING.md), or [open an issue](https://github.com/Kumici13/unity-ship-kit/issues).

## Step 1: Install the tools

Open **Terminal** (press ⌘ Space, type `Terminal`, press Enter) and paste these one at a time.

1. **Homebrew** (installs the other tools). Skip if `brew -v` already works. Follow the "Next steps" it prints at the end.
   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```
2. **Python and Git LFS:**
   ```bash
   brew install python git-lfs && git lfs install
   ```
3. **Unity Hub**: download it from https://unity.com/download, open it and **sign in**. Under **Settings → Licenses**, make sure you have an active license (Unity Personal is fine). The bot uses this license. You don't need to install Unity editors: the bot installs the version each game needs.
4. **Unity command line** (the bot uses it to install editors):
   ```bash
   curl -fsSL https://public-cdn.cloud.unity3d.com/hub/prod/cli/install.sh | UNITY_CLI_CHANNEL=beta bash
   ```
5. **iOS games only:** install **Xcode** from the Mac App Store and open it once to accept the license.
6. **Your games on this Mac.** The bot needs a copy of each game repo to know where it lives on GitHub. If your games aren't on this Mac yet:
   ```bash
   mkdir -p ~/Documents/GitHub && cd ~/Documents/GitHub
   git clone https://github.com/<you>/<your-game>.git
   ```
   If that asks for a password and fails, set up GitHub sign-in first. The easiest way is `brew install gh && gh auth login`, answering the questions with the defaults.

## Step 2: Download ShipKit

```bash
cd ~ && git clone https://github.com/Kumici13/unity-ship-kit.git && cd unity-ship-kit
./setup.sh
```

The first run creates two files for you to fill in:
- **`config.env`**: passwords and IDs. Open it with `open -e config.env`. Each line is `NAME=value`: paste the value right after `=`, with no spaces and no quotes. Lines starting with `#` are notes.
- **`projects.json`**: your games (Step 7).

Many ❌ are normal at this point. The next steps fix them one by one.

## Step 3: Discord bot

1. Go to https://discord.com/developers/applications → **New Application** → name it (e.g. ShipKit).
2. Left menu **Bot** → **Reset Token** → **Copy**. In `config.env`: `DISCORD_TOKEN=<paste>`. Keep it secret: it's the bot's password.
3. Left menu **General Information** → copy the **Application ID**. Put it in this link instead of `<APPLICATION_ID>`, open the link and pick your server:
   `https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot+applications.commands&permissions=309237763072`
4. In the Discord app: **User Settings → Advanced → Developer Mode** on. Now right-clicking things shows **Copy … ID**:
   - Right-click your **server icon** → **Copy Server ID** → `DISCORD_GUILD_ID=`
   - Make a channel for builds (e.g. `#builds`), right-click it → **Copy Channel ID** → `BUILD_CHANNEL_ID=`
   - Optional: right-click **yourself** → **Copy User ID** → `OWNER_DISCORD_ID=` (you get a ping when a build starts while that game is open on your Mac).
   - Optional: right-click a **role** (Server Settings → Roles) → **Copy Role ID** → `ALLOWED_ROLE_ID=` (only people with that role can build) and `UPLOAD_ROLE_ID=` (only they can upload to Play).

## Step 4: Google (Android games only)

**4.1 A Google Cloud project** (free; needs no billing):
1. Go to https://console.cloud.google.com → top bar project picker → **New project** → any name → **Create**. Make sure it's selected in the top bar.
2. **☰ → APIs & Services → Library** → search **Google Drive API** → **Enable**. Using Play upload? Also enable **Google Play Android Developer API**.

**4.2 A robot account for Google Play** (a "service account"). **Optional** with a normal Drive (4.3a): skip it if you don't need the 📤 Upload to Play button. Needed for a Shared Drive (4.3b).
1. **☰ → IAM & Admin → Service Accounts → Create service account** → any name → **Done** (skip the optional roles).
2. Click it → **Keys → Add key → Create new key → JSON**. A file downloads. Move it somewhere safe:
   ```bash
   mkdir -p ~/.config/shipkit && mv ~/Downloads/<the-file>.json ~/.config/shipkit/service-account.json && chmod 600 ~/.config/shipkit/service-account.json
   ```
   In `config.env`: `PLAY_SERVICE_ACCOUNT=~/.config/shipkit/service-account.json`
3. Copy the service account's email (looks like `name@project.iam.gserviceaccount.com`).
4. Using Play upload: https://play.google.com/console → **Users and permissions → Invite new users** → paste that email → under **App permissions** add your games → tick **Release to testing tracks** → **Invite user**.

**4.3 Where the builds go.** Pick **a** or **b**.

**a) Normal Google Drive** (any Gmail, 15 GB free). Builds go into a "ShipKit builds" folder. The bot can only see files it made itself, nothing else in your Drive.
1. In Cloud Console: **☰ → Google Auth Platform** → **Get started**. App name: ShipKit. Support email: yours. Audience: **External**. Contact email: yours. Agree → **Create**.
2. **Audience** → **Publish app** → **Confirm** (status **In production**). Skip this and Google logs the bot out every 7 days.
3. **Clients → Create client** → Application type **Desktop app** → **Create** → **Download JSON**.
4. In Terminal, in the `unity-ship-kit` folder:
   ```bash
   python3 tools/drive_login.py ~/Downloads/client_secret_<…>.json
   ```
   A browser opens. Sign in with the Google account whose Drive should hold the builds. Google warns **"Google hasn't verified this app"**: that's normal, because it's your own app. Click **Advanced → Go to ShipKit (unsafe)** → **Continue**. When the terminal says `Saved`, you're done.
5. In `config.env`: `DRIVE_MODE=personal`

Builds older than 30 days are deleted automatically so the Drive doesn't fill up. If Google ever logs the bot out (for example after a password change), run step 4 again.

**b) Google Workspace Shared Drive** (if your studio uses Workspace):
1. https://drive.google.com → **Shared drives → New** → name it e.g. "Builds".
2. **Manage members** → add the service account email from 4.2 → **Content manager**.
3. Open the drive. The link looks like `drive.google.com/drive/folders/<ID>`. Copy the `<ID>` part → `DRIVE_SHARED_DRIVE_ID=`
4. Testers use personal Gmail accounts? A Workspace admin must allow sharing outside the organization for shared drives (admin.google.com → Apps → Google Workspace → Drive and Docs → Sharing settings). Or keep links inside your company: `DRIVE_SHARING=domain` and `DRIVE_DOMAIN=yourstudio.com`.

## Step 5: Android signing key (Android games only)

Use the keystore your game already ships with. The key that signs a game's first Play upload stays its upload key.

In `config.env`:
```
KEYSTORE_MAIN_PATH=/Users/<you>/keys/my-game.keystore
KEYSTORE_MAIN_PASS=<keystore password>
```
Add `KEYSTORE_MAIN_ALIASPASS=` only if the key password is different. To see the key name (alias) inside it: `keytool -list -keystore <path>`. Games signed with different keystores get one set each: `KEYSTORE_OTHER_PATH` / `KEYSTORE_OTHER_PASS`, then `"keystore": "other"` in projects.json.

No keystore yet (brand-new game)? Make one and **back it up with its passwords**, because a lost keystore means a painful key reset with Google:
```bash
mkdir -p ~/keys && keytool -genkeypair -v -keystore ~/keys/my-game.keystore -alias upload -keyalg RSA -keysize 2048 -validity 10000
```

## Step 6: Apple (optional, iOS games only)

1. https://appstoreconnect.apple.com → **Users and Access → Integrations → App Store Connect API → Team Keys → +** → any name, access **App Manager** → **Generate**.
2. **Download API Key** (Apple lets you download it only once) and move it:
   ```bash
   mkdir -p ~/.appstoreconnect/private_keys && mv ~/Downloads/AuthKey_*.p8 ~/.appstoreconnect/private_keys/
   ```
3. In `config.env`: `ASC_KEY_ID=` the Key ID from the list, `ASC_ISSUER_ID=` the Issuer ID shown above the list, and `ASC_KEY_PATH=~/.appstoreconnect/private_keys/AuthKey_<KEY_ID>.p8`
4. `TEAM_ID=`: https://developer.apple.com/account → **Membership details → Team ID**.
5. **Xcode → Settings → Accounts → +** → sign in with an Apple ID that is **Account Holder or Admin** on the team. Then **Manage Certificates → + → Apple Distribution**.
6. Each game must exist in App Store Connect (**Apps → +**) with the same bundle ID you put in `projects.json`.

## Step 7: Tell it about your games

Open `projects.json` (`open -e projects.json`) and replace the two example games with yours:

```json
{
  "my-game": {
    "repo_path": "~/Documents/GitHub/my-game",
    "branch": "main",
    "package_name": "com.mystudio.mygame",
    "keystore": "main",
    "key_alias": "upload",
    "cheat_defines": ["CHEATS"],
    "platforms": ["android"]
  }
}
```

- `"my-game"`: the name you'll pick in Discord.
- `repo_path`: where the game is on this Mac (Step 1.6). `branch`: its main branch.
- `package_name`: Unity → **Project Settings → Player → Android → Identification → Package Name**.
- `keystore` / `key_alias`: from Step 5.
- `cheat_defines`: the scripting define your cheat menu hides behind (`#if CHEATS`). Leave `[]` if you have none.
- `platforms`: `["android"]`, `["ios"]` or `["android", "ios"]`. For iOS also add `"bundle_id": "com.mystudio.mygame"`.

More than one game? Add another block after a comma. Every option: [Adding a game](DEVELOPERS.md#adding-a-game).

## Step 8: Check and start

```bash
./setup.sh              # repeat until it says "All required checks passed."
./setup.sh --install    # starts the bot now and every time you log in
```

Your bot now shows as online in Discord. In your build channel type `/build`, pick your game and press Enter. **The first build of a game takes 10–30 minutes** (it downloads Unity and imports every asset). After that, a few minutes.

To keep it running when you're away (reboots, power cuts, sleep), see [Power loss, reboots, sleep](DEVELOPERS.md#power-loss-reboots-sleep). Before letting other people use it, read the [Security model](README.md#security-model).
