# Troubleshooting — Unity 6 batch builds, the build bot, Play and Drive

Field notes from real production debugging. Save yourself the 10-minute build cycles.

---

## 1. URP `DecalRendererFeature` blackens objects in batch builds

**Symptom:** Some MeshRenderers (commonly using `URP/Unlit` opaque) render fully **black** in batch-mode iOS builds, but render correctly in:
- Unity Editor Play mode
- Manual Editor builds (File → Build Settings → Build)

The exact same project, same scene, same materials.

**Affected:** Unity 6 (`6000.0.x`) + URP 17.x + Metal renderer + iOS.

**Root cause:** `DecalRendererFeature` in your URP Renderer asset has `technique: 0` (Automatic). In batch builds, Automatic resolves differently than in Editor — selecting `DBuffer` mode, which strips the unlit pass output of opaque non-decal materials on iOS Metal.

**Fix:** Set Decal technique to **Screen Space** (not Automatic, not DBuffer).

In `Assets/Settings/<YourRenderer>.asset`, find:
```yaml
m_Name: DecalRendererFeature
m_Settings:
  technique: 0    # ← change this
```
to:
```yaml
m_Name: DecalRendererFeature
m_Settings:
  technique: 2    # 2 = ScreenSpace
```

Or in the Editor: Select your URP Renderer asset → Decal → Technique → **Screen Space**.

**How we found it:** Built a simple `Quad` GameObject with a basic URP/Unlit material. It rendered black too. Removed renderer features one by one until Decal was the last one, then changed Automatic → ScreenSpace and the bug vanished.

**Why Editor masked it:** Editor uses different shader-variant-stripping rules and a more permissive default for `Automatic` resolution. The bug only manifests after batch-build's stricter variant pruning.

---

## 2. `Sprites/Default` shader on a `MeshRenderer`

**Symptom:** You assigned `Sprites/Default` to a `MeshRenderer` (e.g. on an FBX-imported model) hoping for cheap unlit rendering. Setting `material.mainTexture = tex` at runtime does nothing — the model is invisible or stuck on the previous texture.

**Root cause:** `Sprites/Default` declares `_MainTex` as `[PerRendererData]`. Unity's `SpriteRenderer` provides this via `MaterialPropertyBlock` automatically. `MeshRenderer` does **not** — it ignores `material.mainTexture` writes for `[PerRendererData]` properties.

**Fix:** Use `URP/Unlit` (or any non-`[PerRendererData]` shader) on `MeshRenderer`. If you need transparency, set Surface = Transparent. Or, set the texture via `MaterialPropertyBlock` manually:
```csharp
var mpb = new MaterialPropertyBlock();
renderer.GetPropertyBlock(mpb);
mpb.SetTexture("_MainTex", texture);
renderer.SetPropertyBlock(mpb);
```

---

## 3. Voice/character outline materials going black after first fix

**Symptom:** After fixing decal blackening, a separate "outline" sub-material on the same model still renders black.

**Root cause:** Outline material was instantiated at runtime via `renderer.material` (which clones it). The clone lost the outline shader's color value when texture assignment code ran a fallback path.

**Fix:** Special-case outline renderers in your texture-loading code:
```csharp
if (renderer.name.Contains("outline", StringComparison.InvariantCultureIgnoreCase))
{
    if (renderer.material != null) renderer.material.color = Color.white;
    continue;
}
```

---

## 4. FBX imports breaking shader compilation

**Symptom:** Cryptic shader errors in batch builds for specific FBX models, fine in Editor.

**Fix:** Open the FBX import settings in Editor and ensure:
- `Read/Write Enabled` = `true`
- `Normals` import mode = `Calculate` (not Import)
- `Tangents` import mode = `Calculate Tangent Space` (not Import)

In `.fbx.meta`:
```yaml
isReadable: 1
normalImportMode: 2     # Calculate
tangentImportMode: 1    # CalculateTangentSpace
```

