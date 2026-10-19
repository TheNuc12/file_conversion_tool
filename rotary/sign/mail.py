"""Outgoing email: SMTP when configured, otherwise .eml files in <data>/outbox."""

from __future__ import annotations

import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from html import escape
from pathlib import Path

from .config import Config


class MailError(RuntimeError):
    pass


def _html(organisation: str, heading: str, paragraphs: list[str], button: tuple[str, str] | None) -> str:
    body = "".join(f'<p style="margin:0 0 14px;line-height:1.5">{p}</p>' for p in paragraphs)
    if button:
        label, url = button
        body += (
            f'<p style="margin:22px 0"><a href="{escape(url)}" style="background:#d9481f;color:#fff;'
            f'text-decoration:none;padding:12px 22px;border-radius:8px;font-weight:600;display:inline-block">{escape(label)}</a></p>'
            f'<p style="margin:0 0 14px;font-size:12px;color:#777">If the button doesn\'t work, paste this link into your browser:<br>{escape(url)}</p>'
        )
    return (
        '<!doctype html><html><body style="margin:0;background:#f2efe9;font-family:Segoe UI,Arial,sans-serif;color:#1c1b19">'
        '<div style="max-width:560px;margin:0 auto;padding:28px 16px">'
        f'<div style="font-size:13px;color:#6a665e;margin-bottom:10px">{escape(organisation)} · Rotary Sign</div>'
        '<div style="background:#fff;border:1px solid #dbd6cb;border-radius:12px;padding:24px">'
        f'<h1 style="font-size:20px;margin:0 0 16px">{escape(heading)}</h1>{body}</div>'
        '<p style="font-size:11px;color:#a29d92;margin-top:16px">Sent by the document signing system. '
        "Don't forward this email: its link lets anyone sign in your name.</p>"
        "</div></body></html>"
    )


def compose(
    cfg: Config,
    to: tuple[str, str],
    subject: str,
    heading: str,
    paragraphs: list[str],
    button: tuple[str, str] | None = None,
    attachment: tuple[str, bytes] | None = None,
) -> EmailMessage:
    """``paragraphs`` are plain text; they're escaped for the HTML version."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((f"{cfg.organisation} (Rotary Sign)", cfg.sender_address))
    msg["To"] = formataddr(to)
    msg["Message-ID"] = make_msgid(domain=cfg.sender_address.split("@")[-1] or "localhost")
    text = "\n\n".join(paragraphs)
    if button:
        text += f"\n\n{button[0]}: {button[1]}"
    msg.set_content(text + "\n")
    msg.add_alternative(_html(cfg.organisation, heading, [escape(p).replace("\n", "<br>") for p in paragraphs], button), subtype="html")
    if attachment:
        name, data = attachment
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg


def send(cfg: Config, msg: EmailMessage) -> str:
    """Deliver ``msg``. Returns a short description of where it went; raises MailError on failure."""
    if not cfg.smtp_host:
        outbox = Path(cfg.data) / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        # Named so that sorting the folder lists messages in the order they were sent.
        path = outbox / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 10**9:09d}-{secrets.token_hex(2)}.eml"
        path.write_bytes(bytes(msg))
        return f"saved to outbox ({path.name})"
    try:
        if cfg.smtp_security == "ssl":
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
        with server:
            if cfg.smtp_security == "starttls":
                server.starttls(context=ssl.create_default_context())
            if cfg.smtp_user:
                server.login(cfg.smtp_user, cfg.smtp_password)
            server.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(f"couldn't send email to {msg['To']}: {exc}") from exc
    return "sent"
