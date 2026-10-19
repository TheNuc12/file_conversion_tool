"""The company signing server: accounts, sending, signing, completion and its security rules."""

import email
import io
import json
import re
from pathlib import Path

import pytest

pytest.importorskip("flask")
pytest.importorskip("pyhanko")

from rotary.sign import pdf as P  # noqa: E402
from rotary.sign import store  # noqa: E402
from rotary.sign.config import load  # noqa: E402
from rotary.sign.server import create_app  # noqa: E402

from .test_sign import make_pdf, signature_url, text_of  # noqa: E402


@pytest.fixture
def app(tmp_path, capsys):
    cfg = load(tmp_path / "data", env={"ROTARY_SIGN_URL": "https://sign.example.com", "ROTARY_SIGN_ORGANISATION": "Acme Ltd"})
    application = create_app(cfg)
    application.config["TESTING"] = True
    application.setup_output = capsys.readouterr().out
    return application


def csrf(client) -> str:
    with client.session_transaction() as s:
        s.setdefault("csrf", "test-token")
        return s["csrf"]


def setup_admin(app, client, email="boss@acme.com"):
    code = re.search(r"enter the code (\w+)", app.setup_output).group(1)
    res = client.post("/setup", data={"csrf": csrf(client), "code": code, "name": "Boss", "email": email, "password": "correct horse battery"})
    assert res.status_code == 302, res.data
    return client


def outbox(app) -> list:
    folder = Path(app.config["ROTARY"].data) / "outbox"
    return [email.message_from_bytes(p.read_bytes()) for p in sorted(folder.glob("*.eml"))] if folder.exists() else []


def link_in(msg) -> str:
    body = next(part for part in msg.walk() if part.get_content_type() == "text/plain").get_payload(decode=True).decode()
    return re.search(r"https://sign\.example\.com(/s/[\w-]+)", body).group(1)


