"""Email delivery for YTsummarizer: Cloudflare Email Service, with the SendGrid SDK kept as a rollback path.

send_html_email raises RuntimeError on any failure. Cloudflare is used when CLOUDFLARE_ACCOUNT_ID and
CLOUDFLARE_EMAIL_API_TOKEN are both set, else SendGrid (SENDGRID_API_KEY). The token and request bodies are
never logged or placed in error text.
"""
import logging
import os
from typing import Any, Dict, List
from urllib.parse import quote

import requests

LOGGER = logging.getLogger("yt_summarizer")
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
TIMEOUT_SECONDS = 20


def email_provider() -> str:
    """Return 'cloudflare', 'sendgrid' or '' (nothing configured)."""
    if os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip() and os.getenv("CLOUDFLARE_EMAIL_API_TOKEN", "").strip():
        return "cloudflare"
    if os.getenv("SENDGRID_API_KEY", "").strip():
        return "sendgrid"
    return ""


def send_cloudflare(
    account_id: str,
    token: str,
    sender: str,
    recipients: List[str],
    subject: str,
    html_body: str,
    timeout: float = TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    def redact(text: str) -> str:
        return text.replace(token, "[redacted]")

    url = f"{CLOUDFLARE_API}/accounts/{quote(account_id, safe='')}/email/sending/send"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"to": recipients, "from": sender, "subject": subject, "html": html_body},
            timeout=timeout,
        )
    except requests.Timeout:
        raise RuntimeError(f"Cloudflare Email Service request timed out after {timeout}s") from None
    except requests.RequestException as exc:
        raise RuntimeError(f"Cloudflare Email Service request failed: {redact(str(exc))[:300]}") from None

    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise RuntimeError(f"Cloudflare Email Service returned an unreadable body (HTTP {resp.status_code})")
    if not 200 <= resp.status_code < 300 or payload.get("success") is not True:
        errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
        detail = "; ".join(
            f"{e.get('code', '?')} {e.get('message', '')}".strip() for e in errors if isinstance(e, dict)
        )
        raise RuntimeError(
            f"Cloudflare Email Service send failed: HTTP {resp.status_code}"
            + (f" ({redact(detail)[:300]})" if detail else "")
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Cloudflare Email Service returned no result")

    accepted = {a.lower() for key in ("delivered", "queued") for a in (result.get(key) or []) if isinstance(a, str)}
    LOGGER.info(
        "Cloudflare email message_id=%s permanent_bounces=%s suppressed_recipients=%s",
        result.get("message_id"),
        result.get("permanent_bounces") or [],
        result.get("suppressed_recipients") or [],
    )
    missing = [a for a in recipients if a.lower() not in accepted]
    if missing:
        raise RuntimeError(f"Cloudflare Email Service did not deliver or queue: {', '.join(missing)}")
    return result


def _send_sendgrid(api_key: str, sender: str, recipients: List[str], subject: str, html_body: str) -> None:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    message = Mail(from_email=sender, to_emails=recipients, subject=subject, html_content=html_body)
    response = SendGridAPIClient(api_key).send(message)
    status_code = getattr(response, "status_code", None)
    LOGGER.info("SendGrid response: status=%s", status_code if status_code is not None else "unknown")
    if status_code is None or status_code >= 400:
        raise RuntimeError(f"SendGrid returned non-success status: {status_code}")


def send_html_email(sender: str, recipients: List[str], subject: str, html_body: str) -> None:
    """Send one HTML email to every recipient. Raises RuntimeError unless it was accepted for all of them."""
    if not recipients:
        raise RuntimeError("No recipients")
    provider = email_provider()
    if provider == "cloudflare":
        send_cloudflare(
            os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip(),
            os.getenv("CLOUDFLARE_EMAIL_API_TOKEN", "").strip(),
            sender,
            recipients,
            subject,
            html_body,
        )
    elif provider == "sendgrid":
        _send_sendgrid(os.getenv("SENDGRID_API_KEY", "").strip(), sender, recipients, subject, html_body)
    else:
        raise RuntimeError("No email provider configured")
