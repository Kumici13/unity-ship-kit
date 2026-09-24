#!/usr/bin/env python3
"""
pipeline.py — one build job for bot.py: isolated clone → Unity → Drive / TestFlight.

Usage:
    pipeline.py build  '<job json>'   # {"id","game","platform","format","branch","dev","cheats"}
    pipeline.py upload '<job json>'   # {"id","game"} — push a kept AAB to Play internal
    pipeline.py prune                 # delete Drive + local artifacts older than RETENTION_DAYS

Builds never touch the game repos in projects.json. Each game gets its own clone under
WORK_DIR/work/<game>, reset to origin/<branch> every run; only Library/ survives between
builds. Game repos are only read for their origin URL and for `extra_files`
(gitignored files such as google-services.json).

stdout protocol for bot.py:  '@@PROGRESS <text>'  and one final  '@@RESULT <json>'.
Everything else on stdout is log.
"""

import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ship
import ship_android as sa
from ship import _ENV, die, log, run

SCRIPT_DIR = Path(__file__).parent
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
RETENTION_DAYS = 30
UNITY_TIMEOUT_S = 2 * 3600

cfg = ship.load_config()
WORK = Path(cfg.get("WORK_DIR", "~/ShipKit")).expanduser()
COUNTERS = WORK / "counters.json"
UNITY_CLI = str(Path(cfg.get("UNITY_CLI", "~/.unity/bin/unity")).expanduser())


def progress(text: str) -> None:
    print(f"@@PROGRESS {text}", flush=True)


def result(data: dict) -> None:
    print(f"@@RESULT {json.dumps(data)}", flush=True)


def git(clone: Path, *args: str) -> str:
    return run(["git", "-C", str(clone), *args]).stdout.strip()


# ── Clone ─────────────────────────────────────────────────────────────────────

def prepare_clone(app: dict, branch: str) -> tuple[Path, str]:
    clone = WORK / "work" / app["key"]
    if not (clone / ".git").exists():
        url = git(Path(app["repo_path"]), "remote", "get-url", "origin")
        progress("Cloning repo (first build of this game — Library import will be slow)")
        clone.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--no-checkout", url, str(clone)])

    progress(f"Fetching origin/{branch}")
    ship.validate_branch(str(clone), branch)          # fetch --prune + clear error on typo
    git(clone, "checkout", "-f", "--detach", f"origin/{branch}")
    # Library/ is the import cache — the whole point of keeping the clone.
    git(clone, "clean", "-ffdx", "-e", "/Library/", "-e", "/Logs/")
    git(clone, "submodule", "update", "--init", "--recursive")
    attrs = clone / ".gitattributes"
    if attrs.exists() and "filter=lfs" in attrs.read_text(errors="ignore"):
        git(clone, "lfs", "pull")
    sha = git(clone, "rev-parse", "--short", "HEAD")

    for rel in app.get("extra_files", []):
        src = Path(app["repo_path"]) / rel
        if not src.exists():
            die(f"[{app['key']}] extra file missing: {src}",
                hint="projects.json lists it under extra_files, but it is not in your repo.",
                category="BAD CONFIG")
        (clone / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, clone / rel)
        log(f"Copied extra file {rel}")

    dst = clone / "Assets" / "Editor" / "ShipKit" / "ShipKit.cs"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SCRIPT_DIR / "unity-side" / "ShipKit.cs", dst)
    return clone, sha


# ── Build number ──────────────────────────────────────────────────────────────

def _store_highest(app: dict, platforms: list[str]) -> int:
    """Highest build number Play / TestFlight know. A failed query is not fatal for a
    QA build — the local counter still guarantees uniqueness."""
    best = 0
    if "android" in platforms:
        try:
            token = sa.play_token(cfg["PLAY_SERVICE_ACCOUNT"])
            edit = sa.play_edit_insert(app["package_name"], token)
            best = max(best, sa.play_highest_version_code(app["package_name"], token, edit))
            sa.play_edit_delete(app["package_name"], token, edit)
        except SystemExit:
            log("Play query failed — continuing with local numbers only", "WARN")
    if "ios" in platforms:
        try:
            tf, _ = ship.get_latest_tf_build(app["bundle_id"], *asc_creds(app))
            best = max(best, tf or 0)
        except SystemExit:
            log("TestFlight query failed — continuing with local numbers only", "WARN")
    return best