Forces consistent normal/tangent generation regardless of source FBX exporter quirks.

---

## 5. Batch build fails with `Unity Editor is open`

> The Discord bot builds in its own clones (`~/ShipKit/work`), so it can't hit this. It only applies to `ship.py` / `ship_android.py` run directly against your working repo.

**Symptom:** `ship.py` aborts with `UNITY EDITOR OPEN` — but you closed Unity yesterday.

**Root cause:** `<project>/Temp/UnityLockfile` is held by an `flock()` for the lifetime of the Editor session. Stale lockfile (file present but no process holding it) is fine — `ship.py` checks `lsof` to verify.

**Fix:** If you're sure Editor is closed, delete the lockfile:
```bash
rm <project>/Temp/UnityLockfile
```

If still held, find the process:
```bash
lsof <project>/Temp/UnityLockfile
```

---

## 6. `BuildVersionAutoIncrement` overrides your CI version

**Symptom:** You pass `-buildVersion 4.0 -buildNumber 500`, but the resulting Xcode project has `4.0.1` and `501`.

**Root cause:** Some projects ship a `BuildVersionAutoIncrement.cs` Editor script that bumps version on every build. It runs *after* `BuildScript.cs` sets the values.

**Fix:** `ship.py` detects this — it parses the actual values from the renamed Xcode output folder (`<output> v4.0(500)` format from `BuildOutputRenamer.cs`). The `Done` log line shows the *actual* version uploaded.

If you don't want auto-increment, disable that script or guard it on `BatchMode.IsBatchMode`.

---

## 7. Addressables catalog goes empty in batch builds

**Symptom:** App boots to a black screen on device — `Addressables.LoadAssetAsync` finds no entries, but it works in Editor.

**Root cause:** Calling `BuildPlayerContent()` in your `BuildScript.cs` when Addressables groups have been removed (or `m_BuildAddressablesWithPlayerBuild=0`) generates an empty catalog that overwrites the valid `Library/com.unity.addressables/aa/...` catalog.

**Fix:** Don't call `BuildPlayerContent()` from your build script. The shipped `BuildScript.cs` in this kit deliberately omits it. Unity's `AddressablesPlayerBuildProcessor` handles catalog embedding automatically when groups are properly configured.

---

## 8. `xcodebuild archive` signing failures

**Symptom:** `Code Sign error: No matching profiles found`

**Checklist:**
- Apple Distribution cert in login keychain:
  ```bash
  security find-identity -v -p codesigning
  ```
- Provisioning profile downloaded for `BUNDLE_ID` (Xcode → Settings → Accounts → Download Manual Profiles)
- `TEAM_ID` in `config.env` matches the team that owns the bundle ID
- `-allowProvisioningUpdates` flag passed to xcodebuild (it is by default in `ship.py`)

If signing keeps failing, open the project in Xcode once manually, hit "Try Again" on signing errors, then re-run `ship.py`.

---

## 9. ASC API returns 401

