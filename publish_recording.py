# -*- coding: utf-8 -*-
"""Auto-publish the newest Zoom cloud recording as a GitHub release asset.

Generic, client-agnostic version (webinar-replay-pipeline skill).
Runs in GitHub Actions on a schedule. Fully unattended:
  Zoom S2S OAuth (account credentials -> fresh 1h token each run, no human)
  -> newest completed MP4 recording since MIN_DATE
  -> uploaded as release asset with a FIXED name, so the public URL never changes:
     https://github.com/<owner>/<repo>/releases/download/<RELEASE_TAG>/<ASSET_NAME>

The client's replay page embeds that URL once and never needs editing.
Idempotent: the release body stores the meeting uuid of the published recording;
re-runs exit early unless a NEWER recording exists. Set DRY_RUN=1 to only report.
Requires env: ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET, GITHUB_TOKEN,
GITHUB_REPOSITORY (set by Actions), ZOOM_HOST, MIN_DATE, RELEASE_TAG, ASSET_NAME.
"""
import base64
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ZACC = os.environ["ZOOM_ACCOUNT_ID"]
ZID = os.environ["ZOOM_CLIENT_ID"]
ZSEC = os.environ["ZOOM_CLIENT_SECRET"]
GH_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]  # owner/repo, set by Actions
HOST_USER = os.environ["ZOOM_HOST"]     # zoom userId or email of the webinar host
MIN_DATE = os.environ["MIN_DATE"]       # only recordings on/after this date
TAG = os.environ.get("RELEASE_TAG", "replay")
ASSET = os.environ.get("ASSET_NAME", "webinar-replay.mp4")
DRY = os.environ.get("DRY_RUN", "") == "1"
# Matching the right recording. A strict webinar-id lock is FRAGILE: hosts very
# often run the session in their Personal Meeting Room (PMI) or an ad-hoc meeting,
# so the recording's meeting id != the scheduled webinar id and a strict lock
# silently finds nothing. Prefer a TIME WINDOW around the session start (catches
# PMI/ad-hoc), and keep MEETING_ID only as an optional extra filter.
MEETING_ID = os.environ.get("MEETING_ID", "").strip()      # optional exact id (incl. host PMI)
MATCH_FROM = os.environ.get("MATCH_FROM", "").strip()      # ISO, e.g. 2026-07-21T09:00:00Z
MATCH_TO   = os.environ.get("MATCH_TO", "").strip()        # ISO, e.g. 2026-07-21T13:00:00Z


def _in_window(start_iso):
    if not (MATCH_FROM and MATCH_TO):
        return True
    try:
        s = start_iso.replace("Z", "+00:00")
        return MATCH_FROM.replace("Z", "+00:00") <= s <= MATCH_TO.replace("Z", "+00:00")
    except Exception:
        return True

GH = {"Authorization": "Bearer " + GH_TOKEN, "Accept": "application/vnd.github+json",
      "User-Agent": "replay-bot", "X-GitHub-Api-Version": "2022-11-28"}


def req(url, method="GET", headers=None, data=None, timeout=600):
    r = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
        return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---- 0. Escalating scan-gate (event-relative timing) ----
# Cron fires every 5 min during the window; this gate enforces the exact cadence and
# the give-up cap using the release body as persisted state, so we never poll "forever".
#   T+0..60 wait | T+60..90 every 15m | T+90..120 every 10m | T+120..220 every 5m
#   > T+220 -> give up: open a GitHub issue + disable the schedule.
WEBINAR_START = os.environ.get("WEBINAR_START", "").strip()  # ISO e.g. 2026-07-21T12:00:00+02:00

def _wf_file():
    return os.environ.get("GITHUB_WORKFLOW_REF", "").split("/")[-1].split("@")[0] or "publish.yml"

def _disable_schedule():
    return req(f"https://api.github.com/repos/{REPO}/actions/workflows/{_wf_file()}/disable", "PUT", GH)

def _get_or_create_release():
    c, b = req(f"https://api.github.com/repos/{REPO}/releases/tags/{TAG}", headers=GH)
    if c == 404:
        c, b = req(f"https://api.github.com/repos/{REPO}/releases", "POST",
                   {**GH, "Content-Type": "application/json"},
                   json.dumps({"tag_name": TAG, "name": "Webinar replay",
                               "body": "(nothing published yet)"}).encode())
    return json.loads(b)

