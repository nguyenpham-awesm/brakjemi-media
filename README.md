# brakjemi-media

Automatic webinar-replay publisher. A scheduled GitHub Action fetches the newest
Zoom cloud recording (server-to-server OAuth, unattended) and publishes it as the
release asset `magnetmetoden-opptak.mp4` under the `opptak` tag — so the public
download URL stays **fixed** and the replay page never needs editing.

Fixed video URL:
`https://github.com/nguyenpham-awesm/brakjemi-media/releases/download/opptak/magnetmetoden-opptak.mp4`

- Schedule: every 30 min, evenings (UTC 16:00–23:30). Idempotent — republishes only
  when a *newer* recording exists (marker = meeting uuid in the release body).
- Manual test: Actions → publish-opptak → Run workflow (dry_run = 1 reports only).
- Secrets: `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`.
- Recordings older than `MIN_DATE` (2026-07-22) are ignored.