def next_build_number(app: dict, clone: Path, platforms: list[str]) -> int:
    counters = json.loads(COUNTERS.read_text()) if COUNTERS.exists() else {}
    _, repo_android, _ = sa.read_android_settings(str(clone))
    _, repo_ios = ship.read_project_settings(str(clone))
    store = _store_highest(app, platforms)
    n = max(store, counters.get(app["key"], 0), repo_android, repo_ios) + 1
    log(f"Build number {n} = max(store {store}, issued {counters.get(app['key'], 0)}, "
        f"repo {repo_android}/{repo_ios}) + 1")
    counters[app["key"]] = n
    COUNTERS.write_text(json.dumps(counters, indent=2))
    return n


# ── Unity ─────────────────────────────────────────────────────────────────────

def editor_version(clone: Path) -> str:
    text = (clone / "ProjectSettings" / "ProjectVersion.txt").read_text()
    return re.search(r"m_EditorVersion:\s*(\S+)", text).group(1)


def ensure_modules(version: str, platforms: list[str]) -> str:
    """Path to the editor binary. `unity build --allow-install` installs the editor but
    not platform modules, so add those first when missing."""
    root = Path(sa.editors_dir(cfg)) / version
    wanted = {"android": "AndroidPlayer", "ios": "iOSSupport"}
    missing = [p for p in platforms if not (root / "PlaybackEngines" / wanted[p]).exists()]
    if not root.exists() or missing:
        progress(f"Installing Unity {version} {' + '.join(missing or platforms)} (one-time)")
        if not root.exists():
            run([UNITY_CLI, "install", version, "--yes", "--accept-eula",
                 "--non-interactive", "--no-banner"])
        run([UNITY_CLI, "install-modules", "-e", version, "-m", *(missing or platforms),
             "--cm", "--accept-eula", "--yes", "--non-interactive", "--no-banner"])
    return str(root / "Unity.app" / "Contents" / "MacOS" / "Unity")


PHASES = [("Start importing", "importing assets"), ("il2cpp", "IL2CPP (slowest step)"),
          ("gradle", "Gradle"), ("Compiling shader", "compiling shaders"),
          ("csc", "compiling scripts"), ("Building ", "building player")]


def unity_phase(log_path: Path) -> str:
    """The CLI's progress frames carry no text, so read the phase off the Unity log."""
    try:
        with open(log_path, "rb") as f:
            f.seek(max(0, log_path.stat().st_size - 8192))
            tail = f.read().decode(errors="replace")
    except OSError:
        return "starting"
    last = max(PHASES, key=lambda p: tail.rfind(p[0]))
    return last[1] if tail.rfind(last[0]) >= 0 else "working"


def unity_build(clone: Path, target: str, method: str, out: Path, args: list[str],
                log_path: Path, env: dict | None = None) -> None:
    cmd = [UNITY_CLI, "build", str(clone), "--target", target, "--execute-method", method,
           "--output-path", str(out), "--log-file", str(log_path),
           "--format", "ndjson", "--no-banner", "--non-interactive",
           "--allow-install", "--allow-dirty-build", "--timeout", str(UNITY_TIMEOUT_S),
           "--args", shlex.join(args)]
    if target == "Android":
        cmd += ["--android-export-type", "aab"]
    log("$ " + " ".join(cmd))
    started = time.time()
    cli_errors = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, env=env or _ENV)
    for line in proc.stdout:
        try:
            frame = json.loads(line)
        except ValueError:
            print(line, end="")
            continue
        if frame.get("type") == "progress":
            mins = int(time.time() - started) // 60
            progress(f"Unity {target}: {frame.get('message') or unity_phase(log_path)} ({mins} min)")
        elif frame.get("type") == "result" or "success" in frame:
            log(f"unity build result: {json.dumps(frame)[:500]}")
            cli_errors += [e.get("message", "") for e in frame.get("errors") or []]
    rc = proc.wait()
    if rc != 0:
        tail = []
        try:
            lines = log_path.read_text(errors="replace").splitlines()
            errs = [l for l in lines if any(t in l for t in (
                "error CS", "Exception:", "BuildFailedException", "[ShipKit] ERROR",
                "[ShipKit] FAILED", "Assertion failed"))]
            tail = errs[-15:] if errs else lines[-30:]
        except OSError:
            pass
        die(f"Unity {target} build failed (exit {rc})",
            hint="\n".join(cli_errors + tail[-15:]), category="UNITY FAILED")
    log(f"Unity {target} done in {(time.time() - started) / 60:.1f} min")


