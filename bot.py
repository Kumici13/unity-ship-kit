#!/usr/bin/env python3
"""
bot.py — Unity build bot for Discord. Our own "Unity Cloud Build" on this Mac.

Slash commands (only in BUILD_CHANNEL_ID, anyone there may use all of them):
    /build game platform [format] [branch] [dev] [cheats]   queue a build
    /status     running build, queue, free disk
    /builds     last 10 builds
    /abort      kill the running build
    /cancel     drop a queued build

One job runs at a time (one Unity per machine is plenty); the rest wait in a queue that
survives restarts (WORK_DIR/builds.json). Each job is `pipeline.py` in its own process
group under `caffeinate -i nice -n 10`, so the Mac stays awake and your editor stays snappy.

config.env: DISCORD_TOKEN, DISCORD_GUILD_ID, BUILD_CHANNEL_ID, OWNER_DISCORD_ID (optional).
Run: python3 bot.py   (launchd/com.shipkit.bot.plist keeps it alive)
"""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

import ship

SCRIPT_DIR = Path(__file__).parent
PIPELINE = SCRIPT_DIR / "pipeline.py"
PROJECTS = SCRIPT_DIR / "projects.json"
LOW_DISK_GB = 50
BUILD_KEYS = ("game", "platform", "format", "branch", "dev", "cheats")
REUSE_DAYS = 29  # Drive keeps builds 30 days

cfg = ship.load_config()
TOKEN = cfg.get("DISCORD_TOKEN")
GUILD_ID = cfg.get("DISCORD_GUILD_ID")
CHANNEL_ID = int(cfg.get("BUILD_CHANNEL_ID") or 0)
OWNER_ID = cfg.get("OWNER_DISCORD_ID")
# Optional: only members with this role may use commands/buttons (unset = whole channel).
BUILD_ROLE = int(cfg.get("ALLOWED_ROLE_ID") or 0)
# Optional: role for 📤 Upload to Play (unset = same as ALLOWED_ROLE_ID).
UPLOAD_ROLE = int(cfg.get("UPLOAD_ROLE_ID") or 0) or BUILD_ROLE
NIGHTLY_HOUR = int(cfg.get("NIGHTLY_HOUR", "3"))
WORK = Path(cfg.get("WORK_DIR", "~/ShipKit")).expanduser()
WORK.mkdir(parents=True, exist_ok=True)
STATE = WORK / "builds.json"
LOGS = WORK / "logs"

for key, val in (("DISCORD_TOKEN", TOKEN), ("DISCORD_GUILD_ID", GUILD_ID),
                 ("BUILD_CHANNEL_ID", CHANNEL_ID)):
    if not val:
        sys.exit(f"ERROR: {key} missing from config.env")

try:
    import discord
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "discord.py", "certifi"])
    import discord
import ssl, certifi
_default_ctx = ssl.create_default_context
ssl.create_default_context = lambda *a, **k: _default_ctx(*a, **{"cafile": certifi.where(), **k})
from discord import app_commands
try:
    import segno
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "segno"])
    import segno
import io
from discord.ext import tasks

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
guild = discord.Object(id=int(GUILD_ID))


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {"next_id": 1, "jobs": []}


state = load_state()
wake = asyncio.Event()
running: dict = {"job": None, "proc": None}


def save() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE)


def projects() -> dict:
    return json.loads(PROJECTS.read_text())


def queued() -> list[dict]:
    return [j for j in state["jobs"] if j["status"] == "queued"]


def find(job_id: str) -> dict | None:
    return next((j for j in state["jobs"] if j["id"] == job_id), None)


# ── Rendering ─────────────────────────────────────────────────────────────────

ICON = {"queued": "⏳", "running": "⚙️", "ok": "✅", "failed": "❌", "aborted": "🛑",
        "cancelled": "🚫", "interrupted": "⚠️"}


def mins(a: float | None, b: float | None = None) -> str:
    return f"{int(((b or time.time()) - a) // 60)} min" if a else ""


def headline(j: dict) -> str:
    if j["kind"] == "upload":
        return f"📤 **{j['game']}** {j['version']} ({j['number']}) → Play internal"
    what = j["platform"] + (f" {j['format']}" if j["platform"] != "ios" else "")
    flags = "".join(f" · {f}" for f in ("dev", "cheats", "clean") if j.get(f))
    who = f"<@{j['user_id']}>" if j.get("user_id") else "🌙 nightly"
    note = f"\n🏷️ {j['note']}" if j.get("note") else ""
    return f"🔨 **{j['game']}** {what} · `{j['branch']}`{flags} — #{j['id']} by {who}{note}"


