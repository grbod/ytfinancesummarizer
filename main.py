#!/usr/bin/env python3
import argparse
import fcntl
import html
import json
import logging
import os
import re
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import feedparser
import yaml
from anthropic import Anthropic
from dotenv import load_dotenv
from youtube_transcript_api import YouTubeTranscriptApi

from cf_mail import email_provider, send_html_email

try:
    from youtube_transcript_api.proxies import GenericProxyConfig
except ImportError:  # pragma: no cover - depends on package version/features
    GenericProxyConfig = None

STATE_PENDING = "pending"
STATE_TRANSCRIPT_FETCHED = "transcript_fetched"
STATE_SUMMARIZED = "summarized"
STATE_EMAILED = "emailed"

DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
DEFAULT_TIMEZONE = "America/New_York"

LOGGER = logging.getLogger("yt_summarizer")


class RunLockError(RuntimeError):
    pass


class RunLock:
    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> "RunLock":
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise RunLockError(
                f"Another ytsummarizer run is already in progress (lock: {self.path})."
            ) from exc

        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        self._fd = fd
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and send a stock-focused daily digest from YouTube channels."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all phases except delivery. Prints email subject/body to stdout.",
    )
    parser.add_argument(
        "--skip-email",
        action="store_true",
        help="Run all phases but do not send an email.",
    )
    return parser.parse_args()


def load_channels(channels_path: Path) -> List[Dict[str, str]]:
    if not channels_path.exists():
        raise FileNotFoundError(
            f"Missing {channels_path}. Add channels before running."
        )

    with channels_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    channels = data.get("channels", [])
    if not isinstance(channels, list):
        raise ValueError("channels.yaml must contain a top-level `channels` list.")

    validated = []
    for idx, channel in enumerate(channels):
        if not isinstance(channel, dict):
            raise ValueError(f"channels[{idx}] must be an object.")
        name = str(channel.get("name", "")).strip()
        channel_id = str(channel.get("channel_id", "")).strip()
        if not name or not channel_id:
            raise ValueError(
                f"channels[{idx}] requires non-empty `name` and `channel_id`."
            )
        validated.append({"name": name, "channel_id": channel_id})
    return validated


def load_state(state_path: Path) -> Dict[str, Dict[str, Any]]:
    if not state_path.exists():
        return {}

    try:
        with state_path.open("r", encoding="utf-8") as f:
            raw_state = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {state_path}: {exc}") from exc

    if not isinstance(raw_state, dict):
        raise ValueError(f"{state_path} must contain a top-level object.")

    state: Dict[str, Dict[str, Any]] = {}
    for video_id, item in raw_state.items():
        if isinstance(item, dict):
            state[str(video_id)] = item
        else:
            state[str(video_id)] = {"status": STATE_PENDING}
    return state