def create_envelope(client, recipients, fields, sequential=True, title="Supply agreement"):
    res = client.post(
        "/envelopes/new",
        data={"csrf": csrf(client), "title": title, "document": (io.BytesIO(make_pdf(2)), "agreement.pdf")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 302, res.data
    envelope_id = res.headers["Location"].split("/")[2]
    headers = {"X-CSRF-Token": csrf(client)}
    res = client.post(f"/envelopes/{envelope_id}/save", json={"title": title, "message": "Please sign", "sequential": sequential, "recipients": recipients, "fields": fields}, headers=headers)
    assert res.status_code == 200, res.json
    return envelope_id


TWO_SIGNERS = [{"name": "Ada Lovelace", "email": "ada@example.org"}, {"name": "Grace Hopper", "email": "grace@example.org"}]
FIELDS = [
    {"id": "s1", "kind": "signature", "page": 0, "x": 0.1, "y": 0.8, "w": 0.3, "h": 0.06, "signer": 0},
    {"id": "d1", "kind": "date", "page": 0, "x": 0.5, "y": 0.82, "w": 0.2, "h": 0.03, "signer": 0},
    {"id": "t1", "kind": "text", "page": 1, "x": 0.1, "y": 0.2, "w": 0.4, "h": 0.03, "signer": 0, "required": False},
    {"id": "s2", "kind": "signature", "page": 1, "x": 0.1, "y": 0.8, "w": 0.3, "h": 0.06, "signer": 1},
    {"id": "n2", "kind": "name", "page": 1, "x": 0.5, "y": 0.82, "w": 0.3, "h": 0.03, "signer": 1},
    {"id": "c2", "kind": "checkbox", "page": 1, "x": 0.1, "y": 0.9, "w": 0.03, "h": 0.02, "signer": 1},
]


def sign_as(client, path, values):
    assert client.get(path).status_code == 200  # consent page
    res = client.post(f"{path}/consent", data={"agree": "yes"})
    assert res.status_code == 302
    page = client.get(path)
    assert b'id="page-data"' in page.data
    return client.post(f"{path}/submit", json={"values": values})


def test_full_sequential_signing_flow(app):
    sender = setup_admin(app, app.test_client())
    envelope_id = create_envelope(sender, TWO_SIGNERS, FIELDS)
    res = sender.post(f"/envelopes/{envelope_id}/send", headers={"X-CSRF-Token": csrf(sender)})
    assert res.status_code == 200 and res.json["links"]  # no SMTP: links handed back to the sender

    # Only the first signer is emailed in sequential mode.
    [first_mail] = outbox(app)
    assert first_mail["To"] == "Ada Lovelace <ada@example.org>" and "Supply agreement" in first_mail["Subject"]
    ada = app.test_client()
    res = sign_as(ada, link_in(first_mail), {"s1": signature_url(), "t1": "Delivery by March", "d1": "1999-01-01"})
    assert res.status_code == 200, res.json

    # Grace is emailed only now.
    mails = outbox(app)
    assert len(mails) == 2 and mails[1]["To"] == "Grace Hopper <grace@example.org>"
    grace = app.test_client()
    path = link_in(mails[1])
    # Ada's filled-in fields are shown to Grace read-only; Grace's own fields are hers to fill.
    consent = grace.get(path)
    grace.post(f"{path}/consent", data={"agree": "yes"})
    payload = json.loads(re.search(rb'id="page-data">(.*?)</script>', grace.get(path).data, re.S).group(1))
    done = {f["id"] for f in payload["fields"] if f["done"]}
    assert done == {"s1", "d1", "t1"} and consent.status_code == 200
    res = grace.post(f"{path}/submit", json={"values": {"s2": signature_url(), "c2": "true", "n2": "Somebody Else"}})
    assert res.status_code == 200, res.json

    # Completed, sealed and emailed to everyone with the PDF attached.
    with app.app_context():
        conn = store.connect(app.config["DB_PATH"])
        env = store.get_envelope(conn, envelope_id)
        assert env["status"] == "completed" and store.verify_chain(conn, envelope_id)
    final = sender.get(f"/envelopes/{envelope_id}/download/signed").data
    assert P.sha256(final) == env["final_sha256"]
    seal_cert = (Path(app.config["ROTARY"].data) / "seal" / "seal-cert.pem").read_bytes()
    [seal] = P.verify(final, [seal_cert])
    assert seal["intact"] and seal["trusted"] and seal["covers_whole_file"]
    page2 = text_of(final, 1)
    assert "Grace Hopper" in page2 and "Somebody Else" not in page2  # names come from the envelope
    assert "Delivery by March" in page2
    assert "1999" not in text_of(final, 0)  # dates are stamped by the server
    certificate = text_of(final, 2)
    assert "Certificate of completion" in certificate and "ada@example.org" in certificate and "Agreed to use electronic records" in certificate
    completed = [m for m in outbox(app) if m["Subject"].startswith("Completed")]
    assert sorted(m["To"] for m in completed) == ["Ada Lovelace <ada@example.org>", "Boss <boss@acme.com>", "Grace Hopper <grace@example.org>"]
    assert all(any(p.get_filename() == "agreement-signed.pdf" for p in m.walk()) for m in completed)
    # Signers can download their copy; the verify page recognises the file.
    assert grace.get(f"{path}/download").data == final
    checker = app.test_client()
    res = checker.post("/verify", data={"csrf": csrf(checker), "document": (io.BytesIO(final), "x.pdf")}, content_type="multipart/form-data")
    assert b"Authentic and unchanged" in res.data
    tampered = bytearray(final)
    tampered[final.index(b"stream") + 12] ^= 0x01
    res = checker.post("/verify", data={"csrf": csrf(checker), "document": (io.BytesIO(bytes(tampered)), "x.pdf")}, content_type="multipart/form-data")
    assert b"This document has been changed" in res.data


def test_parallel_signing_emails_everyone_at_once(app):
    sender = setup_admin(app, app.test_client())
    envelope_id = create_envelope(sender, TWO_SIGNERS, FIELDS, sequential=False)
    sender.post(f"/envelopes/{envelope_id}/send", headers={"X-CSRF-Token": csrf(sender)})
    assert sorted(m["To"] for m in outbox(app)) == ["Ada Lovelace <ada@example.org>", "Grace Hopper <grace@example.org>"]


def test_signers_must_take_turns_and_fill_required_fields(app):
    sender = setup_admin(app, app.test_client())
    envelope_id = create_envelope(sender, TWO_SIGNERS, FIELDS)
    sender.post(f"/envelopes/{envelope_id}/send", headers={"X-CSRF-Token": csrf(sender)})
    ada = app.test_client()
    path = link_in(outbox(app)[0])
    # Submitting without consent is refused.
    assert ada.post(f"{path}/submit", json={"values": {"s1": signature_url()}}).status_code == 409
    res = sign_as(ada, path, {"t1": "no signature"})
    assert res.status_code == 422 and res.json["field"] == "s1"
    res = ada.post(f"{path}/submit", json={"values": {"s1": "data:image/svg+xml;base64,PHN2Zz4="}})
    assert res.status_code == 422
    # Grace's fields can't be filled with Ada's link: unknown ids are ignored.
    res = ada.post(f"{path}/submit", json={"values": {"s1": signature_url(), "s2": signature_url()}})
    assert res.status_code == 200
    with app.app_context():
        conn = store.connect(app.config["DB_PATH"])
        values = {f["id"]: f["value"] for f in store.list_fields(conn, envelope_id)}
    assert values["s1"] and values["s2"] is None
    # Signing twice is harmless.
    assert ada.post(f"{path}/submit", json={"values": {"s1": signature_url()}}).status_code == 409


def test_links_are_secret_hashed_and_expire(app):
    sender = setup_admin(app, app.test_client())
    envelope_id = create_envelope(sender, TWO_SIGNERS[:1], FIELDS[:3])
    sender.post(f"/envelopes/{envelope_id}/send", headers={"X-CSRF-Token": csrf(sender)})
    path = link_in(outbox(app)[0])
    token = path.split("/")[-1]
    with app.app_context():
        conn = store.connect(app.config["DB_PATH"])
        assert conn.execute("SELECT COUNT(*) FROM recipients WHERE token_hash = ?", (token,)).fetchone()[0] == 0
        conn.execute("UPDATE recipients SET token_expires = '2000-01-01 00:00:00'")
    client = app.test_client()
    assert client.get(path).status_code == 410
    assert client.get("/s/not-a-real-token").status_code == 404


def test_resending_replaces_the_old_link(app):
    sender = setup_admin(app, app.test_client())
    envelope_id = create_envelope(sender, TWO_SIGNERS[:1], FIELDS[:3])
    sender.post(f"/envelopes/{envelope_id}/send", headers={"X-CSRF-Token": csrf(sender)})
    old = link_in(outbox(app)[0])
    with app.app_context():
        rid = store.connect(app.config["DB_PATH"]).execute("SELECT id FROM recipients").fetchone()[0]
    sender.post(f"/envelopes/{envelope_id}/recipients/{rid}/remind", data={"csrf": csrf(sender)})
    new = link_in(outbox(app)[-1])
    assert new != old
    client = app.test_client()
    assert client.get(old).status_code == 404 and client.get(new).status_code == 200


def test_void_and_decline(app):
    sender = setup_admin(app, app.test_client())
    voided = create_envelope(sender, TWO_SIGNERS, FIELDS, sequential=False)
    sender.post(f"/envelopes/{voided}/send", headers={"X-CSRF-Token": csrf(sender)})
    link = link_in(outbox(app)[0])
    sender.post(f"/envelopes/{voided}/void", data={"csrf": csrf(sender), "reason": "Wrong version"})
    assert app.test_client().get(link).status_code == 404  # links are revoked
    assert any("Cancelled" in m["Subject"] for m in outbox(app))

    declined = create_envelope(sender, TWO_SIGNERS, FIELDS, sequential=False)
    sender.post(f"/envelopes/{declined}/send", headers={"X-CSRF-Token": csrf(sender)})
    ada_link = link_in(outbox(app)[-2])
    grace_link = link_in(outbox(app)[-1])
    app.test_client().post(f"{ada_link}/decline", data={"reason": "Price is wrong"})
    assert any(m["Subject"] == "Declined: Supply agreement" for m in outbox(app))
    assert app.test_client().get(grace_link).status_code == 410
    page = sender.get(f"/envelopes/{declined}").data
    assert b"Price is wrong" in page and b"Declined" in page


def test_access_control_and_csrf(app):
    boss = setup_admin(app, app.test_client())
    boss.post("/admin/users", data={"csrf": csrf(boss), "name": "Sam", "email": "sam@acme.com"})
    flashes = boss.get("/admin/users").data.decode()
    assert "sam@acme.com" in flashes
    envelope_id = create_envelope(boss, TWO_SIGNERS, FIELDS)

    # Log Sam in with a password set directly.
    from werkzeug.security import generate_password_hash

    with app.app_context():
        conn = store.connect(app.config["DB_PATH"])
        sam_id = store.get_user_by_email(conn, "sam@acme.com")["id"]
        store.update_user(conn, sam_id, password_hash=generate_password_hash("sams-password-123"))
    sam = app.test_client()
    assert sam.post("/login", data={"csrf": csrf(sam), "email": "sam@acme.com", "password": "sams-password-123"}).status_code == 302
    assert sam.get(f"/envelopes/{envelope_id}").status_code == 404  # not Sam's envelope
    assert sam.get(f"/envelopes/{envelope_id}/download/original").status_code == 404
    assert sam.get("/admin/users").status_code == 403
    assert boss.get(f"/envelopes/{envelope_id}/prepare").status_code == 200

    # No CSRF token, no state change.
    assert boss.post(f"/envelopes/{envelope_id}/send").status_code == 400
    anonymous = app.test_client()
    assert anonymous.get("/").status_code == 302
    assert anonymous.post("/envelopes/new", data={}).status_code == 400


def test_login_rate_limit_and_setup_lockout(app):
    client = app.test_client()
    assert client.get("/login").headers["Location"].endswith("/setup")
    assert client.post("/setup", data={"csrf": csrf(client), "code": "wrong", "name": "X", "email": "x@x.com", "password": "long enough pw"}).status_code == 200
    setup_admin(app, app.test_client())
    assert app.test_client().get("/setup").status_code == 302  # setup is closed once an admin exists
    for _ in range(10):
        client.post("/login", data={"csrf": csrf(client), "email": "boss@acme.com", "password": "nope"})
    res = client.post("/login", data={"csrf": csrf(client), "email": "boss@acme.com", "password": "correct horse battery"})
    assert b"Too many failed attempts" in res.data


def test_security_headers_and_tampered_audit_trail(app):
    boss = setup_admin(app, app.test_client())
    envelope_id = create_envelope(boss, TWO_SIGNERS, FIELDS)
    res = boss.get(f"/envelopes/{envelope_id}")
    assert res.headers["X-Frame-Options"] == "DENY" and res.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in res.headers["Content-Security-Policy"]
    assert b"Audit trail intact" in res.data
    with app.app_context():
        store.connect(app.config["DB_PATH"]).execute("UPDATE events SET actor = 'someone else' WHERE envelope_id = ?", (envelope_id,))
    assert b"has been altered" in boss.get(f"/envelopes/{envelope_id}").data


def test_validation_of_drafts(app):
    boss = setup_admin(app, app.test_client())
    envelope_id = create_envelope(boss, TWO_SIGNERS, FIELDS)
    headers = {"X-CSRF-Token": csrf(boss)}
    bad = [
        {"recipients": [{"name": "", "email": "x@y.com"}], "fields": []},
        {"recipients": [{"name": "A", "email": "not-an-email"}], "fields": []},
        {"recipients": TWO_SIGNERS, "fields": [dict(FIELDS[0], signer=5)]},
        {"recipients": TWO_SIGNERS, "fields": [dict(FIELDS[0], page=9)]},
    ]
    for body in bad:
        assert boss.post(f"/envelopes/{envelope_id}/save", json=body, headers=headers).status_code == 422
    boss.post(f"/envelopes/{envelope_id}/save", json={"recipients": TWO_SIGNERS, "fields": FIELDS[:2]}, headers=headers)
    res = boss.post(f"/envelopes/{envelope_id}/send", headers=headers)
    assert res.status_code == 422 and "Grace Hopper has no fields" in res.json["error"]
    res = boss.post("/envelopes/new", data={"csrf": csrf(boss), "document": (io.BytesIO(b"not a pdf"), "x.pdf")}, content_type="multipart/form-data")
    assert b"readable PDF" in res.data


def test_cli_add_user_and_verify(tmp_path, capsys):
    from rotary.sign.cli import main

    data = str(tmp_path / "data")
    assert main(["--data", data, "add-user", "it@acme.com", "--name", "IT", "--admin"]) == 0
    assert "temporary password" in capsys.readouterr().out
    assert main(["--data", data, "add-user", "it@acme.com", "--name", "IT"]) == 1
    assert main(["--data", data, "check"]) == 0
    unsealed = tmp_path / "plain.pdf"
    unsealed.write_bytes(make_pdf(1))
    assert main(["--data", data, "verify", str(unsealed)]) == 1


def test_config_file_then_environment(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    example = Path(__file__).parent.parent / "rotary-sign.example.toml"
    (data / "rotary-sign.toml").write_text(example.read_text().replace('organisation = "Example Ltd"', 'organisation = "File Ltd"'))
    cfg = load(data, env={"ROTARY_SIGN_LINK_DAYS": "7", "ROTARY_SIGN_URL": "https://sign.file.example/"})
    assert cfg.organisation == "File Ltd" and cfg.link_days == 7 and cfg.trust_proxy is True
    assert cfg.url == "https://sign.file.example" and cfg.secure_cookies
    with pytest.raises(ValueError):
        load(data, env={"ROTARY_SIGN_SMTP_SECURITY": "tls-ish"})


def test_double_send_emails_once_and_titles_stay_on_one_line(app):
    sender = setup_admin(app, app.test_client())
    envelope_id = create_envelope(sender, TWO_SIGNERS, FIELDS, sequential=False, title="Bad\r\nBcc: evil@example.com")
    headers = {"X-CSRF-Token": csrf(sender)}
    assert sender.post(f"/envelopes/{envelope_id}/send", headers=headers).status_code == 200
    assert sender.post(f"/envelopes/{envelope_id}/send", headers=headers).status_code == 409
    mails = outbox(app)
    assert len(mails) == 2
    assert all(m["Subject"] == "Please sign: Bad Bcc: evil@example.com" and m["Bcc"] is None for m in mails)
