# YTsummarizer - YouTube Stock Analysis Daily Digest

## Context

You follow stock/market YouTube channels (starting with @WillRichYT) and want a daily email summarizing new videos - focusing on which stocks moved, why, and whether it matters. Rather than watching every video, you get a structured digest with the key takeaways.

The script runs once daily via cron (on a VPS, developed locally on Mac). It checks RSS feeds for new videos, grabs transcripts from YouTube, sends them to Claude API for stock-focused summarization, then emails you a combined HTML digest via SendGrid.

## Project Structure

```
/Users/gregsimek/Code/YTsummarizer/
  main.py              # Single entry point - RSS check, transcript fetch, summarize, email
  channels.yaml        # Channel list config (name, channel_id)
  .env                 # Secrets (ANTHROPIC_API_KEY, SENDGRID_API_KEY, emails)
  .env.example         # Template for .env
  seen_videos.json     # State tracking (auto-created on first run)
  requirements.txt     # Python dependencies
  .gitignore           # Ignore .env, seen_videos.json, venv, __pycache__
```

## Dependencies (requirements.txt)

```
anthropic
feedparser
youtube-transcript-api
sendgrid
python-dotenv
pyyaml
```

## Configuration

### .env
```
ANTHROPIC_API_KEY=sk-ant-...
SENDGRID_API_KEY=SG....
SENDER_EMAIL=sender@domain.com
RECIPIENT_EMAILS=you@domain.com
CLAUDE_MODEL=claude-sonnet-4-5-20250929

# Optional: proxy for VPS if YouTube blocks your IP (leave blank for local)
# PROXY_URL=http://user:pass@proxy:port
```

### channels.yaml
```yaml
channels:
  - name: "Will Rich"
    channel_id: "UC5m7yO5V3D_0W1U6G5_7_7g"
```

## Main Flow (main.py)

