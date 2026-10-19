"""``rotary-sign``: run and manage the signing server.

    rotary-sign run --host 0.0.0.0 --port 8080      start the server
    rotary-sign add-user you@company.com --name "You" --admin
    rotary-sign check                               show settings, test email and the seal
    rotary-sign verify signed.pdf                   check a document's seal offline
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import smtplib
import ssl
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rotary-sign", description="Rotary Sign: your company's document signing server.")
    parser.add_argument("--data", help="data folder (default: $ROTARY_SIGN_DATA or ./rotary-sign-data)")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start the server")
    run.add_argument("--host", default="127.0.0.1", help="interface to listen on; 0.0.0.0 for all (default 127.0.0.1)")
    run.add_argument("--port", type=int, default=8080)
    run.add_argument("--threads", type=int, default=8)

    add = sub.add_parser("add-user", help="create an account for someone who sends documents")
    add.add_argument("email")
    add.add_argument("--name", required=True)
    add.add_argument("--admin", action="store_true")

    sub.add_parser("check", help="show the settings in use, test the email server and the seal")
    verify = sub.add_parser("verify", help="check a signed PDF's seal")
    verify.add_argument("pdf")
    args = parser.parse_args(argv)

    from .config import load

    cfg = load(args.data)
    if args.command == "run":
        return _run(cfg, args)
    if args.command == "add-user":
        return _add_user(cfg, args)
    if args.command == "check":
        return _check(cfg)
    return _verify(cfg, Path(args.pdf))


def _run(cfg, args) -> int:
    from waitress import serve

    from .server import create_app

    app = create_app(cfg)
    print(f"Rotary Sign for {cfg.organisation}: serving on http://{args.host}:{args.port} (public address {cfg.url})", flush=True)
    if not cfg.smtp_host:
        print("Email isn't configured: messages go to the outbox folder and senders copy signing links by hand.", flush=True)
    serve(app, host=args.host, port=args.port, threads=args.threads, ident="Rotary Sign")
    return 0


def _add_user(cfg, args) -> int:
    from werkzeug.security import generate_password_hash

    from . import store

    Path(cfg.data).mkdir(parents=True, exist_ok=True)
    conn = store.connect(Path(cfg.data) / "rotary-sign.db")
    store.init(conn)
    if store.get_user_by_email(conn, args.email):
        print(f"There's already an account for {args.email}", file=sys.stderr)
        return 1
    if sys.stdin.isatty():
        password = getpass.getpass("Password (leave empty to generate one): ")
    else:
        password = ""
    generated = not password
    password = password or secrets.token_urlsafe(9)
    if len(password) < 10:
        print("Passwords need at least 10 characters", file=sys.stderr)
        return 1
    store.create_user(conn, args.email, args.name, generate_password_hash(password), is_admin=args.admin)
    print(f"Created {'administrator' if args.admin else 'sender'} {args.email}" + (f" with temporary password: {password}" if generated else ""))
    return 0


def _check(cfg) -> int:
    ok = True
    print(f"Data folder:   {Path(cfg.data).resolve()}")
    print(f"Public URL:    {cfg.url}" + ("" if cfg.secure_cookies else "   (not HTTPS: fine for testing, use HTTPS in production)"))
    print(f"Organisation:  {cfg.organisation}")
    if cfg.smtp_host:
        print(f"Email:         {cfg.smtp_host}:{cfg.smtp_port} ({cfg.smtp_security}) as {cfg.sender_address}")
        try:
            if cfg.smtp_security == "ssl":
                server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=15, context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15)
            with server:
                if cfg.smtp_security == "starttls":
                    server.starttls(context=ssl.create_default_context())
                if cfg.smtp_user:
                    server.login(cfg.smtp_user, cfg.smtp_password)
            print("               connected and signed in OK")
        except (OSError, smtplib.SMTPException) as exc:
            ok = False
            print(f"               FAILED: {exc}")
    else:
        print("Email:         not configured (messages saved to the outbox folder)")
    from . import pdf

    try:
        if cfg.seal_p12:
            signer = pdf.load_sealer(pkcs12=Path(cfg.seal_p12), passphrase=cfg.seal_password or None)
            print(f"Seal:          {cfg.seal_p12}: {signer.signing_cert.subject.human_friendly}")
        else:
            cert = Path(cfg.data) / "seal" / "seal-cert.pem"
            print(f"Seal:          {'self-signed, ' + str(cert) if cert.exists() else 'a self-signed certificate will be created on first use'}")
    except Exception as exc:
        ok = False
        print(f"Seal:          FAILED to load: {exc}")
    print(f"Timestamps:    {cfg.timestamp_url or 'off'}")
    print(f"Links expire:  after {cfg.link_days} days")
    return 0 if ok else 1


def _verify(cfg, path: Path) -> int:
    from . import pdf

    trusted = []
    cert = Path(cfg.data) / "seal" / "seal-cert.pem"
    if cert.exists():
        trusted.append(cert.read_bytes())
    results = pdf.verify(path.read_bytes(), trusted)
    if not results:
        print("No digital seal found.")
        return 1
    for r in results:
        state = "INTACT" if r["intact"] else "CHANGED AFTER SIGNING"
        trust = "trusted" if r["trusted"] else "certificate not in the trusted list"
        print(f"{r['field']}: {state}; signed by {r['signer']} at {r['signed_at']} ({trust})")
    return 0 if all(r["intact"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
