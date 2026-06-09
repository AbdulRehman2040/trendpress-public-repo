"""Operational digest notifications: email (SMTP) and/or Telegram.

After each run the orchestrator builds a ``digest`` dict and calls
``send_digest``. The plain-text summary is emailed (if SMTP_* + ADMIN_MAIL are
set) and/or sent to Telegram (if TELEGRAM_* are set), and is ALWAYS written to
the log so it is never lost.

digest = {
    "title": str,
    "posts": [{"site", "site_name", "title", "status", "url", "edit_url"}],
    "errors": [str],            # per-site publish failures
    "skipped_trends": [str],    # trends that matched no site / were not published
    "missing_images": [str],    # packages that got no featured image
    "paused_sites": [str],      # sites flagged paused by the kill-switch
}
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE = 4096  # Telegram hard limit per message


def send_digest(digest: dict, settings: dict | None = None) -> None:
    """Render the digest and deliver it via email and/or Telegram; always log it."""
    text = format_digest(digest)
    subject = digest.get("title", "trendpress digest")
    sent_via = []
    if _send_email(text, subject):
        sent_via.append(f"email->{os.environ.get('ADMIN_MAIL')}")
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id and _send_telegram(token, chat_id, text):
        sent_via.append("telegram")
    suffix = f" (sent via {', '.join(sent_via)})" if sent_via else ""
    logger.info("DIGEST%s\n%s", suffix, text)


def format_digest(digest: dict) -> str:
    """Render the digest as plain text (with live + edit links per post)."""
    posts = digest.get("posts", [])
    lines = [digest.get("title", "trendpress run")]

    lines.append(f"\nPosts ({len(posts)}):")
    if posts:
        for p in posts:
            label = p.get("site_name") or p.get("site", "?")
            lines.append(f"- [{label}] {p.get('title','(untitled)')} — {p.get('status','?')}")
            if p.get("url"):
                lines.append(f"    url:  {p['url']}")
            if p.get("edit_url"):
                lines.append(f"    edit: {p['edit_url']}")
    else:
        lines.append("- (none)")

    for label, key in (("Errors", "errors"), ("Skipped trends", "skipped_trends"),
                       ("Missing images", "missing_images"), ("Paused sites", "paused_sites")):
        items = digest.get(key) or []
        if items:
            lines.append(f"\n{label} ({len(items)}):")
            lines.extend(f"- {item}" for item in items)

    return "\n".join(lines)


def _send_email(text: str, subject: str) -> bool:
    """Email the digest via SMTP if SMTP_* + ADMIN_MAIL are configured."""
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    recipient = os.environ.get("ADMIN_MAIL")
    if not (host and user and password and recipient):
        return False
    port = int(os.environ.get("SMTP_PORT", "465") or 465)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient
    msg.set_content(text)
    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as server:
                server.login(user, password)
                server.send_message(msg)
        else:  # 587 / STARTTLS
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.ehlo()
                server.starttls(context=context)
                server.login(user, password)
                server.send_message(msg)
        return True
    except Exception as exc:  # never let a mail failure break the run
        logger.warning("notify: email send failed (%s)", exc)
        return False


def _send_telegram(token: str, chat_id: str, text: str) -> bool:
    """POST the message to Telegram; return True on success."""
    try:
        resp = requests.post(
            TELEGRAM_API.format(token=token),
            json={"chat_id": chat_id, "text": text[:MAX_MESSAGE],
                  "disable_web_page_preview": True},
            timeout=20,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:  # network / bad token — fall back to logging
        logger.warning("notify: Telegram send failed (%s)", exc)
        return False