### Phase 1: Load config
- Load `.env` with `python-dotenv`
- Load `channels.yaml` with `pyyaml`
- Load `seen_videos.json` state (create empty `{}` if doesn't exist)

### Phase 2: Check RSS feeds for new videos
- For each channel, fetch `https://www.youtube.com/feeds/videos.xml?channel_id=XXXX`
- Parse with `feedparser` - extract `entry.yt_videoid`, `entry.title`, `entry.published`, `entry.link`
- Compare against `seen_videos.json` - any video_id not in state is "new"
- If RSS returns empty/error, log warning and continue to next channel (don't crash)

### Phase 3: Fetch transcripts
- For each new video, use `youtube-transcript-api` v1.2.4:
  ```python
  from youtube_transcript_api import YouTubeTranscriptApi

  # With optional proxy support for VPS
  if proxy_url:
      from youtube_transcript_api.proxies import GenericProxyConfig
      ytt_api = YouTubeTranscriptApi(
          proxy_config=GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)
      )
  else:
      ytt_api = YouTubeTranscriptApi()

  transcript = ytt_api.fetch(video_id, languages=['en'])
  full_text = " ".join([snippet.text for snippet in transcript])
  ```
- Handle errors: `TranscriptsDisabled`, `NoTranscriptFound`, `IpBlocked`, `RequestBlocked`
- If transcript unavailable (video too new), mark as `"status": "pending"` in state for retry next run
- If transcript fetched, mark as `"status": "transcript_fetched"` and store the text

### Phase 4: Summarize with Claude API
- For each video with status `"transcript_fetched"` (includes retries from previous runs):
  ```python
  from anthropic import Anthropic
  client = Anthropic()  # uses ANTHROPIC_API_KEY env var
  response = client.messages.create(
      model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250929"),
      max_tokens=2000,
      messages=[{"role": "user", "content": prompt}]
  )
  ```
- **Claude Prompt** (key piece - engineered for structured stock analysis):
  ```
  You are a stock market analyst summarizing a YouTube video transcript.
  Analyze this transcript and provide a structured summary in JSON format.

  Return ONLY valid JSON with this structure:
  {
    "tldr": "2-3 sentence overall summary of the video's key message",
    "movers": [
      {
        "ticker": "AAPL",
        "company": "Apple Inc.",
        "direction": "up" or "down" or "neutral",
        "magnitude": "percentage or description if mentioned",
        "reason": "brief explanation of why it moved",
        "importance": "high" or "medium" or "low",
        "timeframe": "short-term" or "long-term" or "unclear"
      }
    ],
    "market_sentiment": "bullish" or "bearish" or "mixed" or "neutral",
    "key_themes": ["theme1", "theme2"],
    "actionable_takeaways": ["takeaway1", "takeaway2"]
  }

  If no specific stocks are mentioned, set movers to an empty array and
  focus on market themes and sentiment.

  Transcript:
  {transcript_text}
  ```
- Parse the JSON response. If Claude API fails, leave status as `"transcript_fetched"` so it retries next run.
- On success, mark as `"status": "summarized"` and store the parsed summary.

### Phase 5: Send combined email via SendGrid
- Collect all newly summarized videos from this run
- If none, exit (no email sent on days with no new content)
- Build HTML email:
  - **Subject**: `"Stock Video Digest - {date} ({n} new videos)"`
  - **TL;DR section** at top: combined bullet points from all videos
  - **Per-video sections**: channel name, video title (linked), movers table, themes, outlook
- Send via SendGrid using pattern from existing projects:
  ```python
  from sendgrid import SendGridAPIClient
  from sendgrid.helpers.mail import Mail

  message = Mail(
      from_email=SENDER_EMAIL,
      to_emails=recipient_list,
      subject=subject,
      html_content=html_body
  )
  sg = SendGridAPIClient(SENDGRID_API_KEY)
  response = sg.send(message)
  ```
- On success, mark videos as `"status": "emailed"`
- On failure, log error - summaries are preserved in state for manual review or next run

### Phase 6: Save state
- Write updated `seen_videos.json` to disk

## State File Format (seen_videos.json)

```json
{
  "VIDEO_ID_1": {
    "title": "Video Title",
    "channel": "Will Rich",
    "published": "2026-02-15T00:00:00Z",
    "url": "https://www.youtube.com/watch?v=VIDEO_ID_1",
    "status": "emailed",
    "transcript": "full transcript text...",
    "summary": { ... parsed JSON summary ... }
  },
  "VIDEO_ID_2": {
    "status": "pending"
  }
}
```

Status progression: `pending` -> `transcript_fetched` -> `summarized` -> `emailed`

Failed steps don't advance status, so next run retries from where it left off.

## HTML Email Template Structure

```
Subject: Stock Video Digest - Feb 15, 2026 (2 new videos)

[TL;DR - Quick Summary]
  - bullet point from video 1 tldr
  - bullet point from video 2 tldr

[Video 1: "Title" - Will Rich]
  [link to video]
  Market Sentiment: Bullish

  Movers:
  | Ticker | Direction | Move    | Reason          | Importance | Timeframe  |
  |--------|-----------|---------|-----------------|------------|------------|
  | AAPL   | Up        | +3.2%   | Strong earnings | High       | Short-term |
  | TSLA   | Down      | -1.5%   | Delivery miss   | Medium     | Long-term  |

  Key Themes: AI spending, earnings season
  Takeaways: Consider AAPL on pullbacks, watch TSLA support levels

[Video 2: ...]
```

## Error Handling Summary

| Failure Point | Behavior |
|--------------|----------|
| RSS feed 404/timeout | Log warning, skip channel, continue |
| Transcript not available yet | Mark `pending`, retry next run |
| IP blocked (VPS) | Log error with suggestion to configure PROXY_URL |
| Claude API error | Keep `transcript_fetched` status, retry next run |
| Claude returns invalid JSON | Log warning, store raw text as summary fallback |
| SendGrid error | Keep `summarized` status, log error, retry next run |
| No new videos found | Exit cleanly, no email sent |

## Files to Reuse/Reference

- **SendGrid pattern**: `/Users/gregsimek/Code/GymMolly_store/backend/sendemail.py`
- **Claude API pattern**: `/Users/gregsimek/Code/custservemail/src/ai_agent.py`
- **.env pattern**: `/Users/gregsimek/Code/Arcgis StPete RE/.env.example`

## Verification / Testing

1. **Local test (no new videos)**: Run `python main.py` with empty `seen_videos.json` - should find videos, fetch transcripts, summarize, and email
2. **Dry run**: Add a `--dry-run` flag that does everything except send the email (prints HTML to stdout instead)
3. **Test individual phases**: Run with `--skip-email` to test RSS + transcript + summarize without emailing
4. **VPS test**: Deploy to VPS, run manually first to check for IP blocking issues before setting up cron
5. **Cron setup**: `0 8 * * * cd /path/to/YTsummarizer && /usr/bin/python3 main.py >> cron.log 2>&1`
