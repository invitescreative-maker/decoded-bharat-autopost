"""
poster.py -- runs on GitHub Actions every 5 minutes (PC can be off).

1. Reads Telegram button taps (only from the owner chat).
     sc:<job>  Approve -> schedule at the job's post_at
     ap:<job>  Post now
     ch:<job>  Needs changes
     dc:<job>  Discard
2. Posts every job that is approved ("scheduled") and due, to Instagram via the Graph API.
3. Updates jobs/*.json + state.json (the workflow commits them back).

Secrets (GitHub repo secrets -> env): IG_ACCESS_TOKEN, IG_USER_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
JOBS = ROOT / "jobs"
STATE = ROOT / "state.json"

IG_TOKEN = os.environ["IG_ACCESS_TOKEN"]
IG_USER = os.environ["IG_USER_ID"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER = str(os.environ["TELEGRAM_CHAT_ID"])
VERSION = os.environ.get("IG_API_VERSION", "v23.0")
GRAPH = os.environ.get("IG_GRAPH_HOST", "https://graph.facebook.com").rstrip("/")
TG = f"https://api.telegram.org/bot{TG_TOKEN}"
MAX_ATTEMPTS = 3


def now():
    return datetime.now(timezone.utc)


def log(m):
    print(f"{now():%Y-%m-%d %H:%M:%S}Z {m}", flush=True)


def say(text):
    try:
        requests.post(f"{TG}/sendMessage", json={"chat_id": OWNER, "text": text}, timeout=30)
    except Exception as e:
        log(f"telegram send failed: {e}")


def load_job(p: Path):
    return json.loads(p.read_text(encoding="utf-8-sig"))


def save_job(job):
    (JOBS / f"{job['id']}.json").write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------ telegram taps
def handle_taps():
    state = json.loads(STATE.read_text()) if STATE.exists() else {"offset": 0}
    r = requests.get(f"{TG}/getUpdates", params={"offset": state["offset"], "timeout": 0}, timeout=30).json()
    for u in r.get("result", []):
        state["offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if not cq:
            continue
        try:
            requests.post(f"{TG}/answerCallbackQuery", json={"callback_query_id": cq["id"]}, timeout=15)
        except Exception:
            pass
        if str(cq["from"]["id"]) != OWNER or str(cq["message"]["chat"]["id"]) != OWNER:
            log("ignored tap from non-owner")
            continue
        action, _, jid = (cq.get("data") or "").partition(":")
        p = JOBS / f"{jid}.json"
        if not p.exists():
            say(f"Unknown or old reel ({jid}).")
            continue
        job = load_job(p)
        if job["status"] not in ("pending", "scheduled"):
            say(f"'{job['title']}' is already {job['status']}.")
            continue
        if action == "sc":
            job["status"] = "scheduled"
            say(f"✅ Approved: '{job['title']}' will be posted to @decodedbharat.ai at {job['post_at']} (works even if your PC is off).")
        elif action == "ap":
            job["status"] = "scheduled"; job["post_at"] = now().isoformat()
            say(f"✅ Approved: posting '{job['title']}' now (within a few minutes).")
        elif action == "ch":
            job["status"] = "needs_changes"
            say("✏️ Marked as needs changes. Tell Claude what to change and a new version will be sent.")
        elif action == "dc":
            job["status"] = "discarded"
            say("🗑 Discarded. Nothing was posted.")
        save_job(job)
    STATE.write_text(json.dumps(state))


# ------------------------------------------------------------ instagram
def api(method, path, **kw):
    params = kw.pop("params", {}) or {}
    params["access_token"] = IG_TOKEN
    r = requests.request(method, f"{GRAPH}/{VERSION}/{path}", params=params, timeout=60, **kw)
    d = r.json()
    if r.status_code >= 400 or "error" in d:
        e = d.get("error", d)
        raise RuntimeError(f"Graph API {r.status_code}: {e.get('message', e)} (code={e.get('code')}, sub={e.get('error_subcode')})")
    return d


def publish(video: Path, caption: str, kind: str = "reel", thumb_ms=None) -> str:
    size = video.stat().st_size
    if kind == "story":
        body = {"media_type": "STORIES", "upload_type": "resumable"}            # stories carry no caption
    else:
        body = {"media_type": "REELS", "upload_type": "resumable", "caption": caption, "share_to_feed": "true"}
        if thumb_ms is not None:
            body["thumb_offset"] = str(int(thumb_ms))                           # cover = frame at this offset (ms)
    c = api("POST", f"{IG_USER}/media", data=body)
    cid = c["id"]
    uri = c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{VERSION}/{cid}"
    last = None
    for scheme in ("Bearer", "OAuth", "Bearer", "OAuth"):          # endpoint is flaky; either scheme may work
        with video.open("rb") as f:
            r = requests.post(uri, headers={"Authorization": f"{scheme} {IG_TOKEN}", "offset": "0",
                                            "file_size": str(size)}, data=f, timeout=900)
        try:
            ok = r.status_code == 200 and r.json().get("success", False)
        except ValueError:
            ok = False
        if ok:
            break
        last = f"{r.status_code}: {r.text[:200]}"
        log(f"upload ({scheme}) failed: {last}")
        time.sleep(5)
    else:
        raise RuntimeError(f"upload failed: {last}")
    t0 = time.time()
    while True:
        st = api("GET", cid, params={"fields": "status_code,status"})
        code = st.get("status_code")
        log(f"container {code}")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"container {code}: {st.get('status')}")
        if time.time() - t0 > 600:
            raise RuntimeError("timed out waiting for processing")
        time.sleep(10)
    media_id = api("POST", f"{IG_USER}/media_publish", data={"creation_id": cid})["id"]
    try:
        return api("GET", media_id, params={"fields": "permalink"}).get("permalink", media_id)
    except Exception:
        return media_id


def post_due():
    for p in sorted(JOBS.glob("*.json")):
        try:
            job = load_job(p)
        except Exception as e:                      # one bad job file must never stop the others
            log(f"skipping unreadable job file {p.name}: {e}")
            continue
        if job.get("status") != "scheduled":
            continue
        if datetime.fromisoformat(job["post_at"]) > now():
            continue
        video = ROOT / job["video"]
        if not video.exists():
            job["status"] = "failed"; job["error"] = "video file missing"; save_job(job)
            say(f"⚠️ '{job['title']}' could not be posted: video file missing in the repo.")
            continue
        # claim the job BEFORE uploading (the workflow commits this state in its final step too, and
        # the workflow's concurrency group guarantees only one run at a time)
        job["status"] = "posting"; job["attempts"] = job.get("attempts", 0) + 1; save_job(job)
        try:
            link = publish(video, job.get("caption", ""), job.get("kind", "reel"), job.get("thumb_ms"))
            job["status"] = "posted"; job["link"] = link; job.pop("error", None)
            save_job(job)
            say(f"🎉 Posted to @decodedbharat.ai: {job['title']}\n{link}")
            video.unlink(missing_ok=True)          # keep the repo small
        except Exception as e:
            job["error"] = str(e)
            if job["attempts"] < MAX_ATTEMPTS:
                job["status"] = "scheduled"        # retry on the next run (nothing was published)
                log(f"attempt {job['attempts']} failed, will retry: {e}")
            else:
                job["status"] = "failed"
                say(f"⚠️ Posting '{job['title']}' failed after {MAX_ATTEMPTS} tries (nothing was published):\n{e}")
            save_job(job)


if __name__ == "__main__":
    JOBS.mkdir(exist_ok=True)
    handle_taps()
    post_due()
    log("done")
