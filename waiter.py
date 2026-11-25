"""
Stays awake in GitHub Actions while posts are due in the next 24 hours, so nothing depends on GitHub's cron
or on the owner's PC. Every minute: pull -> poster.py (taps, due posts, alerts) -> push state.
Exits when nothing is left; before GitHub's job time limit it asks the workflow to start a successor.
"""
import glob
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

MAX_RUN_SECONDS = 5.5 * 3600
SLEEP_SECONDS = 60


def sh(*cmd):
    return subprocess.run(cmd).returncode


def active_jobs(now=None):
    """Jobs that still need a runner: approved ones, plus unapproved ones whose slot is near."""
    now = now or datetime.now(timezone.utc)
    out = []
    for f in glob.glob("jobs/*.json"):
        try:
            with open(f, encoding="utf-8-sig") as fh:
                j = json.load(fh)
            due = datetime.fromisoformat(j["post_at"])
        except Exception:
            continue
        st = j.get("status")
        soon = due <= now + timedelta(hours=24)
        if (st == "scheduled" and soon) or (st == "pending" and soon and due >= now - timedelta(hours=1)):
            out.append(j)
    return out


def max_run_seconds():
    """waiter_max.txt (optional, for testing the hand-over) overrides the limit."""
    try:
        with open("waiter_max.txt") as fh:
            return float(fh.read().strip())
    except Exception:
        return MAX_RUN_SECONDS


def save():
    sh("git", "add", "-A", "jobs", "videos", "state.json")
    if sh("git", "diff", "--cached", "--quiet") != 0:
        sh("git", "commit", "-q", "-m", "autopost state")
    ahead = subprocess.run(["git", "rev-list", "--count", "@{u}..HEAD"], capture_output=True, text=True).stdout.strip()
    if ahead not in ("", "0"):
        if sh("git", "pull", "--rebase", "-q") == 0:
            sh("git", "push", "-q")
        else:
            sh("git", "rebase", "--abort")          # keep local state, retry next minute


def main():
    start = time.time()
    while True:
        if sh("git", "pull", "--rebase", "-q") != 0:
            sh("git", "rebase", "--abort")
            print("pull failed, retrying", flush=True)
        else:
            sh(sys.executable, "poster.py")
            save()
        jobs = active_jobs()
        if not jobs:
            print("nothing left in the next 24 hours - stopping", flush=True)
            return
        if time.time() - start > max_run_seconds():
            print(f"{len(jobs)} job(s) left - starting a successor run", flush=True)
            if sh("gh", "workflow", "run", "autopost.yml", "--ref", "main") != 0:
                sys.exit("could not start successor run")
            return
        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    main()
