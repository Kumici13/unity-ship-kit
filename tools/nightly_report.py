#!/usr/bin/env python3
"""
nightly_report.py — one-screen summary of recent builds from WORK_DIR/builds.json.

    python3 tools/nightly_report.py            # nightly jobs from the last 24 h
    python3 tools/nightly_report.py --last 5   # last 5 jobs of any kind

Prints one line per job and the top of <id>.error.txt for each failure.
The first line says whether the bot is busy (running/queued jobs) — check it before
running pipeline.py by hand, since both share the per-game clone in WORK_DIR/work.
Exit code 1 when any listed job failed.
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def work_dir() -> Path:
    cfg = {}
    env = ROOT / "config.env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"')
    return Path(cfg.get("WORK_DIR", "~/ShipKit")).expanduser()


def main() -> int:
    work = work_dir()
    jobs = json.loads((work / "builds.json").read_text())["jobs"]
    busy = [j["id"] for j in jobs if j["status"] in ("running", "queued")]
    print(f"bot busy: {', '.join('#' + i for i in busy)}" if busy else "bot idle")

    if "--last" in sys.argv:
        picked = jobs[-int(sys.argv[sys.argv.index("--last") + 1]):]
    else:
        # ponytail: nightly = build without a Discord user; 24 h window covers one run
        picked = [j for j in jobs if j["kind"] == "build" and not j.get("user_id")
                  and j.get("created", 0) > time.time() - 86400]

    failed = 0
    for j in picked:
        res = j.get("result") or {}
        what = f"{j.get('platform', '')} {j.get('format', '')}".strip()
        detail = res.get("version", "") and f"{res['version']} ({res.get('number')})"
        print(f"#{j['id']:>4} {j['status']:<11} {j['game']:<18} {what:<12} {detail}")
        if j["status"] == "failed":
            failed += 1
            err = work / "logs" / f"{j['id']}.error.txt"
            text = err.read_text().splitlines() if err.exists() else [j.get("progress", "")]
            text = [l.strip() for l in text if l.strip()]
            errs = list(dict.fromkeys(l for l in text[1:] if "error" in l.lower()))
            for line in text[:1] + (errs or text[1:])[:5]:
                print(f"        {line[:200]}")
            print(f"        full: {work / 'logs' / (j['id'] + '.log')}")
    print(f"{len(picked) - failed}/{len(picked)} ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
