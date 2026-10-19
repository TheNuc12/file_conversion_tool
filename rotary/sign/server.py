"""Rotary Sign server: send PDFs out for signature and collect them back.

Senders log in, upload a PDF, add signers and place fields. Each signer gets an
emailed link (no account needed), agrees to sign electronically, fills in their
fields and signs. When everyone has signed, the document is stamped, given a
certificate of completion and digitally sealed, then emailed to all parties.

Run it with ``rotary-sign run`` (see rotary/sign/cli.py).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path

from flask import (
    Blueprint,
    Flask,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from . import mail, pdf, store
from .config import Config, load

bp = Blueprint("sign", __name__)

EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
STATUS = {
    "draft": ("Draft", "muted"),
    "sent": ("Out for signature", "info"),
    "completed": ("Completed", "ok"),
    "declined": ("Declined", "bad"),
    "voided": ("Voided", "muted"),
    "waiting": ("Waiting", "muted"),
    "viewed": ("Viewed", "info"),
    "signed": ("Signed", "ok"),
}
ATTACH_LIMIT = 10 * 1024 * 1024  # bigger finished documents are linked, not attached
MIN_PASSWORD = 10
_complete_lock = threading.Lock()
# Compared against when an email isn't registered, so a login takes as long either way.
_DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(16))
_login_failures: dict[str, deque] = defaultdict(deque)
_login_lock = threading.Lock()


# --------------------------------------------------------------------------- app


def create_app(cfg: Config | None = None) -> Flask:
    cfg = cfg or load()
    data = Path(cfg.data)
    (data / "documents").mkdir(parents=True, exist_ok=True)

    app = Flask(__name__)
    app.config["ROTARY"] = cfg
    app.config["DB_PATH"] = data / "rotary-sign.db"
    app.config["MAX_CONTENT_LENGTH"] = (cfg.max_upload_mb + 5) * 1024 * 1024

    conn = store.connect(app.config["DB_PATH"])
    try:
        store.init(conn)
        secret = cfg.secret_key or store.get_setting(conn, "secret_key")
        if not secret:
            secret = secrets.token_urlsafe(48)
            store.set_setting(conn, "secret_key", secret)
        if store.count_users(conn) == 0:
            # Whoever can read the server's console gets to create the first admin.
            app.config["SETUP_CODE"] = secrets.token_hex(4)
            print(f"First-time setup: open {cfg.url}/setup and enter the code {app.config['SETUP_CODE']}", flush=True)
    finally:
        conn.close()

    app.secret_key = secret
    app.config.update(
        SESSION_COOKIE_NAME="rotary_sign",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.secure_cookies,
        PERMANENT_SESSION_LIFETIME=dt.timedelta(hours=12),
    )
    if cfg.trust_proxy:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.register_blueprint(bp)
    app.jinja_env.globals.update(csrf_token=csrf_token, status_label=lambda s: STATUS.get(s, (s, "muted")))
    return app


def cfg() -> Config:
    return current_app.config["ROTARY"]


def db():
    if "db" not in g:
        g.db = store.connect(current_app.config["DB_PATH"])
    return g.db


@bp.teardown_app_request
def _close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@bp.app_context_processor
def _template_globals():
    return {"user": current_user(), "org": cfg().organisation}


@bp.after_app_request
def _security_headers(resp):
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
    )
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"  # signing links must never leak via Referer
    if request.endpoint != "static":
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def _client():
    return request.remote_addr, request.user_agent.string[:300]


def doc_path(envelope_id: str, name: str) -> Path:
    return Path(cfg().data) / "documents" / envelope_id / name


# --------------------------------------------------------------------------- auth & CSRF


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@bp.before_app_request
def _check_csrf():
    # Signing links carry their own unguessable secret in the URL, so they don't need a session token.
    if request.method == "POST" and not request.path.startswith("/s/"):
        sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token") or ""
        expected = session.get("csrf") or ""
        # Both must be present: an empty token must never match a session that has none yet.
        if not sent or not expected or not secrets.compare_digest(sent, expected):
            abort(400, "Your session expired. Reload the page and try again.")


def current_user():
    if "user" not in g:
        uid = session.get("uid")
        user = store.get_user(db(), uid) if uid else None
        g.user = user if user and user["active"] else None
    return g.user


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("sign.login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user()["is_admin"]:
            abort(403)
        return view(*args, **kwargs)

    return wrapper


def _too_many_failures(key: str) -> bool:
    with _login_lock:
        attempts = _login_failures[key]
        while attempts and attempts[0] < time.monotonic() - 900:
            attempts.popleft()
        return len(attempts) >= 10


def _record_failure(key: str) -> None:
    with _login_lock:
        _login_failures[key].append(time.monotonic())


def _safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("sign.dashboard")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_app.config.get("SETUP_CODE") and store.count_users(db()) == 0:
        return redirect(url_for("sign.setup"))
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        key = f"{request.remote_addr}|{email.lower()}"
        if _too_many_failures(key):
            error = "Too many failed attempts. Wait 15 minutes and try again."
        else:
            user = store.get_user_by_email(db(), email)
            password_ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, request.form.get("password", ""))
            if user and user["active"] and password_ok:
                session.clear()
                session["uid"] = user["id"]
                session.permanent = True
                return redirect(_safe_next(request.args.get("next")))
            _record_failure(key)
            error = "That email and password don't match an active account."
    return render_template("login.html", error=error)


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("sign.login"))


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    code = current_app.config.get("SETUP_CODE")
    if not code or store.count_users(db()) > 0:
        return redirect(url_for("sign.login"))
    error = None
    if request.method == "POST":
        f = request.form
        error = _check_new_user(f.get("email", ""), f.get("name", ""), f.get("password", ""))
        if not secrets.compare_digest(f.get("code", "").strip(), code):
            error = "That setup code is wrong. It's printed in the server's console."
        if not error:
            uid = store.create_user(db(), f["email"], _one_line(f["name"], 120), generate_password_hash(f["password"]), is_admin=True)
            current_app.config["SETUP_CODE"] = None
            session.clear()
            session["uid"] = uid
            flash("Welcome! Your administrator account is ready.", "ok")
            return redirect(url_for("sign.dashboard"))
    return render_template("setup.html", error=error)


def _one_line(text: str, limit: int) -> str:
    """Titles and names end up in email headers, where a line break would be an injection."""
    return " ".join(str(text).split())[:limit]


def _check_new_user(email: str, name: str, password: str | None) -> str | None:
    if not EMAIL_RE.match(email.strip()):
        return "Enter a valid email address."
    if not name.strip():
        return "Enter a name."
    if password is not None and len(password) < MIN_PASSWORD:
        return f"Passwords need at least {MIN_PASSWORD} characters."
    if store.get_user_by_email(db(), email):
        return "There's already an account with that email."
    return None


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    error = None
    if request.method == "POST":
        user = current_user()
        if not check_password_hash(user["password_hash"], request.form.get("current", "")):
            error = "Your current password is wrong."
        elif len(request.form.get("new", "")) < MIN_PASSWORD:
            error = f"Passwords need at least {MIN_PASSWORD} characters."
        else:
            store.update_user(db(), user["id"], password_hash=generate_password_hash(request.form["new"]))
            flash("Password changed.", "ok")
            return redirect(url_for("sign.dashboard"))
    return render_template("account.html", error=error)


# --------------------------------------------------------------------------- admin


@bp.route("/admin/users", methods=["GET", "POST"])
@admin_required
def users():
    error = new_password = None
    if request.method == "POST":
        f = request.form
        error = _check_new_user(f.get("email", ""), f.get("name", ""), None)
        if not error:
            new_password = secrets.token_urlsafe(9)
            store.create_user(db(), f["email"], _one_line(f["name"], 120), generate_password_hash(new_password), is_admin=bool(f.get("is_admin")))
            flash(f"Account created for {f['email']}. Give them this temporary password: {new_password}", "ok")
            return redirect(url_for("sign.users"))
    return render_template("users.html", users=store.list_users(db()), error=error)


@bp.post("/admin/users/<int:user_id>")
@admin_required
def user_action(user_id: int):
    target = store.get_user(db(), user_id) or abort(404)
    action = request.form.get("action")
    if target["id"] == current_user()["id"] and action in ("deactivate", "demote"):
        flash("You can't remove your own access.", "bad")
    elif action == "deactivate":
        store.update_user(db(), user_id, active=0)
        flash(f"{target['email']} can no longer sign in.", "ok")
    elif action == "activate":
        store.update_user(db(), user_id, active=1)
    elif action == "promote":
        store.update_user(db(), user_id, is_admin=1)
    elif action == "demote":
        store.update_user(db(), user_id, is_admin=0)
    elif action == "reset":
        password = secrets.token_urlsafe(9)
        store.update_user(db(), user_id, password_hash=generate_password_hash(password))
        flash(f"New temporary password for {target['email']}: {password}", "ok")
    return redirect(url_for("sign.users"))


# --------------------------------------------------------------------------- envelopes (senders)


def _envelope_for_user(envelope_id: str):
    env = store.get_envelope(db(), envelope_id)
    user = current_user()
    if not env or (env["owner_id"] != user["id"] and not user["is_admin"]):
        abort(404)
    return env


@bp.get("/")
@login_required
def dashboard():
    user = current_user()
    envelopes = store.list_envelopes(db(), None if user["is_admin"] else user["id"])
    return render_template("dashboard.html", envelopes=envelopes)


@bp.route("/envelopes/new", methods=["GET", "POST"])
@login_required
def new_envelope():
    error = None
    if request.method == "POST":
        upload = request.files.get("document")
        data = upload.read() if upload else b""
        if not data:
            error = "Choose a PDF to send."
        elif len(data) > cfg().max_upload_mb * 1024 * 1024:
            error = f"That file is bigger than {cfg().max_upload_mb} MB."
        else:
            try:
                prepared = pdf.prepare(data)
                sizes = pdf.page_sizes(prepared)
            except pdf.SignError as exc:
                error = str(exc)
            else:
                filename = Path(upload.filename or "document.pdf").name[:200]
                title = _one_line(request.form.get("title", ""), 200) or _one_line(Path(filename).stem, 200)
                conn = db()
                envelope_id = store.create_envelope(conn, current_user()["id"], title[:200], filename, sizes, pdf.sha256(data))
                path = doc_path(envelope_id, "original.pdf")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(prepared)
                store.add_event(conn, envelope_id, current_user()["email"], f"Created from {filename} ({pdf.sha256(data)[:16]}…)", *_client())
                return redirect(url_for("sign.prepare", envelope_id=envelope_id))
    return render_template("new.html", error=error, max_mb=cfg().max_upload_mb)


@bp.get("/envelopes/<envelope_id>/prepare")
@login_required
def prepare(envelope_id: str):
    env = _envelope_for_user(envelope_id)
    if env["status"] != "draft":
        return redirect(url_for("sign.envelope", envelope_id=envelope_id))
    conn = db()
    payload = {
        "id": env["id"],
        "title": env["title"],
        "message": env["message"],
        "sequential": bool(env["sequential"]),
        "pages": [{"src": url_for("sign.envelope_page", envelope_id=env["id"], n=i), "width": w, "height": h} for i, (w, h) in enumerate(json.loads(env["pages"]))],
        "recipients": [{"name": r["name"], "email": r["email"]} for r in store.list_recipients(conn, env["id"])],
        "fields": [_field_json(f) for f in store.list_fields(conn, env["id"])],
        "saveUrl": url_for("sign.save_envelope", envelope_id=env["id"]),
        "sendUrl": url_for("sign.send_envelope", envelope_id=env["id"]),
        "doneUrl": url_for("sign.envelope", envelope_id=env["id"]),
        "me": {"name": current_user()["name"], "email": current_user()["email"]},
    }
    return render_template("prepare.html", env=env, payload=payload)


def _field_json(row, include_value: bool = False) -> dict:
    out = {k: row[k] for k in ("id", "kind", "page", "x", "y", "w", "h")}
    out["signer"] = row["recipient_position"]
    out["required"] = bool(row["required"])
    if include_value:
        out["value"] = row["value"]
    return out


def _validate_draft(body: dict, page_count: int) -> tuple[list[dict], list[dict]]:
    recipients = body.get("recipients") or []
    if not isinstance(recipients, list) or len(recipients) > 50:
        raise ValueError("Too many signers")
    clean_recipients = []
    for r in recipients:
        name, email = _one_line(r.get("name", ""), 120), str(r.get("email", "")).strip()
        if not name or not EMAIL_RE.match(email):
            raise ValueError(f"Each signer needs a name and a valid email ({name or email or 'blank'})")
        clean_recipients.append({"name": name[:120], "email": email[:200]})
    fields = []
    seen = set()
    for raw in body.get("fields") or []:
        f = pdf.Field.from_dict(raw)
        if f.page >= page_count:
            raise ValueError("A field is on a page that doesn't exist")
        if f.signer is None or not 0 <= f.signer < len(clean_recipients):
            raise ValueError("Every field must belong to a signer")
        field_id = re.sub(r"[^A-Za-z0-9_-]", "", f.id or "")[:40] or secrets.token_hex(6)
        while field_id in seen:
            field_id = secrets.token_hex(6)
        seen.add(field_id)
        fields.append({"id": field_id, "kind": f.kind, "page": f.page, "x": f.x, "y": f.y, "w": f.w, "h": f.h, "signer": f.signer, "required": f.required})
    return clean_recipients, fields


@bp.post("/envelopes/<envelope_id>/save")
@login_required
def save_envelope(envelope_id: str):
    env = _envelope_for_user(envelope_id)
    if env["status"] != "draft":
        return jsonify(error="This envelope has already been sent"), 409
    body = request.get_json(silent=True) or {}
    try:
        recipients, fields = _validate_draft(body, len(json.loads(env["pages"])))
    except ValueError as exc:
        return jsonify(error=str(exc)), 422
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        store.update_envelope(
            conn, env["id"],
            title=_one_line(body.get("title") or "", 200) or env["title"],
            message=str(body.get("message") or "")[:2000],
            sequential=int(bool(body.get("sequential", True))),
        )
        store.replace_recipients(conn, env["id"], recipients)
        store.replace_fields(conn, env["id"], fields)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return jsonify(ok=True)


@bp.post("/envelopes/<envelope_id>/send")
@login_required
def send_envelope(envelope_id: str):
    env = _envelope_for_user(envelope_id)
    if env["status"] != "draft":
        return jsonify(error="This envelope has already been sent"), 409
    conn = db()
    recipients = store.list_recipients(conn, env["id"])
    fields = store.list_fields(conn, env["id"])
    if not recipients:
        return jsonify(error="Add at least one signer"), 422
    for r in recipients:
        mine = [f for f in fields if f["recipient_position"] == r["position"]]
        if not mine:
            return jsonify(error=f"{r['name']} has no fields to fill in. Give them at least a signature."), 422
    if not store.transition(conn, env["id"], "draft", "sent", sent_at=store.now()):
        return jsonify(error="This envelope has already been sent"), 409
    store.add_event(conn, env["id"], current_user()["email"], f"Sent to {', '.join(r['name'] + ' <' + r['email'] + '>' for r in recipients)}", *_client())
    first = recipients[:1] if env["sequential"] else recipients
    links = {}
    for r in first:
        links[r["email"]] = _invite(conn, store.get_envelope(conn, env["id"]), r)
    return jsonify(ok=True, url=url_for("sign.envelope", envelope_id=env["id"]), links=links if not cfg().smtp_host else {})


def _invite(conn, env, recipient, reminder: bool = False) -> str:
    """Issue a fresh signing link for ``recipient`` and email it. Returns the link."""
    token = store.issue_token(conn, recipient["id"], cfg().link_days)
    if recipient["status"] == "waiting":
        store.update_recipient(conn, recipient["id"], status="sent")
    link = f"{cfg().url}/s/{token}"
    owner = store.get_user(conn, env["owner_id"])
    paragraphs = [f"{owner['name']} ({owner['email']}) has sent you \"{env['title']}\" to review and sign."]
    if env["message"]:
        paragraphs.append(f"Their message: {env['message']}")
    paragraphs.append(f"This link is personal to you and expires in {cfg().link_days} days.")
    msg = mail.compose(
        cfg(),
        (recipient["name"], recipient["email"]),
        f"{'Reminder: p' if reminder else 'P'}lease sign: {env['title']}",
        f"{'Reminder: ' if reminder else ''}{env['title']} is ready for your signature",
        paragraphs,
        button=("Review and sign", link),
    )
    _deliver(conn, env["id"], msg, f"{'Reminder' if reminder else 'Signing request'} emailed to {recipient['email']}")
    return link


def _deliver(conn, envelope_id: str, msg, description: str) -> bool:
    try:
        where = mail.send(cfg(), msg)
    except mail.MailError as exc:
        store.add_event(conn, envelope_id, "system", f"Email failed: {exc}")
        current_app.logger.warning("%s", exc)
        return False
    store.add_event(conn, envelope_id, "system", description if where == "sent" else f"{description} ({where})")
    return True


@bp.get("/envelopes/<envelope_id>")
@login_required
def envelope(envelope_id: str):
    env = _envelope_for_user(envelope_id)
    conn = db()
    recipients = store.list_recipients(conn, env["id"])
    all_signed = bool(recipients) and all(r["status"] == "signed" for r in recipients)
    return render_template(
        "envelope.html",
        env=env,
        recipients=recipients,
        events=store.list_events(conn, env["id"]),
        chain_ok=store.verify_chain(conn, env["id"]),
        needs_completion=env["status"] == "sent" and all_signed,
        email_configured=bool(cfg().smtp_host),
    )


@bp.get("/envelopes/<envelope_id>/page/<int:n>.png")
@login_required
def envelope_page(envelope_id: str, n: int):
    env = _envelope_for_user(envelope_id)
    return _page_png(env, n)


def _page_png(env, n: int):
    cache = doc_path(env["id"], f"pages/{n}.png")
    if not cache.exists():
        if not 0 <= n < len(json.loads(env["pages"])):
            abort(404)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(pdf.render_page(doc_path(env["id"], "original.pdf").read_bytes(), n, 1100))
    resp = send_file(cache, mimetype="image/png", max_age=3600)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@bp.get("/envelopes/<envelope_id>/download/<which>")
@login_required
def download(envelope_id: str, which: str):
    env = _envelope_for_user(envelope_id)
    stem = Path(env["filename"]).stem
    if which == "original":
        return send_file(doc_path(env["id"], "original.pdf"), mimetype="application/pdf", as_attachment=True, download_name=f"{stem}.pdf")
    if which == "signed" and env["status"] == "completed":
        return send_file(doc_path(env["id"], "signed.pdf"), mimetype="application/pdf", as_attachment=True, download_name=f"{stem}-signed.pdf")
    abort(404)


@bp.post("/envelopes/<envelope_id>/void")
@login_required
def void(envelope_id: str):
    env = _envelope_for_user(envelope_id)
    if env["status"] != "sent":
        abort(409)
    conn = db()
    reason = request.form.get("reason", "").strip()[:500]
    if not store.transition(conn, env["id"], "sent", "voided"):
        abort(409)
    conn.execute("UPDATE recipients SET token_hash = NULL WHERE envelope_id = ?", (env["id"],))
    store.add_event(conn, env["id"], current_user()["email"], f"Voided{': ' + reason if reason else ''}", *_client())
    for r in store.list_recipients(conn, env["id"]):
        if r["status"] in ("sent", "viewed"):
            msg = mail.compose(
                cfg(), (r["name"], r["email"]), f"Cancelled: {env['title']}", f"{env['title']} has been cancelled",
                [f"{current_user()['name']} cancelled this signing request, so there's nothing for you to do." + (f" Reason: {reason}" if reason else "")],
            )
            _deliver(conn, env["id"], msg, f"Cancellation emailed to {r['email']}")
    flash("Envelope voided. Its signing links no longer work.", "ok")
    return redirect(url_for("sign.envelope", envelope_id=env["id"]))


@bp.post("/envelopes/<envelope_id>/delete")
@login_required
def delete(envelope_id: str):
    env = _envelope_for_user(envelope_id)
    if env["status"] != "draft":
        flash("Only drafts can be deleted. Sent envelopes are kept as records.", "bad")
        return redirect(url_for("sign.envelope", envelope_id=env["id"]))
    store.delete_envelope(db(), env["id"])
    for path in sorted(doc_path(env["id"], "").rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    doc_path(env["id"], "").rmdir()
    flash("Draft deleted.", "ok")
    return redirect(url_for("sign.dashboard"))


@bp.post("/envelopes/<envelope_id>/recipients/<int:recipient_id>/<action>")
@login_required
def recipient_action(envelope_id: str, recipient_id: int, action: str):
    env = _envelope_for_user(envelope_id)
    conn = db()
    r = store.get_recipient(conn, recipient_id)
    if not r or r["envelope_id"] != env["id"] or env["status"] != "sent" or r["status"] not in ("sent", "viewed"):
        abort(409)
    if action == "remind":
        _invite(conn, env, r, reminder=True)
        flash(f"Sent {r['name']} a new signing link.", "ok")
    elif action == "link":
        token = store.issue_token(conn, r["id"], cfg().link_days)
        store.add_event(conn, env["id"], current_user()["email"], f"Created a new signing link for {r['email']}", *_client())
        flash(f"New signing link for {r['name']} (the old one no longer works): {cfg().url}/s/{token}", "link")
    else:
        abort(404)
    return redirect(url_for("sign.envelope", envelope_id=env["id"]))


@bp.post("/envelopes/<envelope_id>/complete")
@login_required
def retry_completion(envelope_id: str):
    env = _envelope_for_user(envelope_id)
    try:
        _complete(db(), env["id"])
        flash("Document completed and sealed.", "ok")
    except Exception as exc:
        flash(f"Still couldn't complete the document: {exc}", "bad")
    return redirect(url_for("sign.envelope", envelope_id=env["id"]))


# --------------------------------------------------------------------------- signers


def _closed(title: str, text: str, code: int):
    return make_response(render_template("message.html", title=title, text=text), code)


def _signer(token: str, *, allow_done: bool = False):
    """``(recipient, envelope, None)`` for a usable signing link, else ``(…, response)`` explaining why not."""
    conn = db()
    r = store.recipient_for_token(conn, token)
    if not r:
        return None, None, _closed("This link doesn't work", "It may have been replaced by a newer link, or the request was cancelled. Ask the sender to send it again.", 404)
    env = store.get_envelope(conn, r["envelope_id"])
    if r["token_expires"] and r["token_expires"] < store.now():
        return None, None, _closed("This link has expired", "Ask the sender to send you a new one.", 410)
    if env["status"] == "voided":
        return None, None, _closed("This request was cancelled", "The sender cancelled it, so there's nothing for you to do.", 410)
    if env["status"] == "declined" and r["status"] != "declined":
        return None, None, _closed("This request is closed", "Another signer declined, so the document won't be completed.", 410)
    if not allow_done and r["status"] in ("signed", "declined"):
        return r, env, redirect(url_for("sign.signer_done", token=token))
    if env["sequential"]:
        earlier = [x for x in store.list_recipients(conn, env["id"]) if x["position"] < r["position"]]
        if any(x["status"] != "signed" for x in earlier):
            return None, None, _closed("It's not your turn yet", "Someone else needs to sign first. We'll email you when it's your turn.", 409)
    return r, env, None


@bp.get("/s/<token>")
def signer(token: str):
    r, env, page = _signer(token)
    if page is not None:
        return page
    conn = db()
    if not r["viewed_at"]:
        store.update_recipient(conn, r["id"], viewed_at=store.now(), status="viewed")
        store.add_event(conn, env["id"], f"{r['name']} <{r['email']}>", "Opened the signing link", *_client())
    owner = store.get_user(conn, env["owner_id"])
    if not r["consented_at"]:
        return render_template("consent.html", env=env, recipient=r, owner=owner, token=token)
    fields = []
    for f in store.list_fields(conn, env["id"]):
        item = _field_json(f, include_value=f["recipient_position"] != r["position"])
        item["mine"] = f["recipient_position"] == r["position"]
        item["done"] = bool(f["value"]) and not item["mine"]
        if item["mine"] or item["done"]:
            fields.append(item)
    payload = {
        "pages": [{"src": url_for("sign.signer_page", token=token, n=i), "width": w, "height": h} for i, (w, h) in enumerate(json.loads(env["pages"]))],
        "fields": fields,
        "name": r["name"],
        "submitUrl": url_for("sign.signer_submit", token=token),
        "doneUrl": url_for("sign.signer_done", token=token),
    }
    return render_template("sign.html", env=env, recipient=r, owner=owner, token=token, payload=payload)


@bp.post("/s/<token>/consent")
def signer_consent(token: str):
    r, env, page = _signer(token)
    if page is not None:
        return page
    if request.form.get("agree") != "yes":
        return redirect(url_for("sign.signer", token=token))
    conn = db()
    store.update_recipient(conn, r["id"], consented_at=store.now())
    store.add_event(conn, env["id"], f"{r['name']} <{r['email']}>", "Agreed to use electronic records and signatures", *_client())
    return redirect(url_for("sign.signer", token=token))


@bp.get("/s/<token>/page/<int:n>.png")
def signer_page(token: str, n: int):
    r, env, page = _signer(token, allow_done=True)
    if page is not None:
        abort(404)
    return _page_png(env, n)


@bp.post("/s/<token>/submit")
def signer_submit(token: str):
    r, env, page = _signer(token)
    if page is not None or not r["consented_at"]:
        return jsonify(error="This link can't be used to sign right now. Reload the page."), 409
    values = (request.get_json(silent=True) or {}).get("values") or {}
    if not isinstance(values, dict):
        return jsonify(error="Malformed request"), 400
    conn = db()
    mine = [f for f in store.list_fields(conn, env["id"]) if f["recipient_position"] == r["position"]]
    today = dt.datetime.now(dt.timezone.utc).strftime("%d %b %Y")
    final: dict[str, str | None] = {}
    for f in mine:
        value = values.get(f["id"])
        value = None if value in (None, "") else str(value)
        if f["kind"] == "name":
            value = r["name"]
        elif f["kind"] == "date":
            value = today
        elif f["kind"] in ("signature", "initials") and value is not None:
            try:
                pdf.signature_image(value)  # validate before accepting
            except pdf.SignError as exc:
                return jsonify(error=str(exc)), 422
        elif f["kind"] == "checkbox":
            value = "true" if value == "true" else None
        elif f["kind"] == "text" and value is not None:
            value = value[:500]
        if f["required"] and value is None:
            label = {"signature": "a signature", "initials": "your initials", "checkbox": "a required checkbox", "text": "a required text field"}[f["kind"]]
            return jsonify(error=f"Please fill in {label} (page {f['page'] + 1}).", field=f["id"]), 422
        final[f["id"]] = value

    conn.execute("BEGIN IMMEDIATE")
    try:
        fresh = store.get_recipient(conn, r["id"])
        if fresh["status"] in ("signed", "declined"):
            conn.execute("ROLLBACK")
            return jsonify(ok=True, url=url_for("sign.signer_done", token=token))
        for field_id, value in final.items():
            store.set_field_value(conn, env["id"], field_id, value)
        ip, agent = _client()
        store.update_recipient(conn, r["id"], status="signed", signed_at=store.now(), ip=ip, user_agent=agent)
        store.add_event(conn, env["id"], f"{r['name']} <{r['email']}>", "Signed", ip, agent)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    recipients = store.list_recipients(conn, env["id"])
    if all(x["status"] == "signed" for x in recipients):
        try:
            _complete(conn, env["id"])
        except Exception as exc:  # their signature is safely recorded; the sender can retry
            current_app.logger.exception("completing %s failed", env["id"])
            store.add_event(conn, env["id"], "system", f"Completing the document failed: {exc}")
    elif env["sequential"]:
        nxt = next((x for x in recipients if x["status"] == "waiting"), None)
        if nxt:
            _invite(conn, env, nxt)
    return jsonify(ok=True, url=url_for("sign.signer_done", token=token))


@bp.post("/s/<token>/decline")
def signer_decline(token: str):
    r, env, page = _signer(token)
    if page is not None:
        return page
    conn = db()
    reason = request.form.get("reason", "").strip()[:500]
    ip, agent = _client()
    store.update_recipient(conn, r["id"], status="declined", declined_at=store.now(), decline_reason=reason, ip=ip, user_agent=agent)
    store.update_envelope(conn, env["id"], status="declined")
    store.add_event(conn, env["id"], f"{r['name']} <{r['email']}>", f"Declined to sign{': ' + reason if reason else ''}", ip, agent)
    owner = store.get_user(conn, env["owner_id"])
    msg = mail.compose(
        cfg(), (owner["name"], owner["email"]), f"Declined: {env['title']}", f"{r['name']} declined to sign",
        [f"{r['name']} ({r['email']}) declined to sign \"{env['title']}\"." + (f" Their reason: {reason}" if reason else "")],
        button=("View the envelope", f"{cfg().url}/envelopes/{env['id']}"),
    )
    _deliver(conn, env["id"], msg, f"Decline notice emailed to {owner['email']}")
    return redirect(url_for("sign.signer_done", token=token))


@bp.get("/s/<token>/done")
def signer_done(token: str):
    r, env, page = _signer(token, allow_done=True)
    if page is not None:
        return page
    return render_template("done.html", env=env, recipient=r, token=token)


@bp.get("/s/<token>/download")
def signer_download(token: str):
    r, env, page = _signer(token, allow_done=True)
    if page is not None or env["status"] != "completed" or r["status"] != "signed":
        abort(404)
    return send_file(doc_path(env["id"], "signed.pdf"), mimetype="application/pdf", as_attachment=True, download_name=f"{Path(env['filename']).stem}-signed.pdf")


# --------------------------------------------------------------------------- completion & sealing


def _sealer():
    if "SEALER" not in current_app.config:
        c = cfg()
        if c.seal_p12:
            sealer = pdf.load_sealer(pkcs12=Path(c.seal_p12), passphrase=c.seal_password or None)
            cert_pem = None
        else:
            folder = Path(c.data) / "seal"
            key, cert = folder / "seal-key.pem", folder / "seal-cert.pem"
            if not key.exists():
                pdf.create_seal_certificate(folder, c.organisation)
            sealer = pdf.load_sealer(key=key, cert=cert)
            cert_pem = cert.read_bytes()
        current_app.config["SEALER"] = sealer
        current_app.config["SEAL_CERT_PEM"] = cert_pem or _signer_cert_pem(sealer)
    return current_app.config["SEALER"]


def _signer_cert_pem(sealer) -> bytes:
    from asn1crypto import pem

    return pem.armor("CERTIFICATE", sealer.signing_cert.dump())


def _complete(conn, envelope_id: str) -> None:
    """Stamp every field, add the certificate of completion, seal, store and email the result."""
    with _complete_lock:
        env = store.get_envelope(conn, envelope_id)
        recipients = store.list_recipients(conn, envelope_id)
        if env["status"] != "sent" or not all(r["status"] == "signed" for r in recipients):
            return
        fields = [
            pdf.Field(f["kind"], f["page"], f["x"], f["y"], f["w"], f["h"], f["value"])
            for f in store.list_fields(conn, envelope_id)
        ]
        stamped = pdf.stamp(doc_path(envelope_id, "original.pdf").read_bytes(), fields)
        completed_at = store.now()
        store.add_event(conn, envelope_id, "system", "All parties signed; document completed and sealed")
        owner = store.get_user(conn, env["owner_id"])
        record = {
            "title": env["title"],
            "envelope_id": env["id"],
            "original_sha256": env["original_sha256"],
            "sender": f"{owner['name']} <{owner['email']}>",
            "created_at": f"{env['created_at']} UTC",
            "completed_at": f"{completed_at} UTC",
            "signers": [
                {"name": r["name"], "email": r["email"], "signed_at": r["signed_at"], "ip": r["ip"], "user_agent": r["user_agent"]}
                for r in recipients
            ],
            "events": [{"at": e["at"], "actor": e["actor"], "action": e["action"], "ip": e["ip"]} for e in store.list_events(conn, envelope_id)],
        }
        names = ", ".join(r["name"] for r in recipients)
        sealed = pdf.seal(
            pdf.append(stamped, pdf.certificate(record)),
            _sealer(),
            reason=f"Signed by {names} via Rotary Sign"[:200],
            location=cfg().organisation,
            timestamp_url=cfg().timestamp_url or None,
        )
        doc_path(envelope_id, "signed.pdf").write_bytes(sealed)
        store.update_envelope(conn, envelope_id, status="completed", completed_at=completed_at, final_sha256=pdf.sha256(sealed))

    filename = f"{Path(env['filename']).stem}-signed.pdf"
    attachment = (filename, sealed) if len(sealed) <= ATTACH_LIMIT else None
    people = [(owner["name"], owner["email"], f"{cfg().url}/envelopes/{env['id']}")] + [
        (r["name"], r["email"], None) for r in recipients if r["email"].lower() != owner["email"].lower()
    ]
    for name, email, link in people:
        text = [f"Everyone has signed \"{env['title']}\". The completed document is attached" + ("." if attachment else " on the signing site.")]
        msg = mail.compose(
            cfg(), (name, email), f"Completed: {env['title']}", f"{env['title']} is complete",
            text, button=("View the envelope", link) if link else None, attachment=attachment,
        )
        _deliver(conn, envelope_id, msg, f"Completed document emailed to {email}")


# --------------------------------------------------------------------------- verification


@bp.route("/verify", methods=["GET", "POST"])
def verify():
    result = None
    if request.method == "POST":
        upload = request.files.get("document")
        data = upload.read() if upload else b""
        if not data:
            result = {"error": "Choose a PDF to check."}
        else:
            digest = pdf.sha256(data)
            row = db().execute("SELECT * FROM envelopes WHERE final_sha256 = ? AND status = 'completed'", (digest,)).fetchone()
            try:
                _sealer()
                seals = pdf.verify(data, [current_app.config["SEAL_CERT_PEM"]])
            except pdf.SignError as exc:
                seals, result = [], {"error": str(exc)}
            if result is None:
                result = {
                    "match": dict(row) if row else None,
                    "signers": [dict(r) for r in store.list_recipients(db(), row["id"])] if row else [],
                    "seals": seals,
                }
    return render_template("verify.html", result=result)


@bp.get("/seal-certificate.pem")
def seal_certificate():
    _sealer()
    resp = current_app.response_class(current_app.config["SEAL_CERT_PEM"], mimetype="application/x-pem-file")
    resp.headers["Content-Disposition"] = "attachment; filename=rotary-sign-seal.pem"
    return resp


@bp.app_errorhandler(400)
@bp.app_errorhandler(403)
@bp.app_errorhandler(404)
@bp.app_errorhandler(409)
@bp.app_errorhandler(413)
def _error_page(exc):
    titles = {400: "Something went wrong", 403: "You don't have access to that", 404: "Not found", 409: "That can't be done right now", 413: "That file is too large"}
    code = getattr(exc, "code", 500)
    if request.is_json or request.path.endswith((".png", "/submit", "/save", "/send")):
        return jsonify(error=getattr(exc, "description", titles.get(code))), code
    return render_template("message.html", title=titles.get(code, "Error"), text=getattr(exc, "description", "")), code