def common_args(job: dict, app: dict, version: str, n: int) -> list[str]:
    args = ["-shipkitVersion", version, "-shipkitCode", str(n)]
    if job.get("dev"):
        args += ["-shipkitDev", "true"]
    if job.get("cheats"):
        if not app.get("cheat_defines"):
            die(f"[{app['key']}] has no cheat_defines in projects.json", category="BAD CONFIG")
        args += ["-defineAdd", ",".join(app["cheat_defines"])]
    if app.get("prebuild_method"):
        args += ["-prebuildMethod", app["prebuild_method"]]
    return args


# ── Android ───────────────────────────────────────────────────────────────────

def build_android(job, app, clone, version, n, art: Path) -> dict:
    app.update(sa.resolve_keystore(app, cfg))
    app["new_code"] = n
    app["unity_path"] = ensure_modules(editor_version(clone), ["android"])

    # Projects with their own version-bumping preprocessor (+0.01 / code++) get the
    # values one step below target so they land exactly on target.
    arg_version, arg_code = version, n
    if app.get("self_bumping"):
        arg_version, arg_code = sa.self_bump_back(app, version), n - 1

    # Some projects' build preprocessor rejects Android output unless the filename ends in "<bundleVersion>.aab".
    out = art / f"{app['key']}-{version}.aab"
    args = common_args(job, app, arg_version, n)
    args[args.index("-shipkitCode") + 1] = str(arg_code)
    args += ["-targetSdk", str(app.get("target_sdk", sa.TARGET_SDK)), "-keystore", app["keystore_path"],
             "-keyalias", app["key_alias"]]
    env = {**_ENV, "SHIPKIT_KEYSTORE_PASS": app["keystore_pass"],
           "SHIPKIT_KEYALIAS_PASS": app["keyalias_pass"]}

    if not job.get("dev"):
        strip_debuggable(clone)
    progress("Unity Android build started")
    unity_build(clone, "Android", "ShipKit.Android", out, args,
                art / "unity-android.log", env)

    aabs = sorted(art.glob("*.aab"), key=lambda p: p.stat().st_mtime)
    if not aabs:
        die("Unity reported success but produced no .aab", category="UNITY FAILED")
    aab = aabs[-1]  # a project post-processor may have renamed it

    progress("Verifying AAB (package, targetSdk, signature, versionCode)")
    built_version = sa.verify_aab(app, aab, cfg, require_signed=not job.get("dev"))

    share = sa.build_universal_apk(app, aab, cfg) if job["format"] == "apk" else aab
    tags = "".join(f"-{t}" for t in ("dev", "cheats") if job.get(t))
    if job.get("note"):
        tags += "-" + re.sub(r"[^a-z0-9]+", "-", job["note"].lower()).strip("-")[:24]
    name = f"{app['key']}-{built_version}-{n}-{job['branch'].replace('/', '_')}{tags}{share.suffix}"
    progress(f"Uploading {share.suffix[1:].upper()} to Drive "
             f"({share.stat().st_size / 1024 / 1024:.0f} MB)")
    link = drive_upload(share, app["key"], name)
    return {"file": name, "link": link, "aab": str(aab), "version": built_version,
            "size_mb": round(share.stat().st_size / 1024 / 1024)}


def strip_debuggable(clone: Path) -> None:
    """A hard-coded android:debuggable="true" in a custom manifest makes even release bundles
    debuggable, and Play rejects those. Gradle already sets it per build type, so drop the
    attribute in the throwaway clone. The game repo keeps its own copy (fix it there too)."""
    for mf in (clone / "Assets" / "Plugins" / "Android").rglob("AndroidManifest.xml"):
        text = mf.read_text(errors="replace")
        fixed = re.sub(r'\s+android:debuggable\s*=\s*"[^"]*"', "", text)
        if fixed != text:
            mf.write_text(fixed)
            log(f"Removed android:debuggable from {mf.relative_to(clone)} (release build)", "WARN")


# ── iOS ───────────────────────────────────────────────────────────────────────

