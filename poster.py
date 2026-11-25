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
APPROVAL_BOT = "IndianDiplomacyReview_bot"   # the bot that sends the approval buttons


def handle_taps():
    # Never read another bot's inbox (that would swallow messages meant for its own system).
    me = requests.get(f"{TG}/getMe", timeout=30).json().get("result", {}).get("username")
    st = json.loads(STATE.read_text()) if STATE.exists() else {"offset": 0}
    if st.get("bot") != me:                     # recorded so the setup can be checked from the repo
        st["bot"] = me
        STATE.write_text(json.dumps(st))
    if me != APPROVAL_BOT:
        log(f"TELEGRAM_BOT_TOKEN belongs to @{me}, expected @{APPROVAL_BOT}: taps not read")
        return
    state = json.loads(STATE.read_text()) if STATE.exists() else {"offset": 0}
    # No stored offset: Telegram update ids can restart lower, and a stale higher offset silently drops taps.
    # Read everything unconfirmed, handle it, then confirm exactly what was read (end of this function).
    r = requests.get(f"{TG}/getUpdates", params={"timeout": 0}, timeout=30).json()
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
    if r.get("result"):
        requests.get(f"{TG}/getUpdates", params={"offset": r["result"][-1]["update_id"] + 1, "timeout": 0}, timeout=30)
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


def publish(video: Path, caption: str, kind: str = "reel", thumb_ms=None, cover_url=None) -> str:
    size = video.stat().st_size
    if kind == "story":
        body = {"media_type": "STORIES", "upload_type": "resumable"}            # stories carry no caption
    else:
        body = {"media_type": "REELS", "upload_type": "resumable", "caption": caption, "share_to_feed": "true"}
        if cover_url:
            body["cover_url"] = cover_url                                       # branded cover image
        elif thumb_ms is not None:
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


def publish_carousel(urls, caption: str) -> str:
    kids = []
    for u in urls:
        kids.append(api("POST", f"{IG_USER}/media", data={"image_url": u, "is_carousel_item": "true"})["id"])
        time.sleep(1)
    parent = api("POST", f"{IG_USER}/media", data={"media_type": "CAROUSEL", "children": ",".join(kids), "caption": caption})["id"]
    t0 = time.time()
    while True:
        code = api("GET", parent, params={"fields": "status_code,status"}).get("status_code")
        log(f"carousel container {code}")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"carousel container {code}")
        if time.time() - t0 > 300:
            raise RuntimeError("timed out waiting for carousel processing")
        time.sleep(8)
    media_id = api("POST", f"{IG_USER}/media_publish", data={"creation_id": parent})["id"]
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
        kind = job.get("kind", "reel")
        video = ROOT / job["video"] if job.get("video") else None
        if kind != "carousel" and (video is None or not video.exists()):
            job["status"] = "failed"; job["error"] = "video file missing"; save_job(job)
            say(f"⚠️ '{job['title']}' could not be posted: video file missing in the repo.")
            continue
        # claim the job BEFORE uploading (the workflow commits this state in its final step too, and
        # the workflow's concurrency group guarantees only one run at a time)
        job["status"] = "posting"; job["attempts"] = job.get("attempts", 0) + 1; save_job(job)
        try:
            if kind == "carousel":
                link = publish_carousel(job["images"], job.get("caption", ""))
            else:
                link = publish(video, job.get("caption", ""), kind, job.get("thumb_ms"), job.get("cover_url"))
            job["status"] = "posted"; job["link"] = link; job.pop("error", None)
            save_job(job)
            say(f"🎉 Posted to @decodedbharat.ai: {job['title']}\n{link}")
            if video is not None:
                video.unlink(missing_ok=True)      # keep the repo small
        except Exception as e:
            job["error"] = str(e)
            if job["attempts"] < MAX_ATTEMPTS:
                job["status"] = "scheduled"        # retry on the next run (nothing was published)
                log(f"attempt {job['attempts']} failed, will retry: {e}")
            else:
                job["status"] = "failed"
                say(f"⚠️ Posting '{job['title']}' failed after {MAX_ATTEMPTS} tries (nothing was published):\n{e}")
            save_job(job)


def watchdog():
    """Telegram alerts (each sent once) for late / failed / unapproved posts and a broken Instagram token."""
    for p in sorted(JOBS.glob("*.json")):
        try:
            job = load_job(p)
            due = datetime.fromisoformat(job["post_at"])
        except Exception:
            continue
        late_min = (now() - due).total_seconds() / 60
        st = job.get("status")
        if st == "scheduled" and late_min > 10 and not job.get("late_alerted"):
            say(f"⏰ LATE: '{job['title']}' was due at {job['post_at'][11:16]} IST and is {int(late_min)} min overdue. "
                f"It posts on the next run (attempts so far: {job.get('attempts', 0)}). {job.get('error', '')}")
            job["late_alerted"] = True; save_job(job)
        elif st == "pending" and late_min > 30 and not job.get("late_alerted"):
            say(f"⏰ '{job['title']}' is still NOT approved and its slot ({job['post_at'][11:16]} IST) has passed. "
                f"Tap Approve, or discard it.")
            job["late_alerted"] = True; save_job(job)
        elif st == "posting" and late_min > 40 and not job.get("late_alerted"):
            say(f"⚠️ '{job['title']}' has been stuck in 'posting' for a long time. Check it on Instagram before retrying.")
            job["late_alerted"] = True; save_job(job)
    # token health, at most once per 6 hours
    state = json.loads(STATE.read_text()) if STATE.exists() else {"offset": 0}
    if time.time() - state.get("health_at", 0) > 6 * 3600:
        try:
            api("GET", IG_USER, params={"fields": "username"})
            state["health_ok"] = True
        except Exception as e:
            if state.get("health_ok", True):
                say(f"🚨 Instagram token/API check FAILED - posting will stop until fixed:\n{e}")
            state["health_ok"] = False
        state["health_at"] = time.time()
        STATE.write_text(json.dumps(state))


if __name__ == "__main__":
    JOBS.mkdir(exist_ok=True)
    handle_taps()
    post_due()
    try:
        watchdog()
    except Exception as e:                           # monitoring must never break posting
        log(f"watchdog error: {e}")
    log("done")