def save_state(state_path: Path, state: Dict[str, Dict[str, Any]]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f"{state_path.name}.",
        suffix=".tmp",
        dir=str(state_path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, state_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def get_local_now() -> datetime:
    tz_name = (os.getenv("DIGEST_TIMEZONE", DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE).strip()
    try:
        tzinfo = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        LOGGER.warning(
            "Invalid DIGEST_TIMEZONE '%s'; defaulting to %s.",
            tz_name,
            DEFAULT_TIMEZONE,
        )
        tzinfo = ZoneInfo(DEFAULT_TIMEZONE)
    return datetime.now(tzinfo)


def extract_video_id(entry: Dict[str, Any]) -> str:
    video_id = str(entry.get("yt_videoid", "")).strip()
    if video_id:
        return video_id

    link = str(entry.get("link", "")).strip()
    if not link:
        return ""

    parsed = urlparse(link)
    query = parse_qs(parsed.query)
    values = query.get("v")
    if values:
        return values[0].strip()
    return ""


def discover_new_videos(
    channels: List[Dict[str, str]],
    state: Dict[str, Dict[str, Any]],
    errors: List[str],
) -> List[str]:
    discovered_ids: List[str] = []
    for channel in channels:
        channel_name = channel["name"]
        channel_id = channel["channel_id"]
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        LOGGER.info("Checking RSS feed: %s", channel_name)

        try:
            feed = feedparser.parse(url)
        except Exception:
            errors.append(f"RSS parse failure for {channel_name} ({channel_id}).")
            LOGGER.warning(
                "Failed to parse feed for %s (%s). Skipping.",
                channel_name,
                channel_id,
                exc_info=True,
            )
            continue

        feed_status = getattr(feed, "status", None)
        if isinstance(feed_status, int) and feed_status >= 400:
            errors.append(
                f"RSS request failed for {channel_name} ({channel_id}) with status {feed_status}."
            )
            LOGGER.warning(
                "Feed request failed for %s with status %s. Skipping.",
                channel_name,
                feed_status,
            )
            continue

        if getattr(feed, "bozo", False):
            LOGGER.warning(
                "Feed warning for %s: %s", channel_name, getattr(feed, "bozo_exception", "")
            )

        entries = getattr(feed, "entries", []) or []
        if not entries:
            LOGGER.warning("No entries returned for %s.", channel_name)
            continue

        for entry in entries:
            video_id = extract_video_id(entry)
            if not video_id:
                LOGGER.warning("Skipping entry without a usable video ID: %s", entry)
                continue

            metadata = {
                "title": str(entry.get("title", "")).strip(),
                "channel": channel_name,
                "published": str(entry.get("published", "")).strip(),
                "url": str(entry.get("link", "")).strip(),
            }

            if video_id not in state:
                state[video_id] = {**metadata, "status": STATE_PENDING}
                discovered_ids.append(video_id)
                continue

            # Keep metadata current without overwriting transcript/summary state.
            state[video_id].update(metadata)
    return discovered_ids


def build_ytt_api(proxy_url: str) -> YouTubeTranscriptApi:
    proxy_url = proxy_url.strip()
    if proxy_url:
        if GenericProxyConfig is None:
            raise RuntimeError(
                "PROXY_URL is set but youtube-transcript-api proxy support is unavailable."
            )
        return YouTubeTranscriptApi(
            proxy_config=GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)
        )
    return YouTubeTranscriptApi()


def fetch_transcript_for_video(
    ytt_api: YouTubeTranscriptApi,
    video_id: str,
    item: Dict[str, Any],
    errors: List[str],
) -> bool:
    try:
        transcript = ytt_api.fetch(video_id, languages=["en"])
        full_text = " ".join(snippet.text for snippet in transcript).strip()
        if not full_text:
            raise ValueError("Transcript returned no text.")
        item["transcript"] = full_text
        item["status"] = STATE_TRANSCRIPT_FETCHED
        return True
    except Exception as exc:  # library has multiple runtime exception classes
        error_name = exc.__class__.__name__
        error_msg = str(exc)
        errors.append(
            f"Transcript fetch failed for {video_id} ({error_name}): {error_msg or 'no detail'}"
        )
        if error_name in {"IpBlocked", "RequestBlocked"}:
            LOGGER.error(
                "Transcript request blocked for %s (%s). Configure PROXY_URL if needed.",
                video_id,
                error_name,
            )
        elif error_name in {"TranscriptsDisabled", "NoTranscriptFound"}:
            LOGGER.info(
                "Transcript unavailable for %s (%s). Will retry later.",
                video_id,
                error_name,
            )
        else:
            LOGGER.warning(
                "Transcript fetch failed for %s (%s): %s",
                video_id,
                error_name,
                error_msg,
            )
        item["status"] = STATE_PENDING
        return False


def build_summary_prompt(transcript_text: str) -> str:
    return f"""You are a buy-side tech equities analyst.

Task:
Summarize this YouTube transcript into structured JSON, focusing ONLY on:
1) AI / robotics / semis / data center / cloud / cyber / defense-tech / space-tech tickers
2) Material macro signals that affect those sectors (rates, CPI/PPI, jobs, yields, energy, capex trends)

Strict exclusion rules (do NOT include in output):
- Course/software promotions, special offers, pricing, discounts, community memberships
- Creator self-promotion or tool marketing (e.g., "limited-time", "join", "lifetime access")
- Generic trading psychology, cautionary tales, discipline reminders, stop-loss lectures
- Generic market platitudes without concrete ticker/macro signal

Quality bar:
- Include only concrete, evidence-backed claims from transcript.
- Prefer facts with numbers (%, revenue, contract size, guidance, valuation, dates).
- If a ticker is mentioned without a clear catalyst, omit it.
- If content is mostly promotional/noise, return empty movers and concise macro/themes only if truly present.

Return ONLY valid JSON with this exact structure:
{{
  "tldr": "Max 2 sentences. Only concrete AI/robotics/tech + macro signal summary.",
  "movers": [
    {{
      "ticker": "AAPL",
      "company": "Apple Inc.",
      "direction": "up" or "down" or "neutral",
      "magnitude": "percentage or specific move if mentioned",
      "reason": "specific catalyst with evidence from transcript",
      "importance": "high" or "medium" or "low",
      "timeframe": "short-term" or "long-term" or "unclear"
    }}
  ],
  "market_sentiment": "bullish" or "bearish" or "mixed" or "neutral",
  "key_themes": ["max 5 specific, non-generic themes"],
  "actionable_takeaways": ["max 4 specific takeaways tied to a ticker or macro signal"]
}}

Additional constraints:
- Do not mention promotions/offers/tools unless they materially affect a public ticker (rare).
- Do not include generic "manage risk" advice.
- Keep output concise and specific.

Transcript:
{transcript_text}
"""


def extract_text_from_claude_response(response: Any) -> str:
    chunks: List[str] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "text":
            chunks.append(getattr(block, "text", ""))
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def try_parse_json_payload(raw_text: str) -> Tuple[Any, bool]:
    text = raw_text.strip()
    if not text:
        return None, False

    fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    try:
        return json.loads(text), True
    except json.JSONDecodeError:
        pass

    object_match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if object_match:
        candidate = object_match.group(0)
        try:
            return json.loads(candidate), True
        except json.JSONDecodeError:
            return None, False
    return None, False


def summarize_video(
    client: Anthropic,
    model: str,
    video_id: str,
    item: Dict[str, Any],
    errors: List[str],
) -> bool:
    transcript = str(item.get("transcript", "")).strip()
    if not transcript:
        LOGGER.warning("Skipping summary for %s: transcript is empty.", video_id)
        errors.append(f"Summary skipped for {video_id}: transcript is empty.")
        item["status"] = STATE_PENDING
        return False

    try:
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            messages=[{"role": "user", "content": build_summary_prompt(transcript)}],
        )
    except Exception:
        LOGGER.error("Claude API call failed for %s. Keeping for retry.", video_id, exc_info=True)
        errors.append(f"Claude API call failed for {video_id}.")
        item["status"] = STATE_TRANSCRIPT_FETCHED
        return False

    raw_text = extract_text_from_claude_response(response)
    parsed, ok = try_parse_json_payload(raw_text)
    if ok and isinstance(parsed, dict):
        item["summary"] = parsed
    else:
        LOGGER.warning(
            "Claude returned non-JSON summary for %s. Storing raw response fallback.",
            video_id,
        )
        errors.append(f"Claude returned non-JSON payload for {video_id}.")
        item["summary"] = {"raw_response": raw_text}

    item["status"] = STATE_SUMMARIZED
    return True


def _escape(value: Any) -> str:
    return html.escape(str(value or ""))


def _list_to_html_items(values: Any) -> str:
    if not isinstance(values, list) or not values:
        return "<li>None</li>"
    return "".join(f"<li>{_escape(v)}</li>" for v in values)


def _movers_rows(summary: Dict[str, Any]) -> str:
    movers = summary.get("movers")
    if not isinstance(movers, list) or not movers:
        return (
            "<tr><td colspan='6' style='padding:8px;border:1px solid #ddd;'>"
            "No specific movers identified"
            "</td></tr>"
        )

    rows = []
    for mover in movers:
        if not isinstance(mover, dict):
            continue
        rows.append(
            "<tr>"
            f"<td style='padding:8px;border:1px solid #ddd;'>{_escape(mover.get('ticker'))}</td>"
            f"<td style='padding:8px;border:1px solid #ddd;'>{_escape(mover.get('direction'))}</td>"
            f"<td style='padding:8px;border:1px solid #ddd;'>{_escape(mover.get('magnitude'))}</td>"
            f"<td style='padding:8px;border:1px solid #ddd;'>{_escape(mover.get('reason'))}</td>"
            f"<td style='padding:8px;border:1px solid #ddd;'>{_escape(mover.get('importance'))}</td>"
            f"<td style='padding:8px;border:1px solid #ddd;'>{_escape(mover.get('timeframe'))}</td>"
            "</tr>"
        )
    if not rows:
        return (
            "<tr><td colspan='6' style='padding:8px;border:1px solid #ddd;'>"
            "No specific movers identified"
            "</td></tr>"
        )
    return "".join(rows)


def build_email_digest(
    video_ids: List[str], state: Dict[str, Dict[str, Any]]
) -> Tuple[str, str]:
    local_now = get_local_now()
    date_label = local_now.strftime("%b %d, %Y")
    tz_label = local_now.strftime("%Z")
    subject = f"Stock Video Digest - {date_label} ({len(video_ids)} new videos)"

    tldr_items = []
    sections = []

    for video_id in video_ids:
        item = state[video_id]
        summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}

        tldr = summary.get("tldr") if isinstance(summary, dict) else None
        if isinstance(tldr, str) and tldr.strip():
            tldr_items.append(f"<li>{_escape(tldr.strip())}</li>")
        else:
            raw_preview = str(summary.get("raw_response", "")).strip()[:220]
            if raw_preview:
                tldr_items.append(f"<li>{_escape(raw_preview)}...</li>")
            else:
                tldr_items.append(f"<li>{_escape(item.get('title', 'Untitled video'))}</li>")

        sentiment = (
            summary.get("market_sentiment", "unknown")
            if isinstance(summary, dict)
            else "unknown"
        )
        themes_html = _list_to_html_items(summary.get("key_themes") if isinstance(summary, dict) else [])
        takeaways_html = _list_to_html_items(
            summary.get("actionable_takeaways") if isinstance(summary, dict) else []
        )
        movers_html = _movers_rows(summary if isinstance(summary, dict) else {})
        url = item.get("url", "")

        sections.append(
            f"""
            <section style="margin:28px 0;padding:18px;border:1px solid #ddd;border-radius:8px;">
              <h2 style="margin:0 0 6px 0;">{_escape(item.get("title", "Untitled"))}</h2>
              <div style="margin-bottom:10px;color:#555;">
                Channel: {_escape(item.get("channel", "Unknown"))}
              </div>
              <div style="margin-bottom:10px;">
                <a href="{_escape(url)}" target="_blank" rel="noopener noreferrer">Watch video</a>
              </div>
              <div style="margin-bottom:12px;">
                <strong>Market Sentiment:</strong> {_escape(sentiment)}
              </div>
              <table style="border-collapse:collapse;width:100%;margin:10px 0 14px 0;">
                <thead>
                  <tr>
                    <th style="text-align:left;padding:8px;border:1px solid #ddd;">Ticker</th>
                    <th style="text-align:left;padding:8px;border:1px solid #ddd;">Direction</th>
                    <th style="text-align:left;padding:8px;border:1px solid #ddd;">Move</th>
                    <th style="text-align:left;padding:8px;border:1px solid #ddd;">Reason</th>
                    <th style="text-align:left;padding:8px;border:1px solid #ddd;">Importance</th>
                    <th style="text-align:left;padding:8px;border:1px solid #ddd;">Timeframe</th>
                  </tr>
                </thead>
                <tbody>{movers_html}</tbody>
              </table>
              <div>
                <strong>Key Themes</strong>
                <ul>{themes_html}</ul>
              </div>
              <div>
                <strong>Actionable Takeaways</strong>
                <ul>{takeaways_html}</ul>
              </div>
            </section>
            """
        )

    body = f"""
    <html>
      <body style="font-family:Arial,Helvetica,sans-serif;max-width:900px;margin:0 auto;padding:24px;line-height:1.45;">
        <h1>Stock Video Digest</h1>
        <p style="color:#555;">Generated {date_label} ({tz_label})</p>
        <section style="margin:18px 0;padding:16px;background:#f7f7f7;border-radius:8px;">
          <h2 style="margin-top:0;">TL;DR - Quick Summary</h2>
          <ul>{"".join(tldr_items)}</ul>
        </section>
        {"".join(sections)}
      </body>
    </html>
    """
    return subject, body