def history(game: str, **match) -> list[dict]:
    """Finished-OK builds of a game, newest first, optionally matching more fields."""
    return [j for j in reversed(state["jobs"]) if j["kind"] == "build" and j["game"] == game
            and j["status"] == "ok" and all(j.get(k) == v for k, v in match.items())]


def eta(j: dict) -> str:
    if j["kind"] != "build":
        return ""
    runs = [h["finished"] - h["started"] for h in history(j["game"], platform=j["platform"])[:3]]
    if not runs or not j.get("started"):
        return ""
    left = sum(runs) / len(runs) - (time.time() - j["started"])
    return f" · ~{max(1, round(left / 60))} min left" if left > 0 else " · taking longer than usual"


def render(j: dict) -> str:
    lines = [headline(j)]
    s = j["status"]
    if s == "queued":
        pos = next((i for i, q in enumerate(queued(), 1) if q["id"] == j["id"]), "?")
        lines.append(f"⏳ Queued — position {pos}")
    elif s == "running":
        lines.append(f"⚙️ {j.get('progress', 'Starting')} · {mins(j.get('started'))}{eta(j)}")
    elif s == "ok":
        r = j.get("result", {})
        if j["kind"] == "upload":
            lines.append("✅ Draft on Play internal track — promote it in Play Console")
        else:
            lines.append(f"✅ {r['version']} ({r['number']}) · `{j['branch']}@{r['sha']}` · "
                         f"{mins(j['started'], j['finished'])}")
            if "android" in r:
                a = r["android"]
                lines.append(f"🤖 v{a.get('version', r['version'])} · {a['file']} ({a['size_mb']} MB)\n{a['link']}")
            if "ios" in r:
                lines.append(f"🍎 TestFlight {r['ios']['testflight']} — {r['ios']['state']}")
            ch = r.get("changes") or []
            if ch:
                lines.append(f"📝 {len(ch)}{'+' if len(ch) >= 20 else ''} new commit(s) since last build:")
                lines += [f"• {c[:90]}" for c in ch[:6]]
                if len(ch) > 6:
                    lines.append(f"• …and {len(ch) - 6} more")
    elif s == "failed":
        r = j.get("result", {})
        lines.append(f"❌ **{r.get('category', 'FAILED')}**: {r.get('message', '')[:300]} "
                     "— details in thread")
    else:
        lines.append(f"{ICON[s]} {s.capitalize()}")
    return "\n".join(lines)[:1900]


def direct_link(drive_link: str) -> str:
    """Skips Drive's preview and its "too large to scan" page — a phone tap just downloads."""
    fid = drive_link.split("/d/")[1].split("/")[0]
    return f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"


def result_view(j: dict) -> discord.ui.View | None:
    """Install / Drive link buttons, plus the Play upload button for release AABs."""
    r = j.get("result", {})
    if j["kind"] != "build" or j["status"] in ("queued", "running"):
        return None
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Rebuild", emoji="🔁", custom_id=f"rebuild:{j['id']}"))
    if j["status"] != "ok" or "android" not in r:
        return v
    a = r["android"]
    if a["file"].endswith(".apk"):
        v.add_item(discord.ui.Button(label="Install (tap on phone)", emoji="📱", url=direct_link(a["link"])))
    v.add_item(discord.ui.Button(label="Drive", emoji="💾", url=a["link"]))
    # Play only takes AABs, and never dev/cheat builds.
    if j.get("format") == "aab" and not (j.get("dev") or j.get("cheats")):
        v.add_item(discord.ui.Button(label="Upload to Play (internal)", emoji="📤",
                                     style=discord.ButtonStyle.primary, custom_id=f"upload:{j['id']}"))
    return v


def install_qr(j: dict) -> list:
    """QR of the direct APK link — scan with the phone from a PC screen to install."""
    a = j.get("result", {}).get("android", {})
    if j["status"] != "ok" or not a.get("file", "").endswith(".apk"):
        return []
    buf = io.BytesIO()
    segno.make(direct_link(a["link"])).save(buf, kind="png", scale=5, border=2)
    buf.seek(0)
    return [discord.File(buf, filename=f"install-{j['id']}.png")]


