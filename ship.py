#!/usr/bin/env python3
"""
ship.py — Unity iOS build + TestFlight upload pipeline (project-agnostic)

Usage:
    python3 ship.py <branch> [options]

Steps:
    1. Pre-flight: validate branch exists on origin
    2. git checkout <branch>
    3. Query App Store Connect for latest TestFlight build number
    4. Bump bundleVersion + iOS buildNumber in ProjectSettings.asset
    5. Optionally patch arbitrary asset YAML fields (--asset-patch)
    6. Optionally mutate iOS scripting defines (--define)
    7. Run Unity in batch mode → generate Xcode project
    8. xcodebuild archive + export/upload to TestFlight

Config: ./config.env  (env vars override)
Logs:   ./last-build.log
"""

import sys
import os
import re
import json
import time
import shutil
import signal
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.env"
LOG_FILE = SCRIPT_DIR / "last-build.log"
ERROR_FILE = SCRIPT_DIR / "last-error.txt"
ERROR_JSON = SCRIPT_DIR / "last-error.json"
EXPORT_PLIST = SCRIPT_DIR / "ExportOptions.plist"

# ── Required config keys ──────────────────────────────────────────────────────
# All other keys have defaults below or are optional.

REQUIRED_KEYS = [
    "UNITY_PATH",       # e.g. /Applications/Unity/Hub/Editor/6000.0.60f1/Unity.app/Contents/MacOS/Unity
    "REPO_PATH",        # e.g. /Users/me/MyUnityProject
    "BUNDLE_ID",        # e.g. com.mycompany.myapp
    "TEAM_ID",          # Apple Developer Team ID (10-char)
    "ASC_KEY_ID",       # App Store Connect API key ID
    "ASC_ISSUER_ID",    # ASC issuer ID (UUID)
    "ASC_KEY_PATH",     # path to AuthKey_<KEY_ID>.p8
]

DEFAULTS = {
    "BUILD_OUTPUT":   "/tmp/ios-build",
    "ARCHIVE_PATH":   "/tmp/build.xcarchive",
    "EXPORT_PATH":    "/tmp/build-export",
    "BUILD_METHOD":   "BuildScript.BuildiOS",   # Unity static method to invoke
}

# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists() and CONFIG_FILE.stat().st_mode & 0o077:
        print(f"⚠️  {CONFIG_FILE} is readable by other users — run: chmod 600 {CONFIG_FILE}",
              file=sys.stderr)
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in list(cfg) + REQUIRED_KEYS:
        if k in os.environ:
            cfg[k] = os.environ[k]
    return cfg

# ── Logging ───────────────────────────────────────────────────────────────────

_log_fh = None

_CREDS = re.compile(r"(\w+://)[^/@\s]+@")


def redact(text: str) -> str:
    """Strip user:token@ from URLs (git remotes with an embedded PAT) — logs get posted to Discord."""
    return _CREDS.sub(r"\1***@", text)


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] [{level}] {redact(str(msg))}"
    print(line, flush=True)
    if _log_fh:
        print(line, file=_log_fh, flush=True)