def parse_recipient_emails(raw: str) -> List[str]:
    if not raw:
        return []
    parts = re.split(r"[;,]", raw)
    return [part.strip() for part in parts if part.strip()]


def send_digest_email(
    sender_email: str, recipient_emails: List[str], subject: str, html_body: str
) -> None:
    """Raises on failure. Provider (Cloudflare or SendGrid fallback) is chosen in cf_mail."""
    send_html_email(sender_email, recipient_emails, subject, html_body)


def send_error_alert_email(errors: List[str]) -> None:
    if not errors:
        return

    sender_email = os.getenv("SENDER_EMAIL", "").strip()
    alert_recipients_raw = os.getenv("ALERT_RECIPIENT_EMAILS", "").strip()
    recipient_emails = parse_recipient_emails(
        alert_recipients_raw or os.getenv("RECIPIENT_EMAILS", "")
    )

    if not email_provider() or not sender_email or not recipient_emails:
        LOGGER.error(
            "Unable to send error alert email. Required config missing (CLOUDFLARE_ACCOUNT_ID and "
            "CLOUDFLARE_EMAIL_API_TOKEN, or SENDGRID_API_KEY; SENDER_EMAIL; "
            "RECIPIENT_EMAILS/ALERT_RECIPIENT_EMAILS)."
        )
        return

    local_now = get_local_now()
    subject = (
        f"YTsummarizer Failure Alert - {local_now.strftime('%b %d, %Y %I:%M %p %Z')} "
        f"({len(errors)} issue(s))"
    )
    rendered_errors = "".join(
        f"<li>{_escape(error_text)}</li>" for error_text in errors[:100]
    )
    html_body = f"""
    <html>
      <body style="font-family:Arial,Helvetica,sans-serif;max-width:800px;margin:0 auto;padding:24px;line-height:1.45;">
        <h1>YTsummarizer Failure Alert</h1>
        <p>Detected {len(errors)} issue(s) in the latest run.</p>
        <ul>{rendered_errors}</ul>
      </body>
    </html>
    """

    try:
        send_digest_email(
            sender_email=sender_email,
            recipient_emails=recipient_emails,
            subject=subject,
            html_body=html_body,
        )
        LOGGER.info("Sent failure alert email with %d issue(s).", len(errors))
    except Exception:
        LOGGER.error("Failed to send failure alert email.", exc_info=True)