async def refresh(j: dict, view: discord.ui.View | None = None, files: list | None = None) -> None:
    try:
        msg = client.get_channel(CHANNEL_ID).get_partial_message(j["message_id"])
        extra = {"attachments": files} if files else {}
        await msg.edit(content=render(j), view=view,
                       allowed_mentions=discord.AllowedMentions.none(), **extra)
    except discord.HTTPException as e:
        print(f"edit #{j['id']} failed: {e}", flush=True)


async def refresh_queue() -> None:
    for q in queued():
        await refresh(q)


# ── Worker ────────────────────────────────────────────────────────────────────

def editor_has_open(repo_path: str) -> bool:
    ps = subprocess.run(["ps", "-Ax", "-o", "command"], capture_output=True, text=True).stdout
    repo = str(Path(repo_path).expanduser()).lower().rstrip("/")
    return any("unity.app" in l.lower() and "-projectpath" in l.lower() and repo in l.lower()
               for l in ps.splitlines())


async def run_job(j: dict) -> None:
    channel = client.get_channel(CHANNEL_ID)
    j.update(status="running", started=time.time(), progress="Starting")
    save()
    await refresh(j)
    await refresh_queue()

    if shutil.disk_usage(WORK).free < LOW_DISK_GB * 1024**3:
        await channel.send(f"⚠️ Low disk: {shutil.disk_usage(WORK).free // 1024**3} GB free on the build Mac.")
    game = projects().get(j["game"], {})
    if OWNER_ID and j["kind"] == "build" and editor_has_open(game.get("repo_path", "")):
        await channel.send(f"<@{OWNER_ID}> FYI: #{j['id']} is building **{j['game']}** while your "
                           "editor has it open. Separate clone, so your repo is safe — the Mac "
                           "may just feel slower.")

    LOGS.mkdir(parents=True, exist_ok=True)
    log_path = LOGS / f"{j['id']}.log"
    payload = {k: j.get(k) for k in ("id", *BUILD_KEYS, "version", "number", "note", "clean", "since")}
    if j["kind"] == "upload":
        payload["id"] = j["parent"]
    proc = await asyncio.create_subprocess_exec(
        "caffeinate", "-i", "nice", "-n", "10", sys.executable, "-u", str(PIPELINE),
        j["kind"], json.dumps(payload), cwd=SCRIPT_DIR, start_new_session=True,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=1 << 20)
    running.update(job=j, proc=proc)

    result, last_edit = None, 0.0
    with open(log_path, "w") as log:
        async for raw in proc.stdout:
            line = raw.decode(errors="replace")
            log.write(line)
            log.flush()
            if line.startswith("@@PROGRESS "):
                j["progress"] = line[11:].strip()
                if time.time() - last_edit > 5:
                    last_edit = time.time()
                    await refresh(j)
            elif line.startswith("@@RESULT "):
                result = json.loads(line[9:])
    await proc.wait()
    running.update(job=None, proc=None)

    j["finished"] = time.time()
    j["result"] = result or {"category": "CRASHED", "message": f"pipeline exit {proc.returncode}"}
    if j.get("abort"):
        j["status"] = "aborted"
    else:
        j["status"] = "ok" if result and result.get("ok") else "failed"
    save()
    await refresh(j, result_view(j), install_qr(j))

    msg = channel.get_partial_message(j["message_id"])
    if j["status"] == "failed":
        r = j["result"]
        thread = await (await msg.fetch()).create_thread(name=f"#{j['id']} {j['game']} failed")
        await thread.send(f"**{r.get('category')}**: {r.get('message', '')[:1500]}\n"
                          f"```\n{(r.get('hint') or '')[-1500:]}\n```",
                          file=discord.File(log_path))
    if j["status"] in ("ok", "failed"):
        who = f"<@{j['user_id']}>" if j.get("user_id") else "🌙"
        await channel.send(f"{who} #{j['id']} {j['game']} {ICON[j['status']]}",
                           reference=msg, mention_author=False)


async def worker() -> None:
    while True:
        q = queued()
        if not q:
            wake.clear()
            await wake.wait()
            continue
        try:
            await run_job(q[0])
        except Exception as e:  # never let one job kill the queue
            import traceback
            traceback.print_exc()
            q[0].update(status="failed", finished=time.time(),
                        result={"category": "BOT ERROR", "message": str(e)})
            save()
            await refresh(q[0])


def enqueue(j: dict) -> None:
    state["jobs"].append(j)
    state["jobs"] = state["jobs"][-500:]
    save()
    wake.set()


