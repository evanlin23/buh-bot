"""Twice-daily Discord digest of the YouTube channels: what went up, how it's doing, and how long each queue lasts.

    python digest.py              # build the digest and send it (skips if this morning/evening slot was already sent)
    python digest.py --dry-run    # print it, send nothing, save nothing
    python digest.py --force      # send even if this slot was already sent

Reads only GitHub (the channel repos' upload records, queues and upload runs) and the YouTube Data API.
It uses no Claude at all, so it keeps running when the usage limit is hit, which is exactly when you need it:
the "runway" line says how many days of uploads are left before a channel goes quiet.

Environment:
  GITHUB_TOKEN          any token (the workflow's own is fine); the channel repos are public
  YT_API_KEY            optional: a YouTube Data API key, for views and subscriber counts
  DISCORD_BOT_TOKEN + DISCORD_USER_ID   a DM from your bot, or
  DISCORD_WEBHOOK_URL   a post to a (private) channel
State (what was already reported, last counts) lives in state.json, committed back by the workflow.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state.json"
RUNWAY_WARN_DAYS = 2      # warn when a channel has fewer days of ready videos than this
STALE_RUN_HOURS = 30      # warn when a channel's upload workflow hasn't run for this long

# One entry per channel. per_day is how many the upload workflow posts a day (used for the runway).
CHANNELS = [
    {"name": "HelloBuh", "repo": "evanlin23/hello-buh", "kind": "hellobuh", "per_day": 3,
     "uploads": ("main", "uploads/uploads.json"), "paused": "uploads/PAUSED", "workflow": "upload.yml"},
    {"name": "BuhExplains", "repo": "evanlin23/buh-explains", "kind": "buhexplains", "per_day": 2,
     "uploads": ("main", "buhexplains/uploads.json"), "paused": "buhexplains/PAUSED", "workflow": "upload.yml"},
    {"name": "Buhtown", "repo": "evanlin23/blocky-stories", "kind": "blocky", "per_day": 2,
     "uploads": ("main", "uploads.json"), "paused": None, "workflow": "upload.yml", "approx": True},
    {"name": "Six Degrees", "repo": "evanlin23/sixdegrees", "kind": "sixdegrees", "per_day": 2,
     "uploads": ("queue", "uploaded.json"), "paused": None, "workflow": "upload.yml"},
]


# ---------------------------------------------------------------- HTTP

def http(url: str, *, data: dict | None = None, headers: dict | None = None, method: str | None = None):
    body = json.dumps(data).encode() if data is not None else None
    h = {"User-Agent": "buh-bot-digest (https://github.com/evanlin23/buh-bot, 1.0)", **(headers or {})}
    if body is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def gh(path: str, **params):
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return http(f"https://api.github.com/{path}{q}", headers=h)


def tree(repo: str, ref: str) -> set[str]:
    """Every file path on a branch (one API call)."""
    t = gh(f"repos/{repo}/git/trees/{urllib.parse.quote(ref, safe='')}", recursive="1")
    return {e["path"] for e in t.get("tree", []) if e["type"] == "blob"}


def read(repo: str, ref: str, path: str) -> str | None:
    try:
        r = gh(f"repos/{repo}/contents/{urllib.parse.quote(path)}", ref=ref)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    if r.get("content") is not None and r.get("encoding") == "base64":
        return base64.b64decode(r["content"]).decode()
    # files over 1 MB come back without content: use the blob
    return base64.b64decode(gh(f"repos/{repo}/git/blobs/{r['sha']}")["content"]).decode()


def read_json(repo: str, ref: str, path: str, default=None):
    s = read(repo, ref, path)
    return json.loads(s) if s else default


# ---------------------------------------------------------------- per-channel facts

def records(ch: dict) -> list[dict]:
    """The channel's upload record as [{key, video_id, title, uploaded (CT date or ''), publish_at}]."""
    ref, path = ch["uploads"]
    raw = read_json(ch["repo"], ref, path, default={} if ch["kind"] != "sixdegrees" else [])
    out = []
    if isinstance(raw, list):   # sixdegrees: a list, newest last, no upload date (publish_at only)
        for r in raw:
            out.append({"key": r.get("slug") or r.get("video_id"), "video_id": r.get("video_id"),
                        "title": r.get("title", ""), "uploaded": "", "publish_at": r.get("publish_at", "")})
    else:
        for k, r in raw.items():
            out.append({"key": k, "video_id": r.get("video_id"), "title": r.get("title", ""),
                        "uploaded": r.get("uploaded_central") or r.get("uploaded") or r.get("issued") or "",
                        "publish_at": r.get("publish_at", "")})
    return out