def asc_creds(app: dict) -> tuple[str, str, str]:
    """(key_id, issuer_id, key_path) — per-app override, e.g. an app under another team."""
    p = (app.get("asc_prefix") or "ASC").upper()
    creds = tuple(cfg.get(f"{p}_{k}") for k in ("KEY_ID", "ISSUER_ID", "KEY_PATH"))
    if not all(creds):
        die(f"[{app['key']}] {p}_KEY_ID / {p}_ISSUER_ID / {p}_KEY_PATH not set in config.env",
            hint="iOS needs an App Store Connect API key (App Manager role).",
            category="BAD CONFIG")
    return creds[0], creds[1], str(Path(creds[2]).expanduser())


def build_ios(job, app, clone, version, n, art: Path) -> dict:
    if not app.get("bundle_id"):
        die(f"[{app['key']}] projects.json has no bundle_id", category="BAD CONFIG")
    key_id, issuer, key_path = asc_creds(app)
    team = app.get("team_id") or cfg.get("TEAM_ID")
    if not team:
        die("TEAM_ID not set in config.env", category="BAD CONFIG")
    ensure_modules(editor_version(clone), ["ios"])

    xdir = art / "xcode"
    progress("Unity iOS build started")
    # App Store Connect rejects < 15.0 from Spring 2027 (ITMS-90068 warning until then).
    min_ios = app.get("ios_min_version") or cfg.get("IOS_MIN_VERSION", "15.0")
    unity_build(clone, "iOS", "ShipKit.IOS", xdir,
                common_args(job, app, version, n) + ["-iosMinVersion", min_ios],
                art / "unity-ios.log")

    xdir = Path(ship._find_xcode_output(str(xdir), version, n))
    # A project build preprocessor may bump the marketing version — report what ships.
    built = subprocess.run(["/usr/libexec/PlistBuddy", "-c", "Print :CFBundleShortVersionString",
                            str(xdir / "Info.plist")], capture_output=True, text=True).stdout.strip()
    version = built or version
    # Export compliance is a legal declaration, so it's opt-in: IOS_EXEMPT_ENCRYPTION=true
    # (the app only uses the OS's HTTPS) writes ITSAppUsesNonExemptEncryption=NO and
    # TestFlight skips the question. A game with its own crypto sets "uses_encryption": true.
    # Unset → Info.plist untouched; answer the question in App Store Connect per build.
    if cfg.get("IOS_EXEMPT_ENCRYPTION", "").lower() == "true" and not app.get("uses_encryption"):
        plist = str(xdir / "Info.plist")
        subprocess.run(["/usr/libexec/PlistBuddy", "-c", "Delete :ITSAppUsesNonExemptEncryption", plist],
                       capture_output=True)
        run(["/usr/libexec/PlistBuddy", "-c", "Add :ITSAppUsesNonExemptEncryption bool false", plist])
    ws = xdir / "Unity-iPhone.xcworkspace"
    project = ["-workspace", str(ws)] if ws.exists() else \
              ["-project", str(xdir / "Unity-iPhone.xcodeproj")]
    # Signing + upload go through the Apple ID signed in to Xcode (Settings → Accounts,
    # Account Holder/Admin): an App Manager API key is refused cloud signing
    # ("Cloud signing permission error"). The API key is only used for ASC queries.
    auth = ["-allowProvisioningUpdates"]
    archive = art / "build.xcarchive"

    progress("Xcode archive")
    run(["xcodebuild", "archive", *project, "-scheme", "Unity-iPhone",
         "-configuration", "Release", "-destination", "generic/platform=iOS",
         "-archivePath", str(archive), *auth,
         f"DEVELOPMENT_TEAM={team}", "CODE_SIGN_STYLE=Automatic"])

    plist = art / "ExportOptions.plist"
    plist.write_text((SCRIPT_DIR / "ExportOptions.plist").read_text()
                     .replace("REPLACE_ME_TEAM_ID", team))
    progress("Uploading to TestFlight")
    run(["xcodebuild", "-exportArchive", "-archivePath", str(archive),
         "-exportOptionsPlist", str(plist), "-exportPath", str(art / "export"), *auth])

    shutil.rmtree(xdir, ignore_errors=True)   # multi-GB; the archive is on Apple now
    shutil.rmtree(archive, ignore_errors=True)

    what = f"{job['branch']}@{job['sha']}" + "".join(
        f" [{t.upper()}]" for t in ("dev", "cheats") if job.get(t))
    state = testflight_wait(app, n, what)
    return {"testflight": f"{version} ({n})", "state": state}