def run_pipeline(args: argparse.Namespace, project_root: Path, errors: List[str]) -> int:
    channels_path = project_root / "channels.yaml"
    state_path = project_root / "seen_videos.json"

    try:
        channels = load_channels(channels_path)
        state = load_state(state_path)
    except Exception as exc:
        LOGGER.error("Startup failure: %s", exc)
        errors.append(f"Startup failure: {exc}")
        return 1

    discovered = discover_new_videos(channels, state, errors)
    LOGGER.info("Discovered %d new videos.", len(discovered))

    proxy_url = os.getenv("PROXY_URL", "")
    try:
        ytt_api = build_ytt_api(proxy_url)
    except Exception as exc:
        LOGGER.error("Failed to initialize transcript API: %s", exc)
        errors.append(f"Transcript API initialization failed: {exc}")
        save_state(state_path, state)
        return 1

    pending_ids = [vid for vid, item in state.items() if item.get("status") == STATE_PENDING]
    LOGGER.info("Attempting transcript fetch for %d videos.", len(pending_ids))
    for video_id in pending_ids:
        fetch_transcript_for_video(ytt_api, video_id, state[video_id], errors)

    transcript_ready_ids = [
        vid
        for vid, item in state.items()
        if item.get("status") == STATE_TRANSCRIPT_FETCHED and item.get("transcript")
    ]
    carryover_summarized_ids = [
        vid for vid, item in state.items() if item.get("status") == STATE_SUMMARIZED
    ]
    summarized_this_run: List[str] = []
    LOGGER.info("Attempting summaries for %d videos.", len(transcript_ready_ids))

    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if transcript_ready_ids and not anthropic_api_key:
        error_msg = "ANTHROPIC_API_KEY is required to summarize transcripts."
        LOGGER.error(error_msg)
        errors.append(error_msg)
    else:
        if transcript_ready_ids:
            client = Anthropic(api_key=anthropic_api_key)
            model = os.getenv("CLAUDE_MODEL", DEFAULT_MODEL)
            for video_id in transcript_ready_ids:
                if summarize_video(client, model, video_id, state[video_id], errors):
                    summarized_this_run.append(video_id)

    # Include videos summarized this run plus earlier unsent summaries retained for retry.
    videos_to_email = list(dict.fromkeys(carryover_summarized_ids + summarized_this_run))
    if not videos_to_email:
        LOGGER.info("No summarized videos to send. Exiting cleanly.")
        save_state(state_path, state)
        return 0

    LOGGER.info(
        "Prepared %d summarized videos for email (%d new this run, %d carryover).",
        len(videos_to_email),
        len(summarized_this_run),
        len(carryover_summarized_ids),
    )
    subject, html_body = build_email_digest(videos_to_email, state)

    if args.dry_run:
        print(subject)
        print(html_body)
        LOGGER.info("Dry run complete. Email not sent.")
        save_state(state_path, state)
        return 0

    if args.skip_email:
        LOGGER.info("Skipping email delivery due to --skip-email.")
        save_state(state_path, state)
        return 0

    sender_email = os.getenv("SENDER_EMAIL", "").strip()
    recipient_emails = parse_recipient_emails(os.getenv("RECIPIENT_EMAILS", ""))

    if not email_provider() or not sender_email or not recipient_emails:
        error_msg = (
            "Cloudflare (CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_EMAIL_API_TOKEN) or SENDGRID_API_KEY, "
            "SENDER_EMAIL, and RECIPIENT_EMAILS are required to send email."
        )
        LOGGER.error(error_msg)
        errors.append(error_msg)
        save_state(state_path, state)
        return 1

    try:
        send_digest_email(
            sender_email=sender_email,
            recipient_emails=recipient_emails,
            subject=subject,
            html_body=html_body,
        )
        for video_id in videos_to_email:
            state[video_id]["status"] = STATE_EMAILED
    except Exception as exc:
        errors.append(f"Digest email delivery failed: {exc}")
        LOGGER.error(
            "Email delivery failed. Summaries remain in state for retry.", exc_info=True
        )

    save_state(state_path, state)
    return 0


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    project_root = Path(__file__).resolve().parent
    env_path = project_root / ".env"
    load_dotenv(env_path if env_path.exists() else None)

    errors: List[str] = []
    exit_code = 0
    lock_path = project_root / ".ytsummarizer.lock"

    try:
        with RunLock(lock_path):
            exit_code = run_pipeline(args, project_root, errors)
    except RunLockError as exc:
        LOGGER.warning("%s", exc)
        return 0
    except Exception:
        LOGGER.exception("Unhandled exception during run.")
        errors.append(
            "Unhandled exception: "
            + traceback.format_exc(limit=20).strip().replace("\n", " | ")
        )
        exit_code = 1

    if errors:
        send_error_alert_email(errors)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
