# YT Finance Summarizer - Agent Notes

## What Was Implemented
- Built `main.py` end-to-end pipeline:
  - Load config from `.env` and `channels.yaml`
  - Fetch YouTube RSS feed(s)
  - Fetch transcripts via `youtube-transcript-api`
  - Summarize with Anthropic Claude Sonnet 4.5
  - Build HTML digest and send via SendGrid
  - Persist state in `seen_videos.json`
- Added runtime modes:
  - `--dry-run`: prints digest, does not send
  - `--skip-email`: processes/summarizes, skips send
- Added resilience and ops improvements:
  - Atomic state writes
  - Run lock to prevent overlapping cron executions
  - Failure alert emails on any run errors
  - Local timezone rendering for digest (`America/New_York` default)

## Current Repo + Deployment
- GitHub repo: `https://github.com/grbod/ytfinancesummarizer.git`
- Server: Vultr VM
- App path on server: `/opt/ytfinancesummarizer`
- Daily cron schedule:
  - `CRON_TZ=America/New_York`
  - `30 16 * * * cd /opt/ytfinancesummarizer && /opt/ytfinancesummarizer/venv/bin/python main.py >> /opt/ytfinancesummarizer/cron.log 2>&1`

## State and Cost Controls
- State file: `seen_videos.json` (persistent; not auto-purged).
- Status progression:
  - `pending -> transcript_fetched -> summarized -> emailed`
- Anthropic usage is controlled by status:
  - Only videos in `transcript_fetched` are summarized.
  - Already `summarized` or `emailed` videos are not re-summarized.
- First run behavior:
  - Processes the recent RSS backlog (not strict last-24-hours only).

## Proxy Handling
- VPS initially hit YouTube `RequestBlocked` on transcript fetches.
- Resolved by setting `PROXY_URL` in `.env`.
- Proxy confirmed working in production path (transcript + summarize succeeded on VPS).

## Failure Alerts
- Any pipeline error is collected and triggers an alert email via SendGrid.
- Alert recipients:
  - Uses `ALERT_RECIPIENT_EMAILS` if set
  - Otherwise falls back to `RECIPIENT_EMAILS`

## Key Files
- `main.py` - application logic
- `deploy_vultr.sh` - server bootstrap + deploy + cron install
- `.env.example` - env template
- `channels.yaml` - channel config
- `requirements.txt` - dependencies
- `.gitignore` - excludes `.env`, state, venv, logs, lock file

## Lessons Learned
1. Channel IDs must be exact. Handle URLs (`@handle`) are not RSS IDs.
2. Empty/new state causes backlog ingestion from RSS history.
3. VPS IPs may be blocked for transcript scraping; proxy support is mandatory in practice.
4. Cron safety matters:
   - lock file prevents overlapping runs
   - atomic writes protect `seen_videos.json` from corruption
5. Failure visibility is essential. Alert emails make silent cron failures obvious.
6. Keep secrets out of git:
   - `.env` must remain untracked
   - use `.env.example` for structure only

## Operational Runbook
- Local test:
  - `venv/bin/python main.py --skip-email`
- Full run:
  - `venv/bin/python main.py`
- Check current state counts:
  - inspect `seen_videos.json` statuses
- Check VPS logs:
  - `/opt/ytfinancesummarizer/cron.log`
- Redeploy to VPS:
  - run `./deploy_vultr.sh` with required env vars