def die(msg: str, hint: str | None = None, category: str = "FAILED") -> None:
    """Log error block + write summary files for wrappers + exit."""
    msg, hint = redact(msg), hint and redact(hint)
    log(msg, "ERROR")
    bar = "═" * 60
    print(bar, flush=True)
    print(f"❌ {category}: {msg}", flush=True)
    if hint:
        print("", flush=True)
        for line in hint.rstrip().splitlines():
            print(f"   {line}", flush=True)
    print("", flush=True)
    print(f"📄 Full log: {LOG_FILE}", flush=True)
    print(bar, flush=True)
    try:
        ERROR_FILE.write_text(
            f"❌ {category}: {msg}\n"
            + (f"\n{hint}\n" if hint else "")
            + f"\nLog: {LOG_FILE}\n"
        )
        ERROR_JSON.write_text(json.dumps({
            "category": category,
            "message": msg,
            "hint": hint or "",
            "log": str(LOG_FILE),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, indent=2))
    except Exception:
        pass
    sys.exit(1)

def section(title: str) -> None:
    bar = "─" * 60
    log(bar)
    log(f"  {title}")
    log(bar)

_ENV = {**os.environ, "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + os.environ.get("PATH", "")}

def run(cmd: list, cwd: str = None, check: bool = True) -> subprocess.CompletedProcess:
    log("$ " + " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=_ENV)
    if result.stdout:
        for line in result.stdout.rstrip().splitlines():
            log(line)
    if check and result.returncode != 0:
        stderr_tail = []
        if result.stderr:
            for line in result.stderr.rstrip().splitlines():
                log(line, "ERROR")
                stderr_tail.append(line)
        tool = Path(str(cmd[0])).name
        category = {"xcodebuild": "XCODE FAILED", "git": "GIT FAILED"}.get(tool, "COMMAND FAILED")
        hint = f"Command: {' '.join(str(c) for c in cmd)}\nExit: {result.returncode}"
        if stderr_tail:
            hint += "\n\nstderr tail:\n" + "\n".join(stderr_tail[-10:])
        die(f"{tool} failed (exit {result.returncode})", hint=hint, category=category)
    return result

# ── JWT (App Store Connect ES256) ─────────────────────────────────────────────

def _ensure_packages() -> None:
    missing = []
    try: import jwt  # noqa
    except ImportError: missing.append("PyJWT")
    try: from cryptography.hazmat.primitives.serialization import load_pem_private_key  # noqa
    except ImportError: missing.append("cryptography")
    try: import certifi  # noqa
    except ImportError: missing.append("certifi")
    if missing:
        log(f"Installing {' '.join(missing)} (one-time)...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet"] + missing)

def generate_jwt(key_id: str, issuer_id: str, key_path: str) -> str:
    _ensure_packages()
    import jwt
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    with open(key_path, "rb") as f:
        private_key = load_pem_private_key(f.read(), password=None)
    now = int(time.time())
    token = jwt.encode(
        {"iss": issuer_id, "iat": now, "exp": now + 1200, "aud": "appstoreconnect-v1"},
        private_key, algorithm="ES256", headers={"kid": key_id},
    )
    return token if isinstance(token, str) else token.decode()

# ── App Store Connect API ─────────────────────────────────────────────────────

def asc_get(path: str, token: str) -> dict:
    import ssl, certifi
    url = f"https://api.appstoreconnect.apple.com{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        die(f"ASC API {e.code} at {path}: {body}", category="ASC FAILED")

def get_latest_tf_build(bundle_id: str, key_id: str, issuer_id: str, key_path: str) -> tuple[int | None, str | None]:
    log(f"Querying TestFlight for latest build of {bundle_id}...")
    token = generate_jwt(key_id, issuer_id, key_path)
    apps = asc_get(f"/v1/apps?filter[bundleId]={bundle_id}&fields[apps]=bundleId,name", token)
    if not apps.get("data"):
        die(f"No app found with bundle ID {bundle_id}",
            hint="Check API key permissions in ASC → Users & Access → Keys.",
            category="ASC FAILED")
    app_id = apps["data"][0]["id"]
    app_name = apps["data"][0]["attributes"].get("name", "?")
    log(f"Found app: {app_name} (id={app_id})")
    token = generate_jwt(key_id, issuer_id, key_path)
    builds = asc_get(
        f"/v1/builds?filter[app]={app_id}&sort=-version&limit=1"
        "&fields[builds]=version,uploadedDate,processingState"
        "&include=preReleaseVersion&fields[preReleaseVersions]=version",
        token,
    )
    if not builds.get("data"):
        log("No builds found on TestFlight — using current Unity build number as base.")
        return None, None
    attrs = builds["data"][0]["attributes"]
    build_num = int(attrs["version"])
    marketing_version = None
    for inc in builds.get("included", []):
        if inc.get("type") == "preReleaseVersions":
            marketing_version = inc["attributes"].get("version")
            break
    log(f"Latest TF build: {build_num}  version={marketing_version}  "
        f"state={attrs.get('processingState','?')}")
    return build_num, marketing_version

# ── ProjectSettings.asset manipulation ───────────────────────────────────────

def read_project_settings(repo_path: str) -> tuple[str, int]:
    path = Path(repo_path) / "ProjectSettings" / "ProjectSettings.asset"
    content = path.read_text()
    ver_match = re.search(r"bundleVersion:\s*(\S+)", content)
    current_version = ver_match.group(1) if ver_match else "1.0"
    current_build = 0
    in_block = False
    for line in content.splitlines():
        if re.match(r"\s*buildNumber:", line):
            in_block = True; continue
        if in_block:
            m = re.match(r"\s+iPhone:\s+(\d+)", line)
            if m:
                current_build = int(m.group(1)); break
            if line and not line[0].isspace():
                break
    log(f"Current ProjectSettings: version={current_version}, build={current_build}")
    return current_version, current_build

def bump_version(version: str) -> str:
    parts = version.split(".")
    try: parts[-1] = str(int(parts[-1]) + 1)
    except ValueError: parts.append("1")
    return ".".join(parts)

def version_tuple(v: str) -> tuple:
    try: return tuple(int(x) for x in v.split("."))
    except ValueError: return (0,)

def max_version(a: str | None, b: str | None) -> str | None:
    if a is None: return b
    if b is None: return a
    return a if version_tuple(a) >= version_tuple(b) else b

def write_project_settings(repo_path: str, new_version: str, new_build: int) -> None:
    path = Path(repo_path) / "ProjectSettings" / "ProjectSettings.asset"
    content = path.read_text()
    content = re.sub(r"(bundleVersion:\s*)\S+", f"\\g<1>{new_version}", content)
    lines = content.splitlines(keepends=True)
    in_block = False
    result = []
    for line in lines:
        if re.match(r"\s*buildNumber:", line):
            in_block = True; result.append(line); continue
        if in_block:
            m = re.match(r"(\s+iPhone:\s+)\d+", line)
            if m:
                line = f"{m.group(1)}{new_build}\n"
                in_block = False
            elif line.strip() and not line[0].isspace():
                in_block = False
        result.append(line)
    path.write_text("".join(result))
    log(f"ProjectSettings updated → version={new_version}, build={new_build}")

# ── Generic asset YAML patch ─────────────────────────────────────────────────

def parse_asset_patches(specs: list[str]) -> list[tuple[str, str, str]]:
    """Parse 'Assets/Path.asset:_field:value' triples. Value stays as string for YAML write."""
    out = []
    for s in specs:
        parts = s.split(":", 2)
        if len(parts) != 3:
            die(f"Invalid --asset-patch spec: {s}",
                hint='Format: "Assets/Path.asset:_fieldName:value"  (3 colon-separated parts)',
                category="BAD ARG")
        out.append((parts[0].strip(), parts[1].strip(), parts[2].strip()))
    return out

def apply_asset_patches(repo_path: str, patches: list[tuple[str, str, str]]) -> list[str]:
    touched = []
    for rel, field, val in patches:
        path = Path(repo_path) / rel
        if not path.exists():
            die(f"Asset not found: {rel}", category="ASSET PATCH FAILED")
        text = path.read_text()
        # Match either int/string value lines (greedy non-newline)
        pattern = rf"(^\s+{re.escape(field)}:\s*)([^\n]*)$"
        new_text, n = re.subn(pattern, rf"\g<1>{val}", text, count=1, flags=re.MULTILINE)
        if n == 0:
            die(f"Field '{field}' not found in {rel}",
                hint="Asset format may have changed. Inspect file.",
                category="ASSET PATCH FAILED")
        path.write_text(new_text)
        log(f"Patched {rel}: {field}={val}")
        touched.append(rel)
    return touched

def restore_assets(repo_path: str, asset_paths: list[str]) -> None:
    if not asset_paths: return
    log(f"Restoring asset patches ({len(asset_paths)} files)...")
    subprocess.run(
        ["git", "-C", repo_path, "checkout", "--"] + asset_paths,
        capture_output=True, text=True, env=_ENV,
    )

# ── Git ───────────────────────────────────────────────────────────────────────

def validate_branch(repo_path: str, branch: str) -> None:
    log(f"Validating branch '{branch}' on origin...")
    # Rejects option-looking names ("-upload-pack=…") and refs git itself would refuse.
    if subprocess.run(["git", "check-ref-format", "--branch", branch],
                      capture_output=True, env=_ENV).returncode != 0:
        die(f"Invalid branch name: {branch!r}", category="BAD BRANCH")
    run(["git", "-C", repo_path, "fetch", "origin", "--prune"])
    res = subprocess.run(
        ["git", "-C", repo_path, "ls-remote", "--heads", "origin", branch],
        capture_output=True, text=True, env=_ENV,
    )
    if res.returncode != 0 or not res.stdout.strip():
        refs = subprocess.run(
            ["git", "-C", repo_path, "for-each-ref", "--format=%(refname:short)",
             "refs/remotes/origin/"],
            capture_output=True, text=True, env=_ENV,
        ).stdout.splitlines()
        names = [r.replace("origin/", "", 1) for r in refs if r != "origin/HEAD"]
        lower = branch.lower()
        suggestions = sorted({n for n in names if lower in n.lower() or n.lower().startswith(lower[:3])})[:5]
        hint = f"Branch '{branch}' does not exist on origin."
        if suggestions:
            hint += "\nDid you mean:\n  " + "\n  ".join(suggestions)
        else:
            hint += f"\nRun: git -C {repo_path} branch -r"
        die(f"Unknown branch: {branch}", hint=hint, category="BAD BRANCH")

def checkout_branch(repo_path: str, branch: str) -> None:
    log(f"Checking out branch: {branch}")
    run(["git", "-C", repo_path, "fetch", "--all"])
    run(["git", "-C", repo_path, "worktree", "prune"])
    status = subprocess.run(
        ["git", "-C", repo_path, "status", "--porcelain"],
        capture_output=True, text=True, env=_ENV,
    )
    has_changes = bool(status.stdout.strip())
    if has_changes:
        run(["git", "-C", repo_path, "stash", "push", "-u", "-m", "ship.py auto-stash"])
    run(["git", "-C", repo_path, "checkout", "-B", branch, f"origin/{branch}"])
    if has_changes:
        result = subprocess.run(
            ["git", "-C", repo_path, "stash", "pop"],
            capture_output=True, text=True, env=_ENV,
        )
        if result.stdout: log(result.stdout.rstrip())
        if result.returncode != 0:
            log("stash pop conflict — restoring branch ProjectSettings.asset", "WARN")
            run(["git", "-C", repo_path, "checkout", "--",
                 "ProjectSettings/ProjectSettings.asset"], check=False)
            run(["git", "-C", repo_path, "stash", "drop"], check=False)

# ── Unity build ───────────────────────────────────────────────────────────────

def build_unity(cfg: dict, new_version: str, new_build: int, dev_mode: bool = False,
                define_add: str | None = None, define_remove: str | None = None) -> None:
    unity = cfg["UNITY_PATH"]
    repo_path = cfg["REPO_PATH"]
    output = cfg["BUILD_OUTPUT"]
    method = cfg["BUILD_METHOD"]
    unity_log = SCRIPT_DIR / "unity-build.log"

    if Path(output).exists():
        shutil.rmtree(output)
    Path(output).mkdir(parents=True, exist_ok=True)

    log(f"Unity log → {unity_log}")
    log(f"Starting Unity batch build (version={new_version} build={new_build})...")

    cmd = [
        unity, "-batchmode", "-quit",
        "-projectPath", repo_path,
        "-buildTarget", "iOS",
        "-executeMethod", method,
        "-logFile", str(unity_log),
        "-buildOutput", output,
        "-buildVersion", new_version,
        "-buildNumber", str(new_build),
    ]
    if dev_mode:
        cmd += ["-developmentBuild", "true"]
    if define_add:
        cmd += ["-defineAdd", define_add]
    if define_remove:
        cmd += ["-defineRemove", define_remove]

    result = subprocess.run(cmd)

    if result.returncode != 0:
        tail_lines = []
        try:
            all_lines = unity_log.read_text().splitlines()
            err_lines = [l for l in all_lines
                         if any(tok in l for tok in ("error CS", "Exception:", "BuildFailedException",
                                                     "Assertion failed", "ERROR:", "[BuildScript] FAILED"))]
            tail_lines = (err_lines[-15:] if err_lines else all_lines[-30:])
            log("=== Unity log (relevant tail) ===", "ERROR")
            for l in tail_lines: print(l)
        except Exception as e:
            log(f"Could not read Unity log: {e}", "ERROR")
        hint = f"Unity exited {result.returncode}.\nFull log: {unity_log}"
        if tail_lines:
            hint += "\n\nLast errors:\n" + "\n".join(tail_lines[-10:])
        die(f"Unity build failed (exit {result.returncode})", hint=hint, category="UNITY FAILED")

    log("Unity build complete.")

# ── Xcode archive + TestFlight upload ────────────────────────────────────────

def _ensure_key_in_search_path(key_id: str, key_path: str) -> None:
    key_dir = Path.home() / ".appstoreconnect" / "private_keys"
    key_dir.mkdir(parents=True, exist_ok=True)
    dest = key_dir / f"AuthKey_{key_id}.p8"
    if not dest.exists():
        shutil.copy2(key_path, dest)
        dest.chmod(0o600)
        log(f"Copied API key → {dest}")

def _find_xcode_output(build_output: str, new_version: str, new_build: int) -> str:
    predicted = f"{build_output} v{new_version}({new_build})"
    for candidate in [predicted, build_output]:
        if Path(candidate, "Unity-iPhone.xcodeproj").exists():
            return candidate
    parent = Path(build_output).parent
    stem = Path(build_output).name
    matches = sorted(
        [d for d in parent.iterdir()
         if d.is_dir() and d.name.startswith(stem) and (d / "Unity-iPhone.xcodeproj").exists()],
        key=lambda d: d.stat().st_mtime, reverse=True,
    )
    if matches:
        log(f"Found Xcode project via scan: {matches[0]}")
        return str(matches[0])
    return build_output

def archive_and_upload(cfg: dict, new_version: str, new_build: int) -> None:
    build_output = cfg["BUILD_OUTPUT"]
    archive_path = cfg["ARCHIVE_PATH"]
    export_path = cfg["EXPORT_PATH"]
    team_id = cfg["TEAM_ID"]

    actual_output = _find_xcode_output(build_output, new_version, new_build)
    log(f"Xcode project base: {actual_output}")
    xcodeproj = str(Path(actual_output) / "Unity-iPhone.xcodeproj")
    if not Path(xcodeproj).exists():
        die(f"Xcode project not found: {xcodeproj}",
            hint="Unity build may have put output elsewhere. Check unity-build.log.",
            category="XCODE FAILED")

    _ensure_key_in_search_path(cfg["ASC_KEY_ID"], cfg["ASC_KEY_PATH"])

    for p in [archive_path, export_path]:
        if Path(p).exists():
            shutil.rmtree(p)

    xcworkspace = str(Path(actual_output) / "Unity-iPhone.xcworkspace")
    use_workspace = Path(xcworkspace).exists()
    project_args = ["-workspace", xcworkspace] if use_workspace else ["-project", xcodeproj]
    log(f"Building via {'workspace (CocoaPods)' if use_workspace else 'project'}")

    section("Xcode Archive")
    run([
        "xcodebuild", "archive", *project_args,
        "-scheme", "Unity-iPhone",
        "-configuration", "Release",
        "-destination", "generic/platform=iOS",
        "-archivePath", archive_path,
        "-allowProvisioningUpdates",
        f"DEVELOPMENT_TEAM={team_id}",
        "CODE_SIGN_STYLE=Automatic",
    ])

    section("Export + Upload to TestFlight")
    run([
        "xcodebuild", "-exportArchive",
        "-archivePath", archive_path,
        "-exportOptionsPlist", str(EXPORT_PLIST),
        "-exportPath", export_path,
        "-allowProvisioningUpdates",
    ])
    log("Upload complete. Check App Store Connect → TestFlight → Internal Testing.")

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    global _log_fh

    def _on_signal(signum, _frame):
        name = signal.Signals(signum).name
        die(f"Process received {name} (signal {signum})",
            hint=("Build was killed externally. Common causes:\n"
                  "  • User Ctrl-C\n"
                  "  • OS killed process (out of memory, timeout)\n"
                  "  • Wrapper/CI cancelled the run"),
            category="INTERRUPTED")
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    parser = argparse.ArgumentParser(
        description="Unity iOS → TestFlight pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  ship.py main\n"
            "  ship.py main --dev\n"
            "  ship.py main --version 4.0 --build 500\n"
            "  ship.py main --no-upload\n"
            '  ship.py main --define "+DEBUG_LOG,-MY_FLAG"\n'
            '  ship.py main --asset-patch "Assets/Settings/Env.asset:_environment:1"\n'
            f"\nLogs: {LOG_FILE}\n"
        ),
    )
    parser.add_argument("branch", help="Git branch to build (must exist on origin)")
    parser.add_argument("--dev", action="store_true",
                        help="Development build. No upload unless --upload given.")
    parser.add_argument("--release", action="store_true",
                        help="Force release (default). Errors with --dev.")
    parser.add_argument("--version", dest="version_override", metavar="X.Y",
                        help="Override marketing version. Skips ASC version bump.")
    parser.add_argument("--build", dest="build_override", type=int, metavar="N",
                        help="Override iOS build number. Skips ASC build bump.")
    parser.add_argument("--define", dest="defines", action="append", default=[],
                        help="Add/remove scripting defines: --define +FOO,-BAR  (repeatable).")
    parser.add_argument("--asset-patch", dest="asset_patches", action="append", default=[],
                        metavar="PATH:FIELD:VALUE",
                        help='Patch a YAML field in an asset, e.g. '
                             '"Assets/Settings/Env.asset:_environment:1". Repeatable. '
                             "Asset is restored via git checkout after build.")
    parser.add_argument("--no-upload", action="store_true",
                        help="Build only, skip TestFlight upload.")
    parser.add_argument("--upload", action="store_true",
                        help="Force upload even on --dev builds.")
    args = parser.parse_args()

    if args.dev and args.release:
        die("--dev and --release are mutually exclusive.")
    if args.upload and args.no_upload:
        die("--upload and --no-upload are mutually exclusive.")

    branch = args.branch
    dev_mode = args.dev
    skip_upload = args.no_upload or (dev_mode and not args.upload)

    add_set, remove_set = [], []
    for tok in (t.strip() for entry in args.defines for t in entry.split(",")):
        if not tok: continue
        if tok.startswith("+"):   add_set.append(tok[1:])
        elif tok.startswith("-"): remove_set.append(tok[1:])
        else: die(f"--define token must start with + or -: {tok}", category="BAD ARG")
    define_add = ",".join(add_set) if add_set else None
    define_remove = ",".join(remove_set) if remove_set else None

    asset_patches = parse_asset_patches(args.asset_patches)

    _log_fh = open(LOG_FILE, "w")
    for f in (ERROR_FILE, ERROR_JSON):
        try: f.unlink()
        except FileNotFoundError: pass

    patched_assets = []
    cfg = None
    try:
        cfg = load_config()
        for key in REQUIRED_KEYS:
            if key not in cfg:
                die(f"Missing required config key: {key}",
                    hint=f"Add to {CONFIG_FILE}. See config.env.example.",
                    category="MISSING CONFIG")

        for label, path in [
            ("ASC key", cfg["ASC_KEY_PATH"]),
            ("Unity binary", cfg["UNITY_PATH"]),
            ("Repo", cfg["REPO_PATH"]),
            ("ExportOptions.plist", str(EXPORT_PLIST)),
        ]:
            if not Path(path).exists():
                die(f"{label} not found: {path}",
                    hint=f"Check {CONFIG_FILE} — '{label}' path is wrong.",
                    category="MISSING DEPENDENCY")

        # Guard: same project open in Editor → batch build will deadlock.
        lockfile = Path(cfg["REPO_PATH"]) / "Temp" / "UnityLockfile"
        if lockfile.exists():
            held = subprocess.run(["lsof", str(lockfile)], capture_output=True, text=True)
            if held.returncode == 0 and held.stdout.strip():
                die("Unity Editor has this project open",
                    hint=f"Close project in Editor (or quit Unity).\nLockfile: {lockfile}",
                    category="UNITY EDITOR OPEN")

        section(f"SHIP  branch={branch}  dev={dev_mode}  upload={not skip_upload}")
        start = time.time()

        section("1/5  Git checkout")
        validate_branch(cfg["REPO_PATH"], branch)
        checkout_branch(cfg["REPO_PATH"], branch)
        patched_assets = apply_asset_patches(cfg["REPO_PATH"], asset_patches)

        both_overridden = args.version_override and args.build_override is not None
        if both_overridden:
            section("2/5  Skip TestFlight query (overrides supplied)")
            latest_build, latest_version = None, None
        else:
            section("2/5  Query TestFlight")
            latest_build, latest_version = get_latest_tf_build(
                cfg["BUNDLE_ID"], cfg["ASC_KEY_ID"], cfg["ASC_ISSUER_ID"], cfg["ASC_KEY_PATH"]
            )

        section("3/5  Resolve versions")
        current_version, current_build = read_project_settings(cfg["REPO_PATH"])

        if args.version_override:
            new_version = args.version_override
            log(f"Version override → {new_version}")
        else:
            base_version = max_version(latest_version, current_version) or current_version
            new_version = bump_version(base_version)
            log(f"Base: ASC={latest_version} PS={current_version} → bumped to {new_version}")

        if args.build_override is not None:
            new_build = args.build_override
            log(f"Build override → {new_build}")
        else:
            new_build = max(latest_build or 0, current_build) + 1

        log(f"New → version={new_version}  build={new_build}")
        write_project_settings(cfg["REPO_PATH"], new_version, new_build)

        section("4/5  Unity build")
        build_unity(cfg, new_version, new_build, dev_mode, define_add, define_remove)

        actual_build = new_build
        actual_version = new_version
        try:
            folder = _find_xcode_output(cfg["BUILD_OUTPUT"], new_version, new_build)
            m = re.search(r"v([\d.]+)\((\d+)\)$", Path(folder).name)
            if m:
                actual_version, actual_build = m.group(1), int(m.group(2))
        except Exception:
            pass

        if skip_upload:
            reason = "dev build" if dev_mode else "--no-upload"
            section(f"5/5  SKIPPED upload ({reason})")
            log(f"Xcode project at: {cfg['BUILD_OUTPUT']}")
        else:
            section("5/5  Xcode archive + TestFlight upload")
            archive_and_upload(cfg, actual_version, actual_build)

        elapsed = int(time.time() - start)
        section("DONE")
        tags = []
        if dev_mode: tags.append("DEV")
        if skip_upload and not dev_mode: tags.append("NO-UPLOAD")
        mode_tag = f" [{','.join(tags)}]" if tags else ""
        log(f"Done{mode_tag}  version={actual_version} build={actual_build} "
            f"branch={branch}  {elapsed//60}m {elapsed%60}s")

    except SystemExit:
        raise
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log(tb, "ERROR")
        die(f"Unhandled {type(e).__name__}: {e}",
            hint="Last 5 traceback lines:\n" + "\n".join(tb.rstrip().splitlines()[-5:]),
            category="SCRIPT ERROR")
    finally:
        try:
            if cfg and patched_assets:
                restore_assets(cfg["REPO_PATH"], patched_assets)
        except Exception as e:
            log(f"Asset restore failed: {e}", "WARN")
        if _log_fh:
            _log_fh.close()


if __name__ == "__main__":
    main()
