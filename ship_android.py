#!/usr/bin/env python3
"""
ship_android.py — Unity Android AAB build + Google Play upload (multi-project)

Usage:
    python3 ship_android.py <app>            # one app from projects.json
    python3 ship_android.py --all            # every app, sequentially
    python3 ship_android.py <app> --no-upload
    python3 ship_android.py --list

Steps per app:
    1. Resolve app from projects.json, validate branch, refuse a dirty working tree
    2. git fetch + checkout the configured branch
    3. Ask Google Play for the highest versionCode it has ever seen  → new = max(local, play) + 1
    4. Copy unity-side/ShipKit.cs into the project, run Unity batchmode, remove it again
    5. aapt2 dump badging the .aab — assert targetSdk and package name before anything is uploaded
    6. Play Developer API v3: edit → upload bundle → set track (internal, draft) → commit

Shares config.env, logging and git helpers with ship.py.
Logs: ./last-android-build.log
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import ship
from ship import _ENV, die, log, run, section, validate_branch

SCRIPT_DIR = Path(__file__).parent
PROJECTS_FILE = SCRIPT_DIR / "projects.json"
BUILD_SCRIPT_SRC = SCRIPT_DIR / "unity-side" / "ShipKit.cs"
BUILD_SCRIPT_REL = "Assets/Editor/ShipKit.cs"

TARGET_SDK = 36  # default; override per game with "target_sdk" in projects.json
PLAY_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
PLAY_API = "https://androidpublisher.googleapis.com/androidpublisher/v3"
PLAY_UPLOAD_API = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3"


# ── Config / projects ─────────────────────────────────────────────────────────

def load_projects() -> dict:
    if not PROJECTS_FILE.exists():
        die(f"Missing {PROJECTS_FILE}",
            hint="Copy projects.json from the repo and fill in the app entries.",
            category="BAD CONFIG")
    return json.loads(PROJECTS_FILE.read_text())


def resolve_app(projects: dict, cfg: dict, key: str) -> dict:
    if key not in projects:
        die(f"Unknown app '{key}'",
            hint="Known apps:\n  " + "\n  ".join(sorted(projects)),
            category="BAD ARG")
    app = dict(projects[key])
    app["key"] = key

    for required in ("repo_path", "branch", "package_name", "keystore", "key_alias"):
        if not app.get(required):
            die(f"[{key}] projects.json is missing '{required}'", category="BAD CONFIG")

    repo = Path(os.path.expanduser(app["repo_path"]))
    if not (repo / "ProjectSettings" / "ProjectSettings.asset").exists():
        die(f"[{key}] Not a Unity project: {repo}",
            hint="Clone it first, or fix repo_path in projects.json.",
            category="BAD CONFIG")
    app["repo_path"] = str(repo)
    app.update(resolve_keystore(app, cfg))
    app["unity_path"] = resolve_unity(app, cfg)
    return app


def resolve_keystore(app: dict, cfg: dict) -> dict:
    """Keystore path + passwords from config.env / env, keyed by the keystore name."""
    key = app["key"]
    ks = app["keystore"].upper()
    ks_path = cfg.get(f"KEYSTORE_{ks}_PATH")
    ks_pass = cfg.get(f"KEYSTORE_{ks}_PASS")
    alias_pass = cfg.get(f"KEYSTORE_{ks}_ALIASPASS") or ks_pass
    if not ks_path or not ks_pass:
        die(f"[{key}] KEYSTORE_{ks}_PATH / KEYSTORE_{ks}_PASS not set in config.env",
            hint="An unsigned or debug-signed AAB is rejected by Play. See config.env.example.",
            category="BAD CONFIG")
    ks_path = os.path.expanduser(ks_path)
    if not Path(ks_path).exists():
        die(f"[{key}] Keystore not found: {ks_path}", category="BAD CONFIG")
    return {"keystore_path": ks_path, "keystore_pass": ks_pass, "keyalias_pass": alias_pass}


def editors_dir(cfg: dict) -> str:
    return cfg.get("UNITY_EDITORS_DIR") or "/Applications/Unity/Hub/Editor"


def self_bump_back(app: dict, version: str) -> str:
    """The version one self_bumping step (+0.01) below `version`. Only X.YY versions
    can self-bump: the project's preprocessor does float math on bundleVersion."""
    if not re.fullmatch(r"\d+\.\d{2}", version):
        die(f"[{app['key']}] self_bumping needs an X.YY bundleVersion, got {version!r}",
            hint="Use a version like 1.05, or remove self_bumping from projects.json.",
            category="BAD CONFIG")
    return f"{float(version) - 0.01:.2f}"