def _interval_for(d):
    if d < 60:  return None    # too early
    if d < 90:  return 15
    if d < 120: return 10
    if d <= 220: return 5
    return 0                   # past the cap -> give up

if WEBINAR_START:
    import re as _re
    _start = datetime.datetime.fromisoformat(WEBINAR_START.replace("Z", "+00:00"))
    _now = datetime.datetime.now(datetime.timezone.utc)
    _delta = (_now - _start).total_seconds() / 60.0
    _rel = _get_or_create_release()
    _rb = _rel.get("body") or ""
    if "published uuid:" not in _rb:          # not already done
        _iv = _interval_for(_delta)
        if _iv is None:
            print(f"gate: too early (T+{_delta:.0f}m) — first scan at T+60"); sys.exit(0)
        if _iv == 0:
            if "gaveup:1" not in _rb:
                req(f"https://api.github.com/repos/{REPO}/issues", "POST",
                    {**GH, "Content-Type": "application/json"},
                    json.dumps({"title": "Replay: no cloud recording 220 min after webinar",
                                "body": (f"No completed Zoom **cloud** recording appeared within 220 minutes "
                                         f"of WEBINAR_START={WEBINAR_START}. Most likely the host recorded "
                                         f"locally / to Vimeo / to a different Zoom account. Action needed: "
                                         f"use the manual embed/upload fallback for the replay page.")}).encode())
                req(f"https://api.github.com/repos/{REPO}/releases/{_rel['id']}", "PATCH",
                    {**GH, "Content-Type": "application/json"},
                    json.dumps({"body": (_rb + " gaveup:1").strip()}).encode())
                _disable_schedule()
                print("gate: GAVE UP after 220m — issue opened + schedule disabled.")
            else:
                print("gate: already gave up (issue open); schedule should be disabled.")
            sys.exit(0)
        _m = _re.search(r"lastscan:(\d+)", _rb)
        _age = None if not _m else (_now.timestamp() - int(_m.group(1))) / 60.0
        if _age is not None and _age < _iv - 0.5:
            print(f"gate: skip (T+{_delta:.0f}m, interval {_iv}m, last scan {_age:.0f}m ago)"); sys.exit(0)
        _nb = _re.sub(r"\s*lastscan:\d+", "", _rb).strip()
        _nb = (_nb + f" lastscan:{int(_now.timestamp())}").strip()
        req(f"https://api.github.com/repos/{REPO}/releases/{_rel['id']}", "PATCH",
            {**GH, "Content-Type": "application/json"}, json.dumps({"body": _nb}).encode())
        print(f"gate: SCAN (T+{_delta:.0f}m, phase interval {_iv}m)")

# ---- 1. Zoom token (server-to-server, no human) ----
basic = base64.b64encode(f"{ZID}:{ZSEC}".encode()).decode()
code, body = req(
    f"https://zoom.us/oauth/token?grant_type=account_credentials&account_id={ZACC}",
    "POST", {"Authorization": "Basic " + basic})
if code != 200:
    sys.exit(f"zoom auth failed {code}: {body[:200]}")
ztok = json.loads(body)["access_token"]
zh = {"Authorization": "Bearer " + ztok}

# ---- 2. newest completed MP4 cloud recording since MIN_DATE ----
today = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
code, body = req(
    f"https://api.zoom.us/v2/users/{HOST_USER}/recordings?from={MIN_DATE}&to={today}&page_size=30",
    headers=zh)
if code != 200:
    sys.exit(f"list recordings failed {code}: {body[:300]}")
meetings = json.loads(body).get("meetings", [])
cands, processing = [], 0
for m in meetings:
    if MEETING_ID and str(m.get("id", "")) != MEETING_ID:
        continue
    if not _in_window(m.get("start_time", "")):
        continue
    mp4s_all = [f for f in m.get("recording_files", []) if f.get("file_type") == "MP4"]
    mp4s = [f for f in mp4s_all if f.get("status") == "completed"]
    if mp4s:
        cands.append((m, mp4s))
    elif mp4s_all:
        processing += 1  # a recording exists for this session but Zoom is still processing it