def queue_lines(text: str | None) -> list[str]:
    return [s for s in (ln.split("#")[0].strip() for ln in (text or "").splitlines()) if s]


def ready(ch: dict, done: set[str]) -> list[str]:
    """What the upload workflow could still post, mirroring each repo's own upload script."""
    repo, kind = ch["repo"], ch["kind"]
    if kind == "hellobuh":   # scripts/youtube_upload.py: status "ready" on the videos branch, not posted by hand
        names = sorted({p.split("/")[1] for p in tree(repo, "videos")
                        if p.startswith("shorts/") and p.endswith("/metadata.json")})
        out = []
        for n in names:
            if n in done:
                continue
            m = read_json(repo, "videos", f"shorts/{n}/metadata.json", {}) or {}
            if m.get("status") == "ready" and not m.get("posted"):
                out.append(n)
        return out
    if kind == "buhexplains":   # pipeline/upload.py: in queue.txt, not uploaded, has an episode package
        files = tree(repo, "main")
        q = queue_lines(read(repo, "main", "buhexplains/queue.txt"))
        return [s for s in q if s not in done and f"buhexplains/episodes/{s}/package.json" in files]
    if kind == "blocky":   # scripts/upload.py (approximately): a story with a passing final-film check, not held
        files = tree(repo, "main")
        stories = sorted({p.split("/")[1] for p in files if p.startswith("stories/") and p.endswith("/story.json")})
        out = []
        for s in stories:
            if s in done or f"qa/{s}.json" not in files:
                continue
            q = read_json(repo, "main", f"qa/{s}.json", {}) or {}
            if q.get("stage") == "proof" or q.get("verdict") != "pass":
                continue
            if (read_json(repo, "main", f"stories/{s}/package.json", {}) or {}).get("hold"):
                continue
            out.append(s)
        return out
    if kind == "sixdegrees":   # sixdeg/queue.py: every folder left in queue/ on the queue branch
        return sorted({p.split("/")[1] for p in tree(repo, "queue") if p.startswith("queue/") and p.count("/") >= 2})
    raise ValueError(kind)


def upload_run(ch: dict) -> dict | None:
    r = gh(f"repos/{ch['repo']}/actions/workflows/{ch['workflow']}/runs", per_page=1)
    runs = r.get("workflow_runs", [])
    return runs[0] if runs else None


def activity(ch: dict, since: dt.datetime) -> tuple[int, int]:
    """(commits to main, PRs merged) since the last digest: is anything still being made?"""
    iso = since.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    commits = gh(f"repos/{ch['repo']}/commits", sha="main", since=iso, per_page=100)
    pulls = gh(f"repos/{ch['repo']}/pulls", state="closed", sort="updated", direction="desc", per_page=50)
    merged = sum(1 for p in pulls if p.get("merged_at") and p["merged_at"] >= iso)
    return len(commits), merged


# ---------------------------------------------------------------- YouTube

def yt(path: str, **params):
    key = os.environ.get("YT_API_KEY")
    if not key:
        return None
    return http(f"https://www.googleapis.com/youtube/v3/{path}?" + urllib.parse.urlencode({**params, "key": key}))


def video_stats(ids: list[str]) -> dict[str, dict]:
    """{video_id: {views, likes, comments, channel_id}} for the public ones (private/scheduled ones don't show)."""
    out = {}
    for i in range(0, len(ids), 50):
        r = yt("videos", part="statistics,snippet", id=",".join(ids[i:i + 50]))
        for it in (r or {}).get("items", []):
            s = it.get("statistics", {})
            out[it["id"]] = {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                             "comments": int(s.get("commentCount", 0)), "channel_id": it["snippet"]["channelId"]}
    return out


def channel_stats(cid: str) -> dict | None:
    r = yt("channels", part="statistics", id=cid)
    items = (r or {}).get("items", [])
    if not items:
        return None
    s = items[0]["statistics"]
    return {"subs": int(s.get("subscriberCount", 0)), "views": int(s.get("viewCount", 0))}


# ---------------------------------------------------------------- formatting