def resolve_unity(app: dict, cfg: dict) -> str:
    """Editor for this project, and a guard that it matches the project.

    Opening a project with a newer editor silently upgrades it — re-serialising
    assets and rewriting ProjectVersion.txt. That is a real change to ship, not a
    build detail, so a mismatch is an error rather than something to discover in
    `git status` afterwards.
    """
    version = app.get("unity_version")
    unity = (f"{editors_dir(cfg)}/{version}/Unity.app/Contents/MacOS/Unity"
             if version else cfg["UNITY_PATH"])
    if not Path(unity).exists():
        die(f"[{app['key']}] Unity {version or ''} not installed at {unity}",
            hint="Install it via Unity Hub, or fix unity_version in projects.json.",
            category="BAD CONFIG")

    pv = Path(app["repo_path"]) / "ProjectSettings" / "ProjectVersion.txt"
    m = re.search(r"m_EditorVersion:\s*(\S+)", pv.read_text()) if pv.exists() else None
    if m and m.group(1) not in unity:
        die(f"[{app['key']}] Project is Unity {m.group(1)}, but the build would use "
            f"{version or Path(unity).parents[3].name}",
            hint="Building with a different editor upgrades the project in place. "
                 f"Set \"unity_version\": \"{m.group(1)}\" for this app in projects.json.",
            category="BAD CONFIG")
    if version:
        log(f"[{app['key']}] Unity {version}")
    return unity


# ── Google Play Developer API ────────────────────────────────────────────────

def _http(method: str, url: str, token: str, body=None, content_type=None,
          content_length: int | None = None) -> dict:
    import ssl
    import certifi
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(req, timeout=1800, context=ctx) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        die(f"Play API {e.code} on {method} {url.split('?')[0]}: {detail}",
            hint="Common causes: the service account lacks Release Manager on this app, "
                 "or the app has never had a manual release.",
            category="PLAY FAILED")


def play_token(sa_path: str, scope: str = PLAY_SCOPE) -> str:
    ship._ensure_packages()
    import jwt
    sa = json.loads(Path(os.path.expanduser(sa_path)).read_text())
    now = int(time.time())
    assertion = jwt.encode(
        {
            "iss": sa["client_email"],
            "scope": scope,
            "aud": "https://oauth2.googleapis.com/token",
            "iat": now,
            "exp": now + 3600,
        },
        sa["private_key"], algorithm="RS256",
    )
    if not isinstance(assertion, str):
        assertion = assertion.decode()

    import ssl
    import certifi
    import urllib.parse
    data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            return json.loads(r.read())["access_token"]
    except urllib.error.HTTPError as e:
        die(f"Google OAuth {e.code}: {e.read().decode(errors='replace')}",
            hint=f"Check the service account key at {sa_path}.",
            category="PLAY FAILED")


def play_edit_insert(pkg: str, token: str) -> str:
    return _http("POST", f"{PLAY_API}/applications/{pkg}/edits", token,
                 body=b"{}", content_type="application/json")["id"]