if not cands:
    scope = (f" for meeting {MEETING_ID}" if MEETING_ID else "") + \
            (f" in window {MATCH_FROM}..{MATCH_TO}" if MATCH_FROM else "")
    if processing:
        # IMPORTANT: this is NOT "no recording" — it exists but isn't finished.
        # Exit non-zero-ish signal via message so the scheduler retries next run.
        print(f"recording PROCESSING (not yet downloadable){scope} — will publish on a later run.")
        sys.exit(0)
    print(f"nothing to do: no completed MP4 recordings since {MIN_DATE}{scope}. "
          f"NOTE: if the host recorded LOCALLY / to Vimeo / to a different Zoom account, "
          f"there is no cloud recording to fetch — use the manual/embed fallback.")
    sys.exit(0)
cands.sort(key=lambda t: t[0].get("start_time", ""))
meeting, mp4s = cands[-1]
mp4s.sort(key=lambda f: f.get("file_size", 0))
rec = mp4s[-1]  # largest MP4 = full-length main view
uuid = meeting.get("uuid", "")
print(f"newest recording: {meeting.get('topic')} @ {meeting.get('start_time')} "
      f"({rec.get('file_size', 0) / 1e6:.0f} MB)")

# ---- 3. release marker: skip if this uuid is already published ----
code, body = req(f"https://api.github.com/repos/{REPO}/releases/tags/{TAG}", headers=GH)
if code == 404:
    code, body = req(f"https://api.github.com/repos/{REPO}/releases", "POST",
                     {**GH, "Content-Type": "application/json"},
                     json.dumps({"tag_name": TAG, "name": "Webinar replay",
                                 "body": "(nothing published yet)"}).encode())
    if code not in (200, 201):
        sys.exit(f"release create failed {code}: {body[:300]}")
release = json.loads(body)
if uuid and uuid in (release.get("body") or ""):
    print(f"already published (uuid {uuid}) - nothing to do")
    sys.exit(0)
if DRY:
    print(f"DRY RUN: would publish '{meeting.get('topic')}' {meeting.get('start_time')} "
          f"as {ASSET}")
    sys.exit(0)

# ---- 4. download from Zoom ----
dl = rec["download_url"] + "?access_token=" + urllib.parse.quote(ztok)
print("downloading from Zoom ...")
urllib.request.urlretrieve(dl, "rec.mp4")
size = os.path.getsize("rec.mp4")
print(f"downloaded {size / 1e6:.0f} MB")
if size < 1_000_000:
    sys.exit("download suspiciously small - aborting (recording not ready?)")

# ---- 5. replace the release asset (fixed name -> fixed public URL) ----
for a in release.get("assets", []):
    if a["name"] == ASSET:
        req(a["url"], "DELETE", GH)
        print("deleted previous asset")
up = (f"https://uploads.github.com/repos/{REPO}/releases/{release['id']}/assets"
      f"?name={urllib.parse.quote(ASSET)}")
print("uploading to GitHub ...")
with open("rec.mp4", "rb") as fh:
    r = urllib.request.Request(up, data=fh, method="POST",
                               headers={**GH, "Content-Type": "video/mp4",
                                        "Content-Length": str(size)})
    resp = urllib.request.urlopen(r, timeout=3600)
    if resp.status not in (200, 201):
        sys.exit(f"asset upload failed {resp.status}")

# ---- 6. stamp the marker ----
req(f"https://api.github.com/repos/{REPO}/releases/{release['id']}", "PATCH",
    {**GH, "Content-Type": "application/json"},
    json.dumps({"body": f"published uuid:{uuid} start:{meeting.get('start_time')} "
                        f"topic:{meeting.get('topic')}"}).encode())
print(f"PUBLISHED {ASSET} ({size / 1e6:.0f} MB) from '{meeting.get('topic')}' "
      f"{meeting.get('start_time')}")

# ---- 7. stop the schedule once we've published (don't poll forever) ----
# For a single webinar this disables the cron after success, so the Action isn't
# waking 25x/day doing nothing. Recurring series: set STOP_AFTER_PUBLISH=0 to keep
# it live and re-run setup per webinar. (Needs `permissions: actions: write`.)
if os.environ.get("STOP_AFTER_PUBLISH", "1") != "0":
    code, body = _disable_schedule()
    print(f"schedule disabled after publish [{code}] — re-run setup for the next webinar."
          if code in (204, 200) else f"NOTE: could not auto-disable schedule [{code}] {str(body)[:120]}")
