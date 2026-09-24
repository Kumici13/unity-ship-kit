#!/usr/bin/env bash
# setup.sh — first-time setup and health check. Safe to re-run any time.
#
#   ./setup.sh            # install deps, create config files, report what's missing
#   ./setup.sh --install  # same, then install + start the launchd agent (bot runs on login)
set -uo pipefail
cd "$(dirname "$0")"
KIT=$PWD
fail=0
ok()   { echo "  ✅ $*"; }
bad()  { echo "  ❌ $*"; fail=1; }
warn() { echo "  ⚠️  $*"; }

echo "Tools"
[ "$(uname)" = Darwin ] && ok "macOS" || bad "macOS required (Unity iOS builds + launchd)"
PY=$(command -v python3 || true)
if [ -n "$PY" ] && "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    ok "python3 $("$PY" -V 2>&1 | cut -d' ' -f2)"
    "$PY" -m pip install -q --disable-pip-version-check -r requirements.txt && ok "python packages (requirements.txt)" \
        || bad "pip install -r requirements.txt failed"
else
    bad "python 3.10+ not found — install from python.org or Homebrew"
fi
UNITY=${UNITY_CLI:-$HOME/.unity/bin/unity}
[ -x "$UNITY" ] && ok "Unity CLI" \
    || bad "Unity CLI missing: curl -fsSL https://public-cdn.cloud.unity3d.com/hub/prod/cli/install.sh | UNITY_CLI_CHANNEL=beta bash"
xcodebuild -version >/dev/null 2>&1 && ok "Xcode" || warn "Xcode not found — only needed for iOS builds"
command -v git-lfs >/dev/null && ok "git-lfs" || warn "git-lfs not found — needed if your game repos use LFS (brew install git-lfs)"
if [ ! -f tools/bundletool.jar ]; then
    mkdir -p tools && curl -fsSL -o tools/bundletool.jar \
        https://github.com/google/bundletool/releases/download/1.18.1/bundletool-all-1.18.1.jar \
        && ok "bundletool downloaded" || bad "bundletool download failed"
else
    ok "bundletool"
fi

echo "Config"
for f in config.env projects.json; do
    src=${f%.*}.example.${f##*.}; [ "$f" = config.env ] && src=config.env.example
    [ -f "$f" ] && ok "$f" || { cp "$src" "$f"; warn "$f created from $src — edit it"; }
done
chmod 600 config.env
val() { grep -E "^$1=" config.env | tail -1 | cut -d= -f2- | tr -d '"'; }
# Play is only queried for Android builds (default platforms = android + ios).
needs_play=$(python3 -c 'import json; print(any("android" in v.get("platforms", ["android"]) for v in json.load(open("projects.json")).values()))' 2>/dev/null)
keys="DISCORD_TOKEN DISCORD_GUILD_ID BUILD_CHANNEL_ID DRIVE_SHARED_DRIVE_ID"
[ "$needs_play" = False ] || keys="$keys PLAY_SERVICE_ACCOUNT"
for k in $keys; do
    [ -n "$(val $k)" ] && ok "$k" || bad "$k empty in config.env (see README → Setup)"
done
sa=$(val PLAY_SERVICE_ACCOUNT); sa=${sa/#\~/$HOME}
[ -z "$sa" ] || [ -f "$sa" ] || bad "PLAY_SERVICE_ACCOUNT file not found: $sa"
[ -n "$(val ASC_KEY_ID)" ] && ok "iOS keys (ASC_KEY_ID)" || warn "ASC_* / TEAM_ID empty — iOS builds disabled until set"
if grep -q '"my-game"' projects.json 2>/dev/null; then
    warn "projects.json still has the example games — add yours (README → Adding a game)"
fi

if [ "${1:-}" = --install ]; then
    echo "launchd"
    if [ $fail -ne 0 ]; then
        echo "  fix the ❌ items first"; exit 1
    fi
    dst=~/Library/LaunchAgents/com.shipkit.bot.plist
    sed -e "s|__KIT_DIR__|$KIT|g" -e "s|__HOME__|$HOME|g" -e "s|__PYTHON__|$PY|g" \
        launchd/com.shipkit.bot.plist > "$dst"
    launchctl bootout "gui/$(id -u)/com.shipkit.bot" 2>/dev/null
    launchctl bootstrap "gui/$(id -u)" "$dst" && ok "bot started — log: ~/Library/Logs/shipbot.log" \
        || bad "launchctl bootstrap failed"
fi

echo
[ $fail -eq 0 ] && echo "All required checks passed." || echo "Fix the ❌ items above, then re-run ./setup.sh"
exit $fail