def ago(t: dt.datetime, now: dt.datetime) -> str:
    h = (now - t).total_seconds() / 3600
    return f"{h:.0f}h ago" if h >= 1 else f"{h * 60:.0f}m ago"


def ct(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(CT)


def clock(t: dt.datetime, now: dt.datetime) -> str:
    s = t.strftime("%-I:%M %p")
    if t.date() == now.date():
        return s
    if t.date() == now.date() + dt.timedelta(days=1):
        return f"tomorrow {s}"
    return t.strftime("%a ") + s


def delta(n: int, before: int | None) -> str:
    if before is None or n == before:
        return ""
    return f" ({'+' if n > before else ''}{n - before:,})"


def build(state: dict, now: dt.datetime) -> tuple[str, dict]:
    since = ct(state["last_sent"]) if state.get("last_sent") else now - dt.timedelta(hours=12)
    morning = 3 <= now.hour < 15
    head = f"**{'☀️ Morning' if morning else '🌙 Evening'} YouTube digest** · {now.strftime('%a %b %-d, %-I:%M %p')} CT"
    head += f"\n-# since {clock(since, now)}"
    alerts, sections = [], []
    new_state = {"channels": {}, "last_sent": now.isoformat(), "sent_slots": state.get("sent_slots", [])}

    for ch in CHANNELS:
        name, prev = ch["name"], state.get("channels", {}).get(ch["name"], {})
        cur: dict = {}
        try:
            recs = records(ch)
            done = {r["key"] for r in recs}
            seen = set(prev.get("seen", [])) if "seen" in prev else None
            if seen is None:   # first run: anything uploaded or going public since the window start
                new = [r for r in recs if r["uploaded"] >= since.date().isoformat()
                       or (r["publish_at"] and ct(r["publish_at"]) >= since)]
            else:
                new = [r for r in recs if r["key"] not in seen]
            cur["seen"] = sorted(done)

            rdy = ready(ch, done)
            cur["ready"] = rdy
            fresh = len(set(rdy) - set(prev["ready"])) if "ready" in prev else None
            days = len(rdy) / ch["per_day"]
            paused = bool(ch["paused"] and read(ch["repo"], "main", ch["paused"]) is not None)

            run = upload_run(ch)
            commits, merged = activity(ch, since)

            # YouTube numbers: new uploads, plus the last week's videos
            week = [r for r in recs if r.get("video_id") and (
                (r["uploaded"] and r["uploaded"] >= (now - dt.timedelta(days=7)).date().isoformat())
                or (r["publish_at"] and ct(r["publish_at"]) >= now - dt.timedelta(days=7)))]
            stats = video_stats(sorted({r["video_id"] for r in week + new if r.get("video_id")}))
            cid = prev.get("channel_id") or next((s["channel_id"] for s in stats.values()), None)
            chs = channel_stats(cid) if cid else None
            cur.update({"channel_id": cid, "views": {v: s["views"] for v, s in stats.items()}})
            if chs:
                cur.update(chs)

            # alerts
            if paused:
                alerts.append(f"**{name}** is paused (`{ch['paused']}` exists on main)")
            elif days < RUNWAY_WARN_DAYS:
                alerts.append(f"**{name}** has {len(rdy)} ready ({days:.1f} days at {ch['per_day']}/day): make more before it goes quiet")
            if run and run.get("conclusion") == "failure":
                alerts.append(f"**{name}** last upload run failed {ago(ct(run['created_at']), now)}: <{run['html_url']}>")
            elif not paused and (not run or now - ct(run["created_at"]) > dt.timedelta(hours=STALE_RUN_HOURS)):
                alerts.append(f"**{name}** upload workflow hasn't run in {STALE_RUN_HOURS}h+ (check cron-job.org)")

            # section
            top = f"__**{name}**__"
            if chs:
                top += f" · {chs['subs']:,} subs{delta(chs['subs'], prev.get('subs'))} · {chs['views']:,} views{delta(chs['views'], prev.get('views'))}"
            lines = [top]
            if new:
                for r in new[-6:]:
                    vid = r.get("video_id")
                    s = stats.get(vid) if vid else None
                    if s:
                        tail = f"{s['views']:,} views"
                    elif r["publish_at"] and ct(r["publish_at"]) > now:
                        tail = f"goes public {clock(ct(r['publish_at']), now)}"
                    elif not vid:
                        tail = "manual upload"
                    else:
                        tail = "private or not public yet" if os.environ.get("YT_API_KEY") else "uploaded"
                    link = f" <https://youtu.be/{vid}>" if vid else ""
                    lines.append(f"🆕 {r['title'][:70]} · {tail}{link}")
                if len(new) > 6:
                    lines.append(f"…and {len(new) - 6} more")
            else:
                lines.append("No new uploads")
            pv = prev.get("views", {})
            movers = sorted(((s["views"] - pv.get(v, 0), s["views"], v) for v, s in stats.items() if v in pv), reverse=True)
            if movers and movers[0][0] > 0:
                gain, total, v = movers[0]
                title = next((r["title"] for r in recs if r.get("video_id") == v), v)
                lines.append(f"📈 {title[:60]} · +{gain:,} (now {total:,})")
            run_s = ""
            if run:
                icon = {"success": "✅", "failure": "❌", "cancelled": "⚪"}.get(run.get("conclusion") or "", "⏳")
                run_s = f" · last upload run {icon} {ago(ct(run['created_at']), now)}"
            lines.append(f"📦 {len(rdy)} ready{' (approx.)' * bool(ch.get('approx'))} ≈ {days:.1f} days" + (f" ({'+' if fresh >= 0 else ''}{fresh} new)" if fresh else "") + run_s)
            lines.append(f"🛠️ {commits} commit{'s' * (commits != 1)}, {merged} PR{'s' * (merged != 1)} merged" + (" · ⏸️ paused" if paused else ""))
            sections.append("\n".join(lines))
        except Exception as e:   # one broken channel never stops the digest
            cur = prev
            alerts.append(f"**{name}**: couldn't read ({type(e).__name__}: {str(e)[:120]})")
        new_state["channels"][name] = cur

    body = head + "\n\n"
    body += ("⚠️ **Needs you**\n" + "\n".join(f"• {a}" for a in alerts) + "\n\n") if alerts else "✅ Nothing needs you.\n\n"
    body += "\n\n".join(sections)
    if not os.environ.get("YT_API_KEY"):
        body += "\n\n-# No YT_API_KEY set, so no views or subscriber counts."
    return body, new_state


# ---------------------------------------------------------------- Discord

def chunks(text: str, limit: int = 1900) -> list[str]:
    """Discord caps a message at 2000 characters: split on blank lines, then lines."""
    out, cur = [], ""
    for para in text.split("\n\n"):
        piece = para if not cur else "\n\n" + para
        if len(cur) + len(piece) <= limit:
            cur += piece
            continue
        if cur:
            out.append(cur)
        cur = ""
        for ln in para.split("\n"):
            add = ln if not cur else "\n" + ln
            if len(cur) + len(add) > limit:
                out.append(cur)
                cur = ln[:limit]
            else:
                cur += add
    if cur:
        out.append(cur)
    return out


def send(text: str) -> None:
    msgs = chunks(text)
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    token, user = os.environ.get("DISCORD_BOT_TOKEN"), os.environ.get("DISCORD_USER_ID")
    if token and user:
        h = {"Authorization": f"Bot {token}"}
        dm = http("https://discord.com/api/v10/users/@me/channels", data={"recipient_id": user}, headers=h)
        for m in msgs:
            http(f"https://discord.com/api/v10/channels/{dm['id']}/messages",
                 data={"content": m, "allowed_mentions": {"parse": []}}, headers=h)
    elif hook:
        for m in msgs:
            http(hook, data={"content": m, "allowed_mentions": {"parse": []}})
    else:
        sys.exit("set DISCORD_BOT_TOKEN + DISCORD_USER_ID, or DISCORD_WEBHOOK_URL")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the digest; send and save nothing")
    ap.add_argument("--force", action="store_true", help="send even if this slot was already sent")
    a = ap.parse_args()
    now = dt.datetime.now(CT)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    # one digest per slot (morning / evening), so a doubled cron call never sends twice
    slot = f"{now.date().isoformat()} {'am' if 3 <= now.hour < 15 else 'pm'}"
    if slot in state.get("sent_slots", []) and not (a.force or a.dry_run):
        print(f"already sent the {slot} digest. Nothing done.")
        return
    text, new_state = build(state, now)
    print(text)
    if a.dry_run:
        return
    send(text)
    new_state["sent_slots"] = (new_state["sent_slots"] + [slot])[-10:]
    STATE.write_text(json.dumps(new_state, indent=1, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