def _asc(method: str, path: str, app: dict, body: dict | None = None) -> dict:
    import ssl, certifi
    token = ship.generate_jwt(*asc_creds(app))
    req = urllib.request.Request(
        f"https://api.appstoreconnect.apple.com{path}", method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    ctx = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def testflight_wait(app: dict, n: int, what_to_test: str, timeout_s: int = 45 * 60) -> str:
    """Wait for Apple processing, then set 'What to Test'. Never fails the job: the
    upload already succeeded, this is only status."""
    progress("TestFlight processing (usually 5-20 min)")
    try:
        app_id = _asc("GET", f"/v1/apps?filter[bundleId]={app['bundle_id']}", app)["data"][0]["id"]
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            builds = _asc("GET", f"/v1/builds?filter[app]={app_id}&filter[version]={n}"
                                 "&fields[builds]=processingState", app)["data"]
            state = builds[0]["attributes"]["processingState"] if builds else "UPLOADING"
            if state in ("VALID", "INVALID", "FAILED"):
                if state == "VALID":
                    bid = builds[0]["id"]
                    loc = _asc("GET", f"/v1/builds/{bid}/betaBuildLocalizations", app)["data"]
                    if loc:  # Apple often creates the en-US entry itself — update it
                        _asc("PATCH", f"/v1/betaBuildLocalizations/{loc[0]['id']}", app, {"data": {
                            "type": "betaBuildLocalizations", "id": loc[0]["id"],
                            "attributes": {"whatsNew": what_to_test}}})
                    else:
                        _asc("POST", "/v1/betaBuildLocalizations", app, {"data": {
                            "type": "betaBuildLocalizations",
                            "attributes": {"locale": "en-US", "whatsNew": what_to_test},
                            "relationships": {"build": {"data": {"type": "builds", "id": bid}}}}})
                return state
            time.sleep(60)
        return "PROCESSING (still going — check App Store Connect)"
    except (urllib.error.URLError, KeyError, IndexError) as e:
        log(f"TestFlight status check failed: {e}", "WARN")
        return "UPLOADED (status unknown)"


# ── Google Drive (Shared Drive, via the Play service account) ────────────────

def _drive(method: str, url: str, token: str, body: bytes | None = None,
           ctype: str | None = "application/json", headers: dict | None = None,
           raw: bool = False):
    import ssl, certifi
    h = {"Authorization": f"Bearer {token}", **(headers or {})}
    if ctype:
        h["Content-Type"] = ctype
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        r = urllib.request.urlopen(req, timeout=1800, context=ctx)
        if raw:
            return r
        data = r.read()
        return json.loads(data) if data else {}
    except urllib.error.HTTPError as e:
        die(f"Drive API {e.code} on {method} {url.split('?')[0]}: "
            f"{e.read().decode(errors='replace')[:500]}",
            hint="Check: Drive API enabled in the service account's GCP project; service "
                 "account is Content Manager on the Shared Drive; DRIVE_SHARED_DRIVE_ID is right; "
                 "Workspace allows sharing Shared Drive files outside the org.",
            category="DRIVE FAILED")


def _drive_q(params: dict) -> str:
    drive_id = cfg.get("DRIVE_SHARED_DRIVE_ID")
    if not drive_id:
        die("DRIVE_SHARED_DRIVE_ID not set in config.env", category="BAD CONFIG")
    return urllib.parse.urlencode({"corpora": "drive", "driveId": drive_id,
                                   "includeItemsFromAllDrives": "true",
                                   "supportsAllDrives": "true", **params})


def drive_upload(path: Path, folder_name: str, name: str) -> str:
    token = sa.play_token(cfg["PLAY_SERVICE_ACCOUNT"], DRIVE_SCOPE)
    drive_id = cfg.get("DRIVE_SHARED_DRIVE_ID")
    q = (f"name = '{folder_name}' and '{drive_id}' in parents and trashed = false "
         "and mimeType = 'application/vnd.google-apps.folder'")
    found = _drive("GET", f"{DRIVE_API}/files?" + _drive_q({"q": q, "fields": "files(id)"}),
                   token, ctype=None)["files"]
    folder = found[0]["id"] if found else _drive(
        "POST", f"{DRIVE_API}/files?supportsAllDrives=true", token,
        json.dumps({"name": folder_name, "parents": [drive_id],
                    "mimeType": "application/vnd.google-apps.folder"}).encode())["id"]

    size = path.stat().st_size
    session = _drive("POST", f"{DRIVE_UPLOAD_API}/files?uploadType=resumable&supportsAllDrives=true",
                     token, json.dumps({"name": name, "parents": [folder]}).encode(),
                     "application/json; charset=UTF-8",
                     {"X-Upload-Content-Length": str(size)}, raw=True).headers["Location"]
    with open(path, "rb") as fh:
        f = _drive("PUT", session, token, fh,
                   "application/octet-stream", {"Content-Length": str(size)})
    # DRIVE_SHARING: anyone (link works for anyone, e.g. testers' personal accounts),
    # domain (only DRIVE_DOMAIN accounts) or none (Shared Drive members only).
    sharing = cfg.get("DRIVE_SHARING", "anyone")
    if sharing in ("anyone", "domain"):
        perm = {"type": sharing, "role": "reader"}
        if sharing == "domain":
            perm["domain"] = cfg.get("DRIVE_DOMAIN") or die(
                "DRIVE_SHARING=domain needs DRIVE_DOMAIN", category="BAD CONFIG")
        _drive("POST", f"{DRIVE_API}/files/{f['id']}/permissions?supportsAllDrives=true", token,
               json.dumps(perm).encode())
    elif sharing != "none":
        die(f"DRIVE_SHARING must be anyone, domain or none (got '{sharing}')", category="BAD CONFIG")
    link = f"https://drive.google.com/file/d/{f['id']}/view"
    log(f"Drive: {link}")
    return link


def prune(job: dict) -> dict:
    """Trash builds older than RETENTION_DAYS. Only files inside the per-game folders that
    drive_upload() creates (named after projects.json keys, at the Shared Drive root) are
    touched — anything else on the drive is left alone. {"dry_run": true} only lists;
    {"days": N} overrides the age cutoff."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=job.get("days", RETENTION_DAYS))
    token = sa.play_token(cfg["PLAY_SERVICE_ACCOUNT"], DRIVE_SCOPE)
    drive_id = cfg.get("DRIVE_SHARED_DRIVE_ID")
    names = sa.load_projects()
    fq = (f"'{drive_id}' in parents and trashed = false "
          "and mimeType = 'application/vnd.google-apps.folder'")
    folders = [f["id"] for f in _drive("GET", f"{DRIVE_API}/files?" + _drive_q(
        {"q": fq, "fields": "files(id,name)", "pageSize": "1000"}), token, ctype=None)["files"]
        if f["name"] in names]
    deleted, page = [], None
    if not folders:
        log("Pruned 0 Drive file(s): no game folders found")
        return {"deleted": deleted}
    # ponytail: one OR query — split into chunks if a studio ever has hundreds of games
    q = (f"createdTime < '{cutoff.strftime('%Y-%m-%dT%H:%M:%S')}' and trashed = false "
         "and mimeType != 'application/vnd.google-apps.folder' and ("
         + " or ".join(f"'{f}' in parents" for f in folders) + ")")
    while True:
        params = {"q": q, "fields": "nextPageToken,files(id,name)", "pageSize": "1000"}
        if page:
            params["pageToken"] = page
        resp = _drive("GET", f"{DRIVE_API}/files?" + _drive_q(params), token, ctype=None)
        for f in resp.get("files", []):
            deleted.append(f["name"])
            if job.get("dry_run"):
                continue
            # Content managers may only trash; Shared Drive trash empties itself after 30 days.
            _drive("PATCH", f"{DRIVE_API}/files/{f['id']}?supportsAllDrives=true", token,
                   json.dumps({"trashed": True}).encode())
        page = resp.get("nextPageToken")
        if not page:
            break
    if job.get("dry_run"):
        log(f"Would prune {len(deleted)} Drive file(s): {deleted}")
        return {"deleted": deleted, "dry_run": True}
    for d in (WORK / "artifacts").glob("*"):
        if d.stat().st_mtime < cutoff.timestamp():
            shutil.rmtree(d, ignore_errors=True)
    log(f"Pruned {len(deleted)} Drive file(s): {deleted}")
    return {"deleted": deleted}


# ── Jobs ──────────────────────────────────────────────────────────────────────

def resolve(job: dict) -> dict:
    projects = sa.load_projects()
    if job["game"] not in projects:
        die(f"Unknown game '{job['game']}'", hint="Known: " + ", ".join(sorted(projects)),
            category="BAD ARG")
    app = {**projects[job["game"]], "key": job["game"]}
    app["repo_path"] = str(Path(app["repo_path"]).expanduser())
    return app


def build(job: dict) -> dict:
    app = resolve(job)
    platforms = ["android", "ios"] if job["platform"] == "both" else [job["platform"]]
    unsupported = set(platforms) - set(app.get("platforms", ["android", "ios"]))
    if unsupported:
        die(f"[{app['key']}] not configured for {', '.join(unsupported)}", category="BAD ARG")

    # One build per clone: a manual CLI run and the bot must not share a work dir.
    # The lock is released when this process exits.
    (WORK / "work").mkdir(parents=True, exist_ok=True)
    lock = open(WORK / "work" / f"{app['key']}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        progress(f"Waiting for another {app['key']} build to finish")
        fcntl.flock(lock, fcntl.LOCK_EX)

    if job.get("clean"):
        progress("Deleting Library cache (clean build — full re-import)")
        shutil.rmtree(WORK / "work" / app["key"] / "Library", ignore_errors=True)
    clone, sha = prepare_clone(app, job["branch"])
    job["sha"] = sha
    changes = changelog(clone, job.get("since"))
    version = sa.read_android_settings(str(clone))[0]
    n = next_build_number(app, clone, platforms)
    progress(f"{app['key']} {version} ({n}) from {job['branch']}@{sha}")

    art = WORK / "artifacts" / job["id"]
    art.mkdir(parents=True, exist_ok=True)
    out = {"version": version, "number": n, "sha": sha, "changes": changes}
    for p in platforms:
        out[p] = (build_android if p == "android" else build_ios)(job, app, clone, version, n, art)
    return out


def changelog(clone: Path, since: str | None, limit: int = 20) -> list[str]:
    """Commit subjects since the previous build of this game+branch (newest first)."""
    if not since:
        return []
    res = subprocess.run(["git", "-C", str(clone), "log", "--no-merges", f"-{limit}",
                          "--format=%s (%an)", f"{since}..HEAD"],
                         capture_output=True, text=True, env=_ENV)
    return res.stdout.splitlines() if res.returncode == 0 else []


def upload(job: dict) -> dict:
    """Push the AAB kept from an earlier build to Play internal (draft)."""
    app = resolve(job)
    aabs = sorted((WORK / "artifacts" / job["id"]).glob("*.aab"))
    if not aabs:
        die("AAB for this build is gone (older than 30 days, or the build failed)",
            hint="Run the build again.", category="UPLOAD FAILED")
    app["new_version"] = job["version"]
    token = sa.play_token(cfg["PLAY_SERVICE_ACCOUNT"])
    edit = sa.play_edit_insert(app["package_name"], token)
    highest = sa.play_highest_version_code(app["package_name"], token, edit)
    sa.play_edit_delete(app["package_name"], token, edit)
    if highest >= job["number"]:
        die(f"Play already has versionCode {highest} ≥ {job['number']}",
            hint="A newer build went to Play since. Run a new build and upload that one.",
            category="UPLOAD FAILED")
    progress(f"Uploading {aabs[-1].name} to Play internal")
    sa.play_upload(app, aabs[-1], cfg, "internal")
    return {"track": "internal"}


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    job = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    handler = {"build": build, "upload": upload, "prune": prune}.get(cmd)
    if not handler:
        sys.exit(__doc__)

    (WORK / "logs").mkdir(parents=True, exist_ok=True)
    ship.LOG_FILE = WORK / "logs" / f"{job.get('id', cmd)}.log"
    ship.ERROR_JSON = WORK / "logs" / f"{job.get('id', cmd)}.error.json"
    ship.ERROR_FILE = ship.ERROR_JSON.with_suffix(".txt")
    ship.ERROR_JSON.unlink(missing_ok=True)
    try:
        result({"ok": True, **handler(job)})
    except SystemExit as e:
        if e.code in (0, None):
            raise
        err = (json.loads(ship.ERROR_JSON.read_text()) if ship.ERROR_JSON.exists()
               else {"category": "FAILED", "message": str(e.code), "hint": ""})
        result({"ok": False, **err})
        sys.exit(1)
    except Exception as e:
        import traceback
        traceback.print_exc()
        result({"ok": False, "category": "SCRIPT ERROR", "message": f"{type(e).__name__}: {e}",
                "hint": traceback.format_exc()[-800:]})
        sys.exit(1)


if __name__ == "__main__":
    main()
