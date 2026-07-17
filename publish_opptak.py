# -*- coding: utf-8 -*-
"""Auto-publish the newest BRAKJEMI Zoom cloud recording as a GitHub release asset.

Runs in GitHub Actions on a schedule. Fully unattended:
  Zoom S2S OAuth (account credentials -> fresh 1h token each run, no human)
  -> newest completed MP4 recording since MIN_DATE
  -> uploaded as release asset with a FIXED name, so the public URL never changes:
     https://github.com/<owner>/<repo>/releases/download/opptak/magnetmetoden-opptak.mp4

The /opptak page on go.brakjemi.com embeds that URL once and never needs editing.
Idempotent: the release body stores the meeting uuid of the published recording;
re-runs exit early unless a NEWER recording exists. Set DRY_RUN=1 to only report.
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
HOST_USER = os.environ.get("ZOOM_HOST", "WhNGXd5yTYOdiyFNONzpZA")  # Fredrik
MIN_DATE = os.environ.get("MIN_DATE", "2026-07-22")
TAG = os.environ.get("RELEASE_TAG", "opptak")
ASSET = os.environ.get("ASSET_NAME", "magnetmetoden-opptak.mp4")
DRY = os.environ.get("DRY_RUN", "") == "1"

GH = {"Authorization": "Bearer " + GH_TOKEN, "Accept": "application/vnd.github+json",
      "User-Agent": "opptak-bot", "X-GitHub-Api-Version": "2022-11-28"}


def req(url, method="GET", headers=None, data=None, timeout=600):
    r = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
        return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


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
cands = []
for m in meetings:
    mp4s = [f for f in m.get("recording_files", [])
            if f.get("file_type") == "MP4" and f.get("status") == "completed"]
    if mp4s:
        cands.append((m, mp4s))
if not cands:
    print(f"nothing to do: no completed MP4 recordings since {MIN_DATE}")
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
                     json.dumps({"tag_name": TAG, "name": "Webinar-opptak",
                                 "body": "(ingen publisert enda)"}).encode())
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
