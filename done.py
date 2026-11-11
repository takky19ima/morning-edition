#!/usr/bin/env python3
"""Has today's edition of a paper already gone out?

GitHub sometimes skips or delays scheduled runs, so each paper has backup times, and an alarm clock on
Vercel (bot/api/alarm.js) starts it too. The first run of the day does the work and marks it done;
later runs see the mark and stop.
  python done.py check kk   → prints go=true/false (for $GITHUB_OUTPUT)
  python done.py mark kk    → marks today's edition as done
"""
import datetime
import os
import sys
import tomllib
import zoneinfo
from pathlib import Path

action, paper = sys.argv[1], sys.argv[2]
root = Path(__file__).parent
cfg = tomllib.loads((root / "papers" / f"{paper}.toml").read_text())
today = datetime.datetime.now(zoneinfo.ZoneInfo(cfg["paper"]["timezone"])).date().isoformat()
marker = root / "cache" / f"done-{paper}.txt"

if action == "check":
    done = marker.exists() and marker.read_text().strip() == today
    # a manual run always goes ahead; only scheduled backups and the alarm clock stop
    scheduled = os.environ.get("GITHUB_EVENT_NAME") == "schedule" or os.environ.get("SCHEDULED") == "true"
    print(f"go={'false' if done and scheduled else 'true'}")
else:
    marker.parent.mkdir(exist_ok=True)
    marker.write_text(today + "\n")
