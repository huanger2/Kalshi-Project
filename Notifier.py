"""
Notifier.py

Sends an email alert whenever an AI API call fails because the account is
out of money -- quota exhausted, credit balance too low, billing not
enabled, etc. -- so that failure doesn't just scroll by in a terminal
nobody is watching (e.g. an overnight scheduled run).

Credentials live in EmailAlerts.env, NOT AIAPIKEYS.env, loaded lazily so
importing this module never needs them:

    EMAIL_ADDRESS       the Gmail account the alert is sent from
    EMAIL_APP_PASSWORD  a Gmail App Password (not your normal password --
                        needs 2-Step Verification on; generate one at
                        https://myaccount.google.com/apppasswords)
    ALERT_EMAIL_TO      who receives the alert (defaults to EMAIL_ADDRESS)
    SMTP_HOST / SMTP_PORT   default to smtp.gmail.com:587
"""

import os
import smtplib
from email.message import EmailMessage

from dotenv import load_dotenv

ENV_FILE = "EmailAlerts.env"

# Phrases that show up in real "you're out of money" errors from OpenAI,
# Anthropic and Gemini. Deliberately does NOT include generic "rate limit"
# wording -- a plain 429 is often just short-term throttling, not billing.
_FUNDS_KEYWORDS = [
    "insufficient_quota",
    "insufficient quota",
    "insufficient funds",
    "insufficient credit",
    "credit balance",
    "exceeded your current quota",
    "exceeded your quota",
    "billing details",
    "billing to be enabled",
    "check your plan and billing",
    "payment required",
    "payment method",
    "resource_exhausted",
    "quota exceeded",
    "plans & billing",
]

# LLM names already alerted on in this process, so one run doesn't send the
# same alert once per market queried. Starts empty again on every restart.
_notified: set[str] = set()


def is_insufficient_funds_error(exc: BaseException) -> bool:
    """True if `exc` looks like a billing/quota failure rather than some other error."""
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status == 402:  # HTTP Payment Required
        return True
    message = str(exc).lower()
    return any(keyword in message for keyword in _FUNDS_KEYWORDS)


def send_email(subject: str, body: str, to: str | None = None) -> bool:
    """
    Send an email through the SMTP server configured in EmailAlerts.env.

    Returns True on success. Never raises -- a broken alert shouldn't crash
    the pipeline it's trying to warn about; failures are printed instead.
    """
    # override=True: EmailAlerts.env must win over a stale OS-level env var of
    # the same name, which load_dotenv() otherwise silently leaves in place.
    load_dotenv(ENV_FILE, override=True)
    # .strip(): trailing whitespace/newlines (a stray OS env var, or a
    # copy-paste artifact in the .env file) would otherwise send a wrong
    # password or recipient address without any obvious error.
    sender = (os.getenv("EMAIL_ADDRESS") or "").strip() or None
    password = (os.getenv("EMAIL_APP_PASSWORD") or "").strip() or None
    recipient = to or (os.getenv("ALERT_EMAIL_TO") or "").strip() or sender
    host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    port = int(os.getenv("SMTP_PORT", "587").strip())

    if not sender or not password or not recipient:
        print(f"[Notifier] can't send alert -- {ENV_FILE} is missing EMAIL_ADDRESS / "
              f"EMAIL_APP_PASSWORD / ALERT_EMAIL_TO")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(sender, password)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"[Notifier] failed to send alert email: {e}")
        return False


def notify_insufficient_funds(llm_name: str, error: BaseException, context: str = "") -> None:
    """
    Email an alert that `llm_name`'s API call looks like a billing/quota
    failure. Sent once per LLM per process run -- a daily script that
    queries many markets would otherwise send one email per market.

    Args:
        llm_name: which AI's API call failed (e.g. "OpenAI").
        error:    the exception that was raised.
        context:  optional extra detail (e.g. the market title being queried).
    """
    if llm_name in _notified:
        return
    _notified.add(llm_name)

    subject = f"[NBA Betting] {llm_name} API is out of funds"
    body = f"{llm_name}'s API call failed with what looks like a billing/quota error.\n\nError: {error}\n"
    if context:
        body += f"\nContext: {context}\n"

    send_email(subject, body)