def new_id() -> str:
    i = state["next_id"]
    state["next_id"] = i + 1
    return str(i)


# ── Commands ──────────────────────────────────────────────────────────────────

def in_channel(inter: discord.Interaction) -> bool:
    return CHANNEL_ID in (inter.channel_id, getattr(inter.channel, "parent_id", None))


def has_role(inter: discord.Interaction, role: int) -> bool:
    return not role or any(r.id == role for r in getattr(inter.user, "roles", []))


async def wrong_channel(inter: discord.Interaction, role: int = 0) -> bool:
    """True (and replies) when the user may not act here: wrong channel or missing role."""
    if not in_channel(inter):
        msg = f"Use this in <#{CHANNEL_ID}>."
    elif not has_role(inter, role or BUILD_ROLE):
        msg = f"You need the <@&{role or BUILD_ROLE}> role for this."
    else:
        return False
    await inter.response.send_message(msg, ephemeral=True,
                                      allowed_mentions=discord.AllowedMentions.none())
    return True


async def game_complete(inter: discord.Interaction, current: str):
    return [app_commands.Choice(name=k, value=k) for k in sorted(projects())
            if current.lower() in k.lower()][:25]


async def branch_complete(inter: discord.Interaction, current: str):
    game = projects().get(inter.namespace.game or "")
    if not game:
        return []
    clone = WORK / "work" / inter.namespace.game
    repo = clone if (clone / ".git").exists() else Path(game["repo_path"]).expanduser()
    out = subprocess.run(["git", "-C", str(repo), "for-each-ref", "--sort=-committerdate",
                          "--format=%(refname:lstrip=3)", "refs/remotes/origin"],
                         capture_output=True, text=True, timeout=2).stdout.split()
    return [app_commands.Choice(name=b, value=b) for b in out
            if b != "HEAD" and current.lower() in b.lower()][:25]


async def remote_sha(game: str, branch: str) -> str | None:
    repo = str(Path(projects()[game]["repo_path"]).expanduser())
    def ls() -> str:
        url = subprocess.run(["git", "-C", repo, "remote", "get-url", "origin"],
                             capture_output=True, text=True).stdout.strip()
        return subprocess.run(["git", "ls-remote", url, f"refs/heads/{branch}"],
                              capture_output=True, text=True, timeout=30).stdout.split("\t")[0]
    try:
        return await asyncio.to_thread(ls) or None
    except (subprocess.SubprocessError, OSError):
        return None


async def show(dest, j: dict, prefix: str = "", **kw) -> None:
    """Post a finished build's card (buttons + QR) somewhere else — reuse, /latest."""
    await dest.send(prefix + render(j), view=result_view(j), files=install_qr(j),
                    allowed_mentions=discord.AllowedMentions.none(), **kw)


async def submit(params: dict, user_id: int | None, force: bool = False,
                 quiet_reuse: bool = False) -> str:
    """Queue a build — or, when this exact commit + settings was already built, repost it."""
    channel = client.get_channel(CHANNEL_ID)
    sha = await remote_sha(params["game"], params["branch"])
    if sha is None:
        return f"Branch `{params['branch']}` not found on origin."
    same = [h for h in history(params["game"], **{k: params[k] for k in BUILD_KEYS if k != "game"})
            if sha.startswith(h["result"]["sha"]) and time.time() - h["finished"] < REUSE_DAYS * 86400]
    if same and not force and not params.get("clean"):
        if not quiet_reuse:
            await show(channel, same[0], f"♻️ No new commits since #{same[0]['id']} — here it is again "
                                          "(use `force:True` to rebuild anyway)\n")
        return f"Same commit as #{same[0]['id']} — reposted it."
    prev = history(params["game"], branch=params["branch"])
    j = {**params, "id": new_id(), "kind": "build", "user_id": user_id, "status": "queued",
         "created": time.time(), "since": prev[0]["result"]["sha"] if prev else None}
    j["message_id"] = (await channel.send("…", allowed_mentions=discord.AllowedMentions.none())).id
    enqueue(j)
    await refresh(j)
    return f"Queued #{j['id']}."


@tree.command(guild=guild, name="build", description="Build a game (Android → Drive link, iOS → TestFlight)")
@app_commands.describe(game="Game from projects.json", platform="Which platform(s) (default android)",
                       format="Android file type (APK installs directly, AAB is for Play)",
                       branch="Branch on origin (default: the game's main branch)",
                       dev="Development build (profiler, debug logs)",
                       cheats="Enable this game's cheat defines",
                       note="Short label, e.g. 'shop fix' — shown in the message and file name",
                       clean="Wipe the Library cache first (slow; fixes broken imports)",
                       force="Rebuild even if this exact commit was already built")