**Causes:**
- `.p8` file path in config wrong
- Key was revoked in ASC → Users & Access → Keys
- System clock drift (JWT expiry validation fails) — check `date`
- Wrong `ASC_ISSUER_ID` (it's a UUID at the top of the Keys page, *not* per-key)

The script generates a fresh JWT per request with 20-minute expiry, so token expiry mid-run shouldn't happen.

---

## 10. Build number not incrementing on TestFlight

**Symptom:** You upload, ASC accepts, but next `ship.py` run picks the same build number again.

**Root cause:** TestFlight is still *processing* the previous build (state: `PROCESSING` not `VALID`). The ASC API `sort=-version&limit=1` query returns it but the next build will conflict.

**Fix:** Wait 5–15 min for processing, or override:
```bash
ship.py main --build 501
```

---

## 11. Process killed mid-build (`exit -15`)

**Symptom:** Bot/CI reports `Build failed (exit -15)`, no clear cause.

**Root cause:** Signal 15 (SIGTERM) — something killed the process: user Ctrl-C, OS OOM killer, CI timeout, or an explicit `/abort` from the Discord bot.

**`ship.py` behavior:** Traps SIGTERM/SIGINT and writes `last-error.json` with `category: "INTERRUPTED"` so wrappers can show the cause instead of just "exit -15".

---

## 12. Domain reload races on scripting-define changes

**Symptom:** `--define +FOO` doesn't seem to take effect mid-build. Scripts that check `#if FOO` behave inconsistently.

**Root cause:** `PlayerSettings.SetScriptingDefineSymbols()` triggers a domain reload — but `BuildScript.cs` is already executing in the loaded domain. The Editor's currently-loaded code won't recompile mid-method.

**`BuildPlayer()` behavior:** It compiles the *player* assemblies fresh with the current defines, so the player build *will* have the new defines. Editor-side already-loaded code retains the old defines for the rest of the batch session.

**Practical impact:** None for player builds — the device gets the right defines. Just don't expect Editor-side `#if` code in the same batch session to react.

---

## 13. Asset patches lingering in working tree

**Symptom:** After `ship.py --asset-patch ...`, your `git status` shows the asset modified.

**Root cause:** `ship.py` crashed before reaching its `finally` block, OR the `git checkout` failed silently.

**Manual fix:**
```bash
git -C <REPO_PATH> checkout -- <Assets/Path.asset>
```

`ship.py`'s `finally` block always tries to restore — but if it doesn't, this command is the safety net.

---

## 14. Stale `bundleVersion` / `buildNumber` from previous CI run

**Symptom:** Build number mysteriously starts from a stale value.

**Root cause:** A previous `ship.py` run was killed after `write_project_settings()` but before commit (and you don't commit version bumps). Next run starts from the stale value.

**`ship.py` behavior:** Stashes uncommitted changes during checkout, pops them after. If the stash pop conflicts on `ProjectSettings.asset`, ship.py drops the conflicted hunk and uses the branch version.

If you see weird version numbers, check:
```bash
git -C <REPO_PATH> stash list   # leftover stashes from killed runs
```

---

## 15. Discord bot doesn't show new slash command options

**Symptom:** You changed a command in `bot.py` (e.g. a new `/build` option) and restarted the bot, but Discord still shows the old options or says "This command is outdated".

**Root cause:** The Discord client caches slash command schemas. The bot syncs to the server (look for `Synced 6 command(s)` in `~/Library/Logs/shipbot.log`), but the client UI lags.

**Fix:**
1. Reload Discord (Ctrl/Cmd+R), or force quit and reopen it.
2. Check the server-side sync in `~/Library/Logs/shipbot.log`.

---

## 16. `unity build` exits 6: "Forwarded argument '-buildVersion' conflicts with a reserved Unity flag"

The Unity CLI reserves `-buildVersion` (and its own versioning flags). Our arguments are prefixed:
`-shipkitVersion`, `-shipkitCode`, `-shipkitDev`. The real reason is in the CLI's JSON result
envelope, which `pipeline.py` now puts in the failure hint. Unity's own log stays empty because
Unity never started.

## 17. Development AAB "is not properly signed / jar is unsigned"

This is normal. The Android Gradle plugin only signs non-debuggable bundles, so a development AAB
is always unsigned even though Unity was given the keystore. The APK derived from it is signed by
bundletool with our keystore, and dev builds never go to Play, so `verify_aab(require_signed=False)`
skips the check for dev builds.

## 18. Output renamed (`my-game-1.01-1.02.aab`), or the version differs from the repo

Some projects have a build post-processor (e.g. a `BuildVersionRenamer` or `BuildVersionAutoIncrement` in a shared core package)
that renames the output or bumps `bundleVersion`. `pipeline.py` takes the newest `.aab` in the
artifact folder and reads the real `versionName` back from the bundle. The version code is still
verified against the number we assigned. If a project also bumps the code, set
`"self_bumping": true` in projects.json.

## 19. Drive: uploads fine, but the cleanup gets 404 on DELETE

A **Content manager** on a Shared Drive can't permanently delete files. The API answers 404, not
403. The prune step moves files to the trash instead (`trashed: true`), and Shared Drive trash
empties itself after 30 days. Service accounts have no storage of their own, so a normal "My Drive"
folder doesn't work: it has to be a Shared Drive.

## 20. Drive link opens a "can't scan for viruses" page

Drive shows this for any file over about 100 MB. The bot's **📱 Install** button and QR code use
`drive.usercontent.google.com/download?id=…&export=download&confirm=t`, which downloads directly.
The 💾 Drive button still goes to the normal page.

## 21. Discord: "This command is outdated, please try again"

The Discord client cached the old command list. It happens right after the bot restarts with
changed commands. Reload Discord (Ctrl/Cmd+R).

## 22. Discord: "… is thinking" forever

A command crashed after deferring. The `@tree.error` handler now replies with the error. Check
`~/Library/Logs/shipbot.log` for the traceback.

## 23. First build of a game takes 10–30 min

Expected. It's a fresh clone plus a full asset import into that clone's own `Library/`. Later builds
reuse it (Idle Race: 10 min first, 4 min after). Use `clean:True` only when the cache is actually
broken.

## 24. iOS export: "Cloud signing permission error / No profiles for '<bundle id>' were found"

`xcodebuild -exportArchive -authenticationKey…` with an **App Manager** API key can't use cloud signing.
The pipeline now signs and uploads through the Apple ID signed in to Xcode (Account Holder/Admin)
and uses the API key only for App Store Connect queries. If this comes back, check that Xcode →
Settings → Accounts is still signed in.

## 25. TestFlight "What to Test" returns 409 Conflict

Apple often creates the en-US beta localization itself. Update it (PATCH) instead of creating a
new one.

## 26. iOS version in TestFlight differs from the repo (0.67 → 0.68)

The project's build preprocessor bumps `bundleVersion` during the build. The bot reads
`CFBundleShortVersionString` from the generated Xcode project and reports that.

## 27. iOS archive from the bot: "Undefined il2cpp.a" / Ld UnityFramework failed

The real error sits higher up in the log: *"Executable softwareupdate could not be found"*. IL2CPP's
build program runs `/usr/sbin/softwareupdate` to find Xcode, and launchd agents get a minimal PATH
without `/usr/sbin`. It works from Terminal but fails from the bot. The plist PATH and `ship._ENV`
include `/usr/sbin:/sbin`. After editing the plist, reload it with bootout + bootstrap; a kickstart
keeps the old environment.

## 28. Play upload 403 "APK is marked as debuggable" on a release build

A custom `Assets/Plugins/Android/AndroidManifest.xml` hard-codes `android:debuggable="true"`
(Idle Train did), so even the Gradle `bundleRelease` output is debuggable. Remove the attribute:
Unity/Gradle set it per build type. The bot now removes the attribute from manifests under
`Assets/Plugins/Android` in its clone for release builds (logged as a WARN; the game repo is
untouched), and `verify_aab` still fails any release build that ends up debuggable.

## Debugging tips

**Compare batch vs Editor builds:**
- Batch is stricter on shader variant stripping
- Batch uses different `Application.isEditor` paths
- Batch may apply different graphics settings (check `GraphicsSettings.asset`)

**Frame Debugger isn't available in batch builds.** To diagnose visual issues:
1. Repro the bug in a manual Editor build (File → Build → Run on device)
2. If it doesn't repro in Editor build: it's a batch-mode-specific issue — check shader variant collections, build defines, AssetBundle/Addressables catalogs
3. If it repros in Editor build: it's a runtime issue (use Xcode → Capture GPU Frame on device)

**Always test with `--no-upload` first** when changing build pipeline code. Saves a TestFlight slot per failed attempt.