def play_commit(pkg: str, token: str, edit_id: str) -> None:
    """Commit an edit, coping with Play's two mutually exclusive commit modes.

    Whether `changesNotSentForReview` is required or forbidden depends on the app's
    review state, and Play rejects the wrong choice either way:

      * app with a pending review (e.g. after a policy flag) →
        "Changes cannot be sent for review automatically. Please set ... to true."
      * app that has never shipped to production →
        "Changes are sent for review automatically. The query parameter ... must
        not be set."

    There is no field telling us which applies, so try one and flip on that error.
    """
    import ssl
    import certifi
    ctx = ssl.create_default_context(cafile=certifi.where())
    base = f"{PLAY_API}/applications/{pkg}/edits/{edit_id}:commit"

    last = ""
    for suffix in ("?changesNotSentForReview=true", ""):
        req = urllib.request.Request(
            base + suffix, data=b"", method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
                r.read()
            log("Committed" + (" (not sent for review)" if suffix else
                               " (sent for review automatically)"))
            return
        except urllib.error.HTTPError as e:
            last = e.read().decode(errors="replace")
            if e.code == 400 and "changesNotSentForReview" in last:
                continue  # wrong mode for this app — try the other one
            die(f"Play commit {e.code} for {pkg}: {last}", category="PLAY FAILED")

    die(f"Play refused both commit modes for {pkg}: {last}", category="PLAY FAILED")


def play_edit_delete(pkg: str, token: str, edit_id: str) -> None:
    try:
        _http("DELETE", f"{PLAY_API}/applications/{pkg}/edits/{edit_id}", token)
    except SystemExit:
        pass  # cleanup is best-effort; the real error is already reported


def play_highest_version_code(pkg: str, token: str, edit_id: str) -> int:
    """Highest versionCode Play knows about — uploaded bundles AND anything in a track.

    Local ProjectSettings.asset routinely lags production, so this is the number that
    matters. bundles.list covers drafts and superseded uploads that tracks.list omits.
    """
    codes = []

    bundles = _http("GET", f"{PLAY_API}/applications/{pkg}/edits/{edit_id}/bundles", token)
    codes += [int(b["versionCode"]) for b in bundles.get("bundles", [])]

    tracks = _http("GET", f"{PLAY_API}/applications/{pkg}/edits/{edit_id}/tracks", token)
    for track in tracks.get("tracks", []):
        for release in track.get("releases", []):
            codes += [int(c) for c in release.get("versionCodes", [])]
            if release.get("versionCodes"):
                log(f"  track '{track['track']}': {release.get('status')} "
                    f"{release.get('name', '?')} → {release['versionCodes']}")

    return max(codes) if codes else 0


def play_upload(app: dict, aab: Path, cfg: dict, track: str) -> None:
    section(f"[{app['key']}] Uploading to Play ({track}, draft)")
    pkg = app["package_name"]
    token = play_token(cfg["PLAY_SERVICE_ACCOUNT"])
    edit_id = play_edit_insert(pkg, token)
    log(f"Edit {edit_id} opened for {pkg}")

    try:
        size = aab.stat().st_size
        log(f"Uploading {aab.name} ({size / 1024 / 1024:.0f} MB) — this takes a few minutes...")
        with open(aab, "rb") as fh:
            bundle = _http(
                "POST",
                f"{PLAY_UPLOAD_API}/applications/{pkg}/edits/{edit_id}/bundles?uploadType=media",
                token, body=fh, content_type="application/octet-stream", content_length=size,
            )
        version_code = int(bundle["versionCode"])
        log(f"Uploaded as versionCode {version_code}")

        _http("PUT", f"{PLAY_API}/applications/{pkg}/edits/{edit_id}/tracks/{track}", token,
              body=json.dumps({
                  "track": track,
                  "releases": [{
                      "name": f"{app['new_version']} ({version_code})",
                      "versionCodes": [str(version_code)],
                      "status": "draft",
                  }],
              }).encode(), content_type="application/json")

        play_commit(pkg, token, edit_id)
        log(f"Draft release on '{track}' — review and promote it in the Play Console.")
    except BaseException:
        play_edit_delete(pkg, token, edit_id)
        raise

    # Read back what Play actually accepted.
    verify_edit = play_edit_insert(pkg, play_token(cfg["PLAY_SERVICE_ACCOUNT"]))
    accepted = play_highest_version_code(pkg, token, verify_edit)
    play_edit_delete(pkg, token, verify_edit)
    log(f"Play now reports highest versionCode = {accepted}")


# ── ProjectSettings (read-only) ──────────────────────────────────────────────

def read_android_settings(repo_path: str) -> tuple[str, int, int]:
    text = (Path(repo_path) / "ProjectSettings" / "ProjectSettings.asset").read_text()

    def grab(key: str, default: str) -> str:
        m = re.search(rf"^\s*{key}:\s*(\S+)", text, re.MULTILINE)
        return m.group(1) if m else default

    version = grab("bundleVersion", "1.0")
    code = int(grab("AndroidBundleVersionCode", "0"))
    target = int(grab("AndroidTargetSdkVersion", "0"))
    log(f"ProjectSettings: version={version} versionCode={code} "
        f"targetSdk={target if target else 'auto'}")
    return version, code, target


# ── Unity build ──────────────────────────────────────────────────────────────

def restore_repo(repo_path: str, key: str) -> None:
    """Undo what the build wrote into the game repo.

    Unity serialises PlayerSettings to ProjectSettings.asset when a batchmode process
    exits, so the version, versionCode, target SDK and keystore path we set in memory
    land on disk anyway — plus incidental churn like URP global settings. require_clean()
    guarantees the tree was pristine beforehand, so a blanket restore is safe here.
    """
    subprocess.run(["git", "-C", repo_path, "checkout", "--", "."],
                   capture_output=True, text=True, env=_ENV)
    leftover = subprocess.run(["git", "-C", repo_path, "status", "--porcelain"],
                              capture_output=True, text=True, env=_ENV).stdout.strip()
    if leftover:
        log(f"[{key}] Untracked files left by the build (not deleted):\n  "
            + "\n  ".join(leftover.splitlines()[:10]), "WARN")
    else:
        log(f"[{key}] Repo restored to a clean tree.")


def build_unity(app: dict, cfg: dict, out_aab: Path, dev: bool, restore: bool = True) -> Path:
    section(f"[{app['key']}] Unity build → {out_aab.name}")
    repo = Path(app["repo_path"])
    dropped = repo / BUILD_SCRIPT_REL
    unity_log = SCRIPT_DIR / f"unity-android-{app['key']}.log"

    if out_aab.exists():
        out_aab.unlink()
    out_aab.parent.mkdir(parents=True, exist_ok=True)

    dropped.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(BUILD_SCRIPT_SRC, dropped)
    log(f"Dropped {BUILD_SCRIPT_REL} into the project (removed after the build)")

    cmd = [
        app["unity_path"], "-batchmode", "-quit",
        "-projectPath", app["repo_path"],
        "-buildTarget", "Android",
        "-executeMethod", "ShipKit.Android",
        "-logFile", str(unity_log),
        "-buildOutput", str(out_aab),
        "-shipkitVersion", app["arg_version"],
        "-shipkitCode", str(app["arg_code"]),
        "-targetSdk", str(app.get("target_sdk", TARGET_SDK)),
        "-keystore", app["keystore_path"],
        "-keyalias", app["key_alias"],
    ]
    if dev:
        cmd += ["-shipkitDev", "true"]

    # Passwords go through the environment, not argv — argv is visible in `ps`.
    env = {
        **_ENV,
        "SHIPKIT_KEYSTORE_PASS": app["keystore_pass"],
        "SHIPKIT_KEYALIAS_PASS": app["keyalias_pass"],
    }

    log(f"Unity log → {unity_log}")
    log(f"Building {app['package_name']} v{app['new_version']} ({app['new_code']}) "
        f"targetSdk={app.get('target_sdk', TARGET_SDK)}. IL2CPP takes 15-25 min.")
    started = time.time()
    result = subprocess.run(cmd, env=env)

    try:
        dropped.unlink(missing_ok=True)
        Path(str(dropped) + ".meta").unlink(missing_ok=True)
        # Only remove Assets/Editor if we created it and it is now empty again.
        if dropped.parent.is_dir() and not any(dropped.parent.iterdir()):
            dropped.parent.rmdir()
            Path(str(dropped.parent) + ".meta").unlink(missing_ok=True)
    except OSError as e:
        log(f"Could not remove {BUILD_SCRIPT_REL}: {e}", "WARN")

    if restore:
        restore_repo(app["repo_path"], app["key"])

    if result.returncode != 0:
        tail = []
        try:
            lines = unity_log.read_text(errors="replace").splitlines()
            errs = [l for l in lines if any(t in l for t in (
                "error CS", "Exception:", "BuildFailedException", "[ShipKit] ERROR",
                "[ShipKit] FAILED", "Assertion failed"))]
            tail = errs[-15:] if errs else lines[-30:]
            log("=== Unity log (relevant tail) ===", "ERROR")
            for l in tail:
                print(l)
        except OSError as e:
            log(f"Could not read Unity log: {e}", "ERROR")
        die(f"[{app['key']}] Unity build failed (exit {result.returncode})",
            hint=f"Full log: {unity_log}\n\n" + "\n".join(tail[-10:]),
            category="UNITY FAILED")

    if not out_aab.exists():
        # A project's own IPostprocessBuildWithReport may rename the artifact — e.g.
        # a BuildVersionRenamer appends the bundleVersion, and when a sibling
        # post-processor has already bumped that version the name gains a
        # second suffix (my-game-0.24.aab -> my-game-0.24-0.25.aab). The bundle
        # itself is still correct; verify_aab() re-checks versionCode against the
        # target, so trusting the file Unity actually produced is safe.
        produced = sorted((p for p in out_aab.parent.glob("*.aab")
                           if p.stat().st_mtime >= started),
                          key=lambda p: p.stat().st_mtime)
        if not produced:
            die(f"[{app['key']}] Unity reported success but {out_aab} does not exist",
                hint=f"Check {unity_log}", category="UNITY FAILED")
        out_aab = produced[-1]
        log(f"Output was renamed by the project's build post-processor → {out_aab.name}")

    log(f"Build complete in {(time.time() - started) / 60:.1f} min "
        f"({out_aab.stat().st_size / 1024 / 1024:.0f} MB)")
    return out_aab


# ── Artifact verification ────────────────────────────────────────────────────

def _jdk_tool(cfg: dict, name: str, unity_path: str | None = None) -> str:
    """java / jarsigner from the JDK Unity ships — there is no system Java here."""
    override = cfg.get(f"{name.upper()}_PATH")
    if override:
        return override
    # .../<version>/Unity.app/Contents/MacOS/Unity → .../<version>/
    tool = (Path(unity_path or cfg["UNITY_PATH"]).parents[3]
            / "PlaybackEngines/AndroidPlayer/OpenJDK/bin" / name)
    if not tool.exists():
        die(f"{name} not found at {tool}",
            hint=f"Set {name.upper()}_PATH in config.env.", category="BAD CONFIG")
    return str(tool)


def find_bundletool(cfg: dict) -> str:
    bt = cfg.get("BUNDLETOOL_PATH") or str(SCRIPT_DIR / "tools" / "bundletool.jar")
    if not Path(bt).exists():
        die(f"bundletool.jar not found at {bt}",
            hint="Download it once:\n  mkdir -p tools && curl -sL -o tools/bundletool.jar \\\n"
                 "    https://github.com/google/bundletool/releases/download/1.18.1/"
                 "bundletool-all-1.18.1.jar",
            category="BAD CONFIG")
    return bt


def verify_aab(app: dict, aab: Path, cfg: dict, require_signed: bool = True) -> str:
    """The check that the whole exercise actually worked. Never upload without it.
    Returns the versionName actually in the bundle (project post-processors may bump it).

    Development bundles are never signed (AGP only signs non-debuggable bundles), so
    require_signed=False skips that check — dev builds are for sideloading, never Play.

    aapt2 cannot read an .aab — a bundle stores its manifest as protobuf, so this
    goes through bundletool.
    """
    section(f"[{app['key']}] Verifying {aab.name}")
    java = _jdk_tool(cfg, "java", app.get("unity_path"))
    out = subprocess.run([java, "-jar", find_bundletool(cfg), "dump", "manifest",
                          "--bundle", str(aab)], capture_output=True, text=True, env=_ENV)
    if out.returncode != 0:
        die(f"[{app['key']}] bundletool could not read {aab.name}",
            hint=(out.stderr or out.stdout).strip()[-500:], category="VERIFY FAILED")

    def field(pattern: str) -> str | None:
        m = re.search(pattern, out.stdout)
        return m.group(1) if m else None

    pkg = field(r'package="([^"]+)"')
    target = field(r'android:targetSdkVersion="(\d+)"')
    code = field(r'android:versionCode="(\d+)"')
    log(f"package={pkg} versionCode={code} targetSdk={target} "
        f"minSdk={field(r'android:minSdkVersion=.(\d+)')}")

    if require_signed:
        # An unsigned or debug-signed bundle is rejected by Play — catch it here, not
        # after a 60 MB upload.
        signed = subprocess.run([_jdk_tool(cfg, "jarsigner", app.get("unity_path")),
                                 "-verify", "-certs",
                                 "-verbose:summary", str(aab)],
                                capture_output=True, text=True, env=_ENV)
        if "jar verified" not in signed.stdout:
            die(f"[{app['key']}] {aab.name} is not properly signed",
                hint=(signed.stdout + signed.stderr).strip()[-500:], category="VERIFY FAILED")
        cn = re.search(r"X\.509,\s*(\S[^\n]*)", signed.stdout)
        signer = cn.group(1).strip() if cn else "unknown"
        if "Android Debug" in signer:
            die(f"[{app['key']}] {aab.name} is debug-signed — Play will reject it",
                category="VERIFY FAILED")
        log(f"Signed by: {signer}")
    else:
        log("Development bundle — signature check skipped (AGP leaves debug bundles unsigned)")

    if require_signed and field(r'android:debuggable="(true)"'):
        die(f"[{app['key']}] Release bundle is marked debuggable — Play rejects it",
            hint="Something forces android:debuggable=\"true\" — usually a custom "
                 "Assets/Plugins/Android/AndroidManifest.xml. Remove the attribute; Unity/Gradle "
                 "set it per build type (dev builds stay debuggable).",
            category="VERIFY FAILED")

    if pkg != app["package_name"]:
        die(f"[{app['key']}] Package mismatch: built '{pkg}', expected "
            f"'{app['package_name']}'", category="VERIFY FAILED")
    if target is None or int(target) < 35:
        die(f"[{app['key']}] targetSdkVersion is {target} — Play requires 35+",
            hint="Raise target_sdk in projects.json (or the TARGET_SDK default). Not uploaded.",
            category="VERIFY FAILED")
    if int(code) != app["new_code"]:
        die(f"[{app['key']}] versionCode is {code}, expected {app['new_code']}",
            category="VERIFY FAILED")

    verify_application_class(app, aab, out.stdout)
    log(f"Verified: targetSdk {target} clears the Play requirement.")
    return field(r'android:versionName="([^"]+)"') or app.get("new_version", "?")


def verify_application_class(app: dict, aab: Path, manifest: str) -> None:
    """Assert the manifest's application class actually exists in the bundle.

    A bundle can be correctly named, versioned, targeted and signed and still die
    on launch because the class <application android:name=...> points at was never
    packaged — which is exactly what happened when a gradle migration dropped the
    multidex dependency while the manifest still named
    androidx.multidex.MultiDexApplication:

        java.lang.ClassNotFoundException: Didn't find class
        "androidx.multidex.MultiDexApplication" on path: DexPathList

    Google Play rejects that under the Broken Functionality policy, so it has to
    fail here rather than in users' hands.
    """
    import zipfile

    app_tag = re.search(r"<application\b[^>]*>", manifest, re.S)
    if not app_tag:
        log("No <application> tag in the manifest — skipping class check", "WARN")
        return
    name = re.search(r'android:name="([^"]+)"', app_tag.group(0))
    if not name:
        log("Manifest declares no application class — nothing to check")
        return

    cls = name.group(1)
    descriptor = ("L" + cls.replace(".", "/") + ";").encode()

    with zipfile.ZipFile(aab) as z:
        dex_names = [n for n in z.namelist()
                     if n.startswith("base/dex/") and n.endswith(".dex")]
        if not dex_names:
            die(f"[{app['key']}] {aab.name} contains no base/dex/*.dex",
                category="VERIFY FAILED")
        found = any(descriptor in z.read(n) for n in dex_names)

    if not found:
        die(f"[{app['key']}] Application class {cls} is not in the bundle",
            hint=f"The manifest declares it, but it is absent from "
                 f"{len(dex_names)} dex file(s). The app would crash on launch with "
                 f"ClassNotFoundException. A dependency providing it is probably "
                 f"missing from mainTemplate.gradle.",
            category="VERIFY FAILED")
    log(f"Application class present: {cls} ({len(dex_names)} dex files)")


def build_universal_apk(app: dict, aab: Path, cfg: dict) -> Path:
    """Produce a sideloadable APK from the AAB that was just verified.

    Derived from the bundle rather than built again with buildAppBundle = false:
    one Unity build instead of two, and the APK is provably the same code that
    goes to Play.
    """
    import zipfile

    section(f"[{app['key']}] Building universal APK")
    apks = aab.with_suffix(".apks")
    apk = aab.with_suffix(".apk")

    # Passwords go to bundletool as files in a private (0700) temp dir, never on the
    # command line (visible in `ps`). bundletool reads the first line of each file.
    with tempfile.TemporaryDirectory() as tmp:
        ks_pass, key_pass = Path(tmp, "ks"), Path(tmp, "key")
        ks_pass.write_text(app["keystore_pass"])
        key_pass.write_text(app["keyalias_pass"])
        res = subprocess.run([
            _jdk_tool(cfg, "java", app.get("unity_path")), "-jar", find_bundletool(cfg),
            "build-apks", "--bundle", str(aab), "--output", str(apks),
            "--mode=universal", "--overwrite",
            "--ks", app["keystore_path"],
            "--ks-key-alias", app["key_alias"],
            f"--ks-pass=file:{ks_pass}",
            f"--key-pass=file:{key_pass}",
        ], capture_output=True, text=True, env=_ENV)
    if res.returncode != 0:
        die(f"[{app['key']}] bundletool could not build the APK",
            hint=(res.stderr or res.stdout).strip()[-500:], category="APK FAILED")

    with zipfile.ZipFile(apks) as z:
        names = [n for n in z.namelist() if n.endswith("universal.apk")]
        if not names:
            die(f"[{app['key']}] No universal.apk inside {apks.name}",
                hint=f"Archive contains: {', '.join(z.namelist()[:10])}",
                category="APK FAILED")
        apk.write_bytes(z.read(names[0]))
    apks.unlink(missing_ok=True)

    log(f"APK: {apk}  ({apk.stat().st_size / 1024 / 1024:.0f} MB)")
    log(f"Install with: adb install -r {apk}")
    return apk


# ── Git ──────────────────────────────────────────────────────────────────────

def require_clean(repo_path: str, key: str) -> None:
    # -uno: tracked changes only. Unity leaves untracked junk behind (e.g. the
    # Performance Testing package writes Assets/Resources/PerformanceTestRun*.json),
    # which would otherwise block every run after the first.
    dirty = subprocess.run(["git", "-C", repo_path, "status", "--porcelain", "-uno"],
                           capture_output=True, text=True, env=_ENV).stdout.strip()
    if dirty:
        files = dirty.splitlines()
        die(f"[{key}] Working tree has {len(files)} uncommitted change(s)",
            hint="Refusing to ship a tree that does not match origin. Commit, stash or "
                 "discard first:\n  " + "\n  ".join(files[:10])
                 + (f"\n  ... and {len(files) - 10} more" if len(files) > 10 else ""),
            category="DIRTY TREE")


# ── Per-app pipeline ─────────────────────────────────────────────────────────

def ship_one(key: str, projects: dict, cfg: dict, args) -> dict:
    section(f"═══ {key} ═══")
    app = resolve_app(projects, cfg, key)
    repo = app["repo_path"]

    if not args.skip_git:
        validate_branch(repo, app["branch"])
        require_clean(repo, key)
        run(["git", "-C", repo, "checkout", app["branch"]])
        run(["git", "-C", repo, "merge", "--ff-only", f"origin/{app['branch']}"])
    else:
        log("Skipping git checks (--skip-git)", "WARN")

    version, local_code, local_target = read_android_settings(repo)

    if args.no_upload:
        play_code = 0
        log("--no-upload: skipping the Play version-code query", "WARN")
    else:
        token = play_token(cfg["PLAY_SERVICE_ACCOUNT"])
        edit_id = play_edit_insert(app["package_name"], token)
        log(f"Querying Play for {app['package_name']}...")
        play_code = play_highest_version_code(app["package_name"], token, edit_id)
        play_edit_delete(app["package_name"], token, edit_id)
        log(f"Play highest versionCode = {play_code} (local = {local_code})")

    app["new_code"] = max(local_code, play_code) + 1

    if app.get("self_bumping"):
        # This project's own IPreprocessBuildWithReport does `bundleVersion + 0.01f`
        # and `bundleVersionCode++` during the build, and rejects the build unless the
        # output filename already matches its bumped version. Hand it the values one
        # step below target so it lands exactly on target.
        self_bump_back(app, version)  # validates the X.YY format
        app["new_version"] = args.version or f"{float(version) + 0.01:.2f}"
        app["arg_version"] = version
        app["arg_code"] = app["new_code"] - 1
        log(f"self_bumping: passing {app['arg_version']} ({app['arg_code']}) — "
            f"the project's own preprocessor bumps it")
    else:
        app["new_version"] = args.version or ship.bump_version(version)
        app["arg_version"] = app["new_version"]
        app["arg_code"] = app["new_code"]

    log(f"→ shipping {app['new_version']} ({app['new_code']}), targetSdk {app.get('target_sdk', TARGET_SDK)}")

    # Some projects' BuildPreprocessor rejects any Android output whose filename
    # does not end with "<bundleVersion>.aab".
    out_dir = Path(cfg.get("ANDROID_BUILD_OUTPUT", "/tmp/android-builds"))
    out_aab = out_dir / f"{key}-{app['new_version']}.aab"
    out_aab = build_unity(app, cfg, out_aab, dev=args.dev, restore=not args.skip_git)
    verify_aab(app, out_aab, cfg)

    if args.apk:
        build_universal_apk(app, out_aab, cfg)

    if args.no_upload:
        log(f"--no-upload: artifact left at {out_aab}")
    else:
        play_upload(app, out_aab, cfg, args.track)

    return {"app": key, "version": app["new_version"], "code": app["new_code"],
            "aab": str(out_aab)}


def main() -> None:
    p = argparse.ArgumentParser(description="Build + upload Unity Android AABs to Google Play.")
    p.add_argument("app", nargs="?", help="app key from projects.json")
    p.add_argument("--all", action="store_true", help="ship every app in projects.json")
    p.add_argument("--list", action="store_true", help="list configured apps and exit")
    p.add_argument("--no-upload", action="store_true", help="build + verify only")
    p.add_argument("--apk", action="store_true",
                   help="also emit a sideloadable universal APK from the same bundle")
    p.add_argument("--track", default="internal", help="Play track (default: internal)")
    p.add_argument("--version", help="override bundleVersion instead of auto-bumping")
    p.add_argument("--dev", action="store_true", help="development build (never upload these)")
    p.add_argument("--skip-git", action="store_true", help="build the tree as-is")
    args = p.parse_args()

    projects = load_projects()

    if args.list:
        for k, v in sorted(projects.items()):
            print(f"  {k:26s} {v['package_name']:34s} {v['repo_path']}")
        return

    if not args.app and not args.all:
        p.error("give an app key, or --all (see --list)")

    ship.LOG_FILE = SCRIPT_DIR / "last-android-build.log"
    cfg = ship.load_config()
    for required in ("UNITY_PATH", "PLAY_SERVICE_ACCOUNT"):
        if not cfg.get(required):
            die(f"{required} not set in config.env", category="BAD CONFIG")

    keys = sorted(projects) if args.all else [args.app]
    results, failures = [], []
    for key in keys:
        if len(keys) > 1:
            # One bad app must not abort the other eight.
            try:
                results.append(ship_one(key, projects, cfg, args))
            except SystemExit:
                failures.append(key)
                log(f"[{key}] FAILED — continuing with the rest", "ERROR")
        else:
            results.append(ship_one(key, projects, cfg, args))

    section("Summary")
    for r in results:
        log(f"  ✅ {r['app']:26s} {r['version']} ({r['code']})")
    for f in failures:
        log(f"  ❌ {f}")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