@app_commands.autocomplete(game=game_complete, branch=branch_complete)
@app_commands.choices(
    platform=[app_commands.Choice(name=n, value=n) for n in ("android", "ios", "both")],
    format=[app_commands.Choice(name=n, value=n) for n in ("apk", "aab")])
async def build(inter: discord.Interaction, game: str, platform: str = "android", format: str = "apk",
                branch: str | None = None, dev: bool = False, cheats: bool = False,
                note: str | None = None, clean: bool = False, force: bool = False):
    if await wrong_channel(inter):
        return
    p = projects()
    if game not in p:
        await inter.response.send_message(f"Unknown game `{game}`. Pick one from the list.", ephemeral=True)
        return
    if cheats and not p[game].get("cheat_defines"):
        await inter.response.send_message(f"`{game}` has no cheats configured yet.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True, thinking=True)
    params = {"game": game, "platform": platform, "format": format,
              "branch": branch or p[game]["branch"], "dev": dev, "cheats": cheats,
              "note": (note or "")[:80] or None, "clean": clean}
    await inter.followup.send(await submit(params, inter.user.id, force=force), ephemeral=True)


@tree.command(guild=guild, name="latest", description="Newest finished build of a game (install link + QR)")
@app_commands.describe(game="Game", branch="Only this branch (default: any)")
@app_commands.autocomplete(game=game_complete, branch=branch_complete)
async def latest(inter: discord.Interaction, game: str, branch: str | None = None):
    if await wrong_channel(inter):
        return
    h = history(game, **({"branch": branch} if branch else {}))
    if not h:
        await inter.response.send_message(f"No finished builds of `{game}` yet.", ephemeral=True)
        return
    await inter.response.send_message(render(h[0]), view=result_view(h[0]), files=install_qr(h[0]),
                                      ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@tree.command(guild=guild, name="status", description="Running build, queue and free disk")
async def status(inter: discord.Interaction):
    if await wrong_channel(inter):
        return
    r = running["job"]
    lines = [f"⚙️ #{r['id']} {r['game']}: {r.get('progress')} · {mins(r.get('started'))}"
             if r else "Idle."]
    lines += [f"⏳ #{q['id']} {q['game']} {q.get('platform', 'upload')} `{q.get('branch', '')}`"
              for q in queued()]
    lines.append(f"💾 {shutil.disk_usage(WORK).free // 1024**3} GB free")
    await inter.response.send_message("\n".join(lines), ephemeral=True)


@tree.command(guild=guild, name="builds", description="Last 10 builds")
async def builds(inter: discord.Interaction):
    if await wrong_channel(inter):
        return
    rows = []
    for j in reversed([j for j in state["jobs"] if j["kind"] == "build"][-10:]):
        r = j.get("result", {})
        num = f"{r['version']} ({r['number']})" if r.get("number") else ""
        when = datetime.fromtimestamp(j["created"]).strftime("%d.%m %H:%M")
        link = r.get("android", {}).get("link", "")
        rows.append(f"{ICON[j['status']]} #{j['id']} {when} **{j['game']}** {j['platform']} "
                    f"`{j['branch']}` {num} <@{j['user_id']}> {link}")
    await inter.response.send_message("\n".join(rows) or "No builds yet.", ephemeral=True,
                                      allowed_mentions=discord.AllowedMentions.none(),
                                      suppress_embeds=True)


@tree.command(guild=guild, name="abort", description="Kill the running build")
async def abort(inter: discord.Interaction):
    if await wrong_channel(inter):
        return
    j, proc = running["job"], running["proc"]
    if not j:
        await inter.response.send_message("Nothing is running.", ephemeral=True)
        return
    j["abort"] = True
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    await inter.response.send_message(f"🛑 Aborting #{j['id']} {j['game']} — by {inter.user.mention}")
    await asyncio.sleep(20)
    if running["proc"] is proc:  # Unity ignored SIGTERM
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def queued_complete(inter: discord.Interaction, current: str):
    return [app_commands.Choice(name=f"#{q['id']} {q['game']} {q.get('platform', 'upload')}", value=q["id"])
            for q in queued() if current in q["id"]][:25]


@tree.command(guild=guild, name="cancel", description="Remove a queued build")
@app_commands.autocomplete(build=queued_complete)
async def cancel(inter: discord.Interaction, build: str):
    if await wrong_channel(inter):
        return
    j = find(build)
    if not j or j["status"] != "queued":
        await inter.response.send_message(f"#{build} is not queued.", ephemeral=True)
        return
    j["status"] = "cancelled"
    save()
    await inter.response.send_message(f"🚫 Cancelled #{build}.", ephemeral=True)
    await refresh(j)
    await refresh_queue()


@tree.error
async def on_command_error(inter: discord.Interaction, error: app_commands.AppCommandError):
    """Answer instead of leaving the user on "is thinking…" forever."""
    import traceback
    traceback.print_exception(error)
    msg = f"⚠️ Bot error: `{type(getattr(error, 'original', error)).__name__}: {getattr(error, 'original', error)}`"
    send = inter.followup.send if inter.response.is_done() else inter.response.send_message
    await send(msg[:1900], ephemeral=True)


@client.event
async def on_interaction(inter: discord.Interaction):
    cid = (inter.data or {}).get("custom_id", "")
    if inter.type != discord.InteractionType.component:
        return
    if not cid.startswith(("rebuild:", "upload:")):
        return
    if await wrong_channel(inter, UPLOAD_ROLE if cid.startswith("upload:") else 0):
        return
    if cid.startswith("rebuild:"):
        old = find(cid.split(":", 1)[1])
        if not old:
            await inter.response.send_message("That build is gone.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True, thinking=True)
        params = {k: old.get(k) for k in (*BUILD_KEYS, "note")}
        await inter.followup.send(await submit(params, inter.user.id), ephemeral=True)
        return
    parent = find(cid.split(":", 1)[1])
    if not parent or parent["status"] != "ok":
        await inter.response.send_message("That build is gone.", ephemeral=True)
        return
    if any(j.get("parent") == parent["id"] and j["status"] in ("queued", "running", "ok")
           for j in state["jobs"]):
        await inter.response.send_message("Already uploaded or queued.", ephemeral=True)
        return
    r = parent["result"]
    j = {"id": new_id(), "kind": "upload", "parent": parent["id"], "game": parent["game"],
         "version": r["version"], "number": r["number"], "user_id": inter.user.id,
         "status": "queued", "created": time.time()}
    await inter.response.send_message(f"Queued Play upload #{j['id']}.", ephemeral=True)
    j["message_id"] = (await inter.channel.send(
        "…", reference=inter.message, mention_author=False,
        allowed_mentions=discord.AllowedMentions.none())).id
    enqueue(j)
    await refresh(j)


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@tasks.loop(hours=24)
async def prune_drive():
    if not cfg.get("DRIVE_SHARED_DRIVE_ID"):
        return
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / "prune.log", "w") as log:
        proc = await asyncio.create_subprocess_exec(sys.executable, str(PIPELINE), "prune",
                                                    cwd=SCRIPT_DIR, stdout=log, stderr=log)
        await proc.wait()


@tasks.loop(time=dtime(hour=NIGHTLY_HOUR, tzinfo=datetime.now().astimezone().tzinfo))
async def nightly():
    """QA build (dev + cheats, APK) of every game's main branch, iOS too where configured —
    skipped when nothing changed since the last nightly."""
    for game, conf in projects().items():
        if conf.get("nightly", True):
            ios = "ios" in conf.get("platforms", ["android", "ios"]) and conf.get("bundle_id")
            params = {"game": game, "platform": "both" if ios else "android", "format": "apk",
                      "branch": conf["branch"], "dev": True, "cheats": bool(conf.get("cheat_defines")),
                      "note": None, "clean": False}
            print(f"nightly {game}: {await submit(params, None, quiet_reuse=True)}", flush=True)


_started = False


@client.event
async def on_ready():
    global _started
    print(f"on_ready: {client.user}", flush=True)
    if _started:  # on_ready fires again after reconnects
        return
    _started = True
    for j in state["jobs"]:
        if j["status"] == "running":  # bot died mid-build
            j["status"] = "interrupted"
            await refresh(j, result_view(j))
    save()
    cmds = await tree.sync(guild=guild)
    print(f"Synced {len(cmds)} command(s) to guild {GUILD_ID}.", flush=True)
    prune_drive.start()
    nightly.start()
    asyncio.create_task(worker())
    wake.set()


client.run(TOKEN)
