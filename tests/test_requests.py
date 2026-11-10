"""Getting signatures without a server: signer copies, reading returns, finishing, and the desktop API."""

import http.client
import io
import json
import threading
from pathlib import Path

import pytest

pytest.importorskip("pypdf")

from pypdf import PdfReader, PdfWriter  # noqa: E402
from reportlab.lib.pagesizes import letter  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from rotary.sign import local, outlook  # noqa: E402
from rotary.sign import pdf as P  # noqa: E402

from .test_sign import make_pdf, text_of  # noqa: E402

REQ = "abcdef123456"
NAMES = ["Ada Lovelace", "Grace Hopper"]
FIELDS = [
    {"id": "s0", "kind": "signature", "page": 0, "x": 0.1, "y": 0.7, "w": 0.3, "h": 0.06, "signer": 0, "required": True},
    {"id": "d0", "kind": "date", "page": 0, "x": 0.1, "y": 0.8, "w": 0.2, "h": 0.03, "signer": 0, "required": False},
    {"id": "n0", "kind": "name", "page": 0, "x": 0.1, "y": 0.85, "w": 0.3, "h": 0.03, "signer": 0, "required": False},
    {"id": "s1", "kind": "signature", "page": 0, "x": 0.55, "y": 0.7, "w": 0.3, "h": 0.06, "signer": 1, "required": True},
    {"id": "c1", "kind": "checkbox", "page": 1, "x": 0.1, "y": 0.2, "w": 0.03, "h": 0.023, "signer": 1, "required": True},
]


def mine(signer):
    return [f for f in FIELDS if f["signer"] == signer]


def save(writer) -> bytes:
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def draw_on(data: bytes, page: int, draw) -> bytes:
    """Add page content, as Fill & Sign or a 'print to PDF' would."""
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    layer = io.BytesIO()
    c = canvas.Canvas(layer, pagesize=letter)
    draw(c)
    c.save()
    writer.pages[page].merge_page(PdfReader(io.BytesIO(layer.getvalue())).pages[0])
    return save(writer)


def fill(data: bytes, page: int, values: dict) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer.update_page_form_field_values(writer.pages[page], values, auto_regenerate=False)
    return save(writer)


def scribble(x, y):
    def draw(c):
        c.setStrokeColorRGB(0.1, 0.15, 0.5)
        c.setLineWidth(2.2)
        path = c.beginPath()
        path.moveTo(x, y)
        path.curveTo(x + 30, y + 35, x + 60, y - 10, x + 90, y + 20)
        path.lineTo(x + 150, y + 4)
        c.drawPath(path)
    return draw


@pytest.fixture(scope="module")
def original():
    return P.prepare(make_pdf(2))


@pytest.fixture(scope="module")
def copies(original):
    return [
        local.make_copy(original, mine(i), request_id=REQ, signer=i, signer_name=n, sender="Bea Boss", title="Supply Agreement")
        for i, n in enumerate(NAMES)
    ]


# ------------------------------------------------------------------ the copy a signer gets


def test_copy_has_instructions_then_only_that_signers_fields(copies):
    reader = PdfReader(io.BytesIO(copies[0]))
    assert len(reader.pages) == 3
    assert "Please sign: Supply Agreement" in text_of(copies[0], 0) and "Bea Boss" in text_of(copies[0], 0)
    fields = reader.get_fields()
    assert set(fields) == {f"rotary_{REQ}_0_s0", f"rotary_{REQ}_0_d0", f"rotary_{REQ}_0_n0"}
    assert fields[f"rotary_{REQ}_0_n0"]["/V"] == "Ada Lovelace"
    assert reader.metadata["/RotaryRequest"] == REQ
    # The fields sit where they were placed: page 2 of the copy, signature box at 10%/70%.
    widget = next(a.get_object() for a in reader.pages[1]["/Annots"] if a.get_object()["/T"] == f"rotary_{REQ}_0_s0")
    x0, y0, x1, y1 = (float(v) for v in widget["/Rect"])
    assert (round(x0), round(y1)) == (round(0.1 * 612), round(792 - 0.7 * 792))


# ------------------------------------------------------------------ reading a returned copy


def test_typed_values_are_read_from_the_form(copies):
    back = fill(copies[0], 1, {f"rotary_{REQ}_0_s0": "Ada Lovelace", f"rotary_{REQ}_0_d0": "9 Nov 2026"})
    result = local.read_return(copies[0], back, mine(0), request_id=REQ, signer=0, signer_names=NAMES)
    assert result.issues == [] and result.form_found
    assert result.values == {"s0": {"value": "Ada Lovelace"}, "d0": {"value": "9 Nov 2026"}}


def test_drawn_signature_and_tick_are_read_from_the_page(copies):
    back = draw_on(copies[1], 1, scribble(0.55 * 612 + 10, 792 - 0.76 * 792 + 8))
    back = fill(back, 2, {f"rotary_{REQ}_1_c1": "/Yes"})
    result = local.read_return(copies[1], back, mine(1), request_id=REQ, signer=1, signer_names=NAMES)
    assert result.issues == []
    assert result.values["s1"]["value"].startswith("data:image/png;base64,") and "rect" in result.values["s1"]
    assert result.values["c1"] == {"value": "true"}


def test_missing_required_fields_are_reported(copies):
    result = local.read_return(copies[1], copies[1], mine(1), request_id=REQ, signer=1, signer_names=NAMES)
    assert result.issues == ["Missing a signature on page 1.", "Missing a required tick box on page 2."]


def test_changes_outside_the_fields_are_caught(copies):
    back = fill(copies[0], 1, {f"rotary_{REQ}_0_s0": "Ada Lovelace"})

    def edit(c):
        c.setFont("Helvetica", 11)
        c.drawString(300, 600, "Price: $1")

    result = local.read_return(copies[0], draw_on(back, 1, edit), mine(0), request_id=REQ, signer=0, signer_names=NAMES)
    assert result.issues == ["Page 1 has changes outside the signing fields."]


def test_wrong_copies_are_refused(copies):
    back = fill(copies[0], 1, {f"rotary_{REQ}_0_s0": "Ada"})
    with pytest.raises(P.SignError, match="This is Ada Lovelace's copy, not Grace Hopper's"):
        local.read_return(copies[1], back, mine(1), request_id=REQ, signer=1, signer_names=NAMES)
    with pytest.raises(P.SignError, match="different signing request"):
        local.read_return(copies[0], back, mine(0), request_id="000000000000", signer=0, signer_names=NAMES)
    with pytest.raises(P.SignError, match="readable PDF"):
        local.read_return(copies[0], b"nope", mine(0), request_id=REQ, signer=0, signer_names=NAMES)
    with pytest.raises(P.SignError, match="has 1 pages"):
        local.read_return(copies[0], make_pdf(1), mine(0), request_id=REQ, signer=0, signer_names=NAMES)


def test_flattened_copy_without_the_instructions_page_still_reads(copies):
    """A 'print to PDF' loses the form and someone deleted page 1: read it from the pixels."""
    reader = PdfReader(io.BytesIO(copies[0]))
    writer = PdfWriter()
    for page in reader.pages[1:]:
        writer.add_page(page)
    for page in writer.pages:
        page.pop("/Annots", None)
    flat = draw_on(save(writer), 0, scribble(0.1 * 612 + 12, 792 - 0.76 * 792 + 8))
    result = local.read_return(copies[0], flat, mine(0), request_id=REQ, signer=0, signer_names=NAMES)
    assert not result.form_found and result.issues == []
    assert result.values["s0"]["value"].startswith("data:image/png")


# ------------------------------------------------------------------ finishing


def test_finish_puts_everyone_on_the_original(original, copies, tmp_path):
    ada = fill(copies[0], 1, {f"rotary_{REQ}_0_s0": "Ada Lovelace"})
    grace = fill(draw_on(copies[1], 1, scribble(0.55 * 612 + 10, 792 - 0.76 * 792 + 8)), 2, {f"rotary_{REQ}_1_c1": "/Yes"})
    request = {
        "id": REQ, "title": "Supply Agreement", "sender": "Bea Boss", "created_at": "2026-11-10 10:00:00",
        "original_sha256": P.sha256(original), "fields": FIELDS, "events": [],
        "signers": [{"name": n, "email": "x@y.com", "returned_at": "2026-11-10 12:00:00", "returned_sha256": "ab" * 32} for n in NAMES],
    }
    for i, back in enumerate([ada, grace]):
        request["signers"][i]["values"] = local.read_return(copies[i], back, mine(i), request_id=REQ, signer=i, signer_names=NAMES).values
    key, cert = P.create_seal_certificate(tmp_path, "Bea Boss")
    final = local.finish(original, request, seal_with=P.load_sealer(key=key, cert=cert))
    assert len(PdfReader(io.BytesIO(final)).pages) == 3
    page1 = text_of(final, 0)
    assert "Ada Lovelace" in page1 and "10 Nov 2026" in page1  # blank date: the day the copy came back
    assert "Certificate of completion" in text_of(final, 2) and "Returned file" in text_of(final, 2)
    assert P.verify(final, [cert.read_bytes()])[0]["intact"]


# ------------------------------------------------------------------ the desktop app's API


@pytest.fixture
def server(tmp_path, monkeypatch):
    from rotary.engine import Engine
    from rotary.web import requests_api
    from rotary.web.server import make_server

    monkeypatch.setattr("rotary.app.data_dir", lambda: tmp_path / "appdata")
    revealed = []
    monkeypatch.setattr(requests_api, "_reveal", revealed.append)
    srv = make_server(Engine(), "127.0.0.1", 0, data_dir=tmp_path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.revealed = revealed
    yield srv
    srv.shutdown()


def call(srv, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=60)
    hdrs = {"X-Rotary": "1"} if method == "POST" else {}
    hdrs.update(headers or {})
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
        hdrs["Content-Type"] = "application/json"
    conn.request(method, path, body=body, headers=hdrs)
    res = conn.getresponse()
    data = res.read()
    conn.close()
    if res.getheader("Content-Type", "").startswith("application/json"):
        data = json.loads(data)
    return res, data


def test_request_lifecycle_over_http(server, tmp_path):
    res, req = call(server, "POST", "/api/requests?name=Supply%20Agreement.pdf&sender=Bea%20Boss", make_pdf(2))
    assert res.status == 201 and req["status"] == "draft" and req["title"] == "Supply Agreement" and len(req["pages"]) == 2
    rid = req["id"]
    assert call(server, "POST", f"/api/requests/{rid}/send")[0].status == 422  # no signers yet
    signers = [{"name": n, "email": n.split()[0].lower() + "@example.org"} for n in NAMES]
    res, req = call(server, "POST", f"/api/requests/{rid}/draft", {"title": "Supply\nAgreement", "signers": signers, "fields": FIELDS})
    assert res.status == 200 and req["title"] == "Supply Agreement" and len(req["fields"]) == 5
    res, req = call(server, "POST", f"/api/requests/{rid}/send")
    assert req["status"] == "out" and [s["status"] for s in req["signers"]] == ["waiting", "waiting"]
    assert req["signers"][0]["mail"]["subject"] == "Please sign: Supply Agreement"
    assert call(server, "POST", f"/api/requests/{rid}/draft", {"signers": [], "fields": []})[0].status == 409

    copies = []
    for i in range(2):
        res, data = call(server, "GET", f"/api/requests/{rid}/copy/{i}")
        assert res.status == 200 and res.getheader("X-Rotary-Filename").endswith("to%20sign.pdf")
        copies.append(data)
    ada = fill(copies[0], 1, {f"rotary_{rid}_0_s0": "Ada Lovelace"})
    res, req = call(server, "POST", f"/api/requests/{rid}/return/0", ada)
    assert req["signers"][0]["status"] == "signed"
    # Grace's first copy is missing her tick box: it needs a look, and finishing waits.
    grace_partial = draw_on(copies[1], 1, scribble(0.55 * 612 + 10, 792 - 0.76 * 792 + 8))
    res, req = call(server, "POST", f"/api/requests/{rid}/return/1", grace_partial)
    assert req["signers"][1]["status"] == "check" and "tick box" in req["signers"][1]["issues"][0]
    assert call(server, "POST", f"/api/requests/{rid}/finish", {"seal": False})[0].status == 409
    assert call(server, "POST", f"/api/requests/{rid}/accept/1", {"reason": ""})[0].status == 422
    res, req = call(server, "POST", f"/api/requests/{rid}/accept/1", {"reason": "She confirmed by phone"})
    assert req["signers"][1]["status"] == "signed" and req["signers"][1]["accepted_reason"] == "She confirmed by phone"

    res, req = call(server, "POST", f"/api/requests/{rid}/finish", {"seal": True})
    assert req["status"] == "completed"
    res, final = call(server, "GET", f"/api/requests/{rid}/signed")
    assert res.getheader("X-Rotary-Filename") == "Supply%20Agreement%20-%20signed.pdf"
    assert "Ada Lovelace" in text_of(final, 0) and "confirmed by phone" in text_of(final, 2)
    assert P.verify(final, [(tmp_path / "appdata" / "seal" / "seal-cert.pem").read_bytes()])[0]["intact"]

    assert call(server, "POST", f"/api/requests/{rid}/reveal/signed")[0].status == 200
    assert server.revealed and server.revealed[-1].name == "signed.pdf"
    res, listing = call(server, "GET", "/api/requests")
    assert listing["requests"][0]["status"] == "completed" and listing["requests"][0]["signed"] == 2
    assert call(server, "POST", f"/api/requests/{rid}/delete")[0].status == 200
    assert call(server, "GET", f"/api/requests/{rid}")[0].status == 404


def test_api_guards(server):
    assert call(server, "POST", "/api/requests?name=x.pdf", make_pdf(1), headers={"X-Rotary": ""})[0].status == 403
    assert call(server, "GET", "/api/requests/..%2F..%2Fetc")[0].status == 404
    assert call(server, "GET", "/api/requests/zzzzzzzzzzzz")[0].status == 404
    res, err = call(server, "POST", "/api/requests?name=x.pdf", b"not a pdf")
    assert res.status == 422 and "readable PDF" in err["error"]
    res, req = call(server, "POST", "/api/requests?name=x.pdf", make_pdf(1))
    assert call(server, "POST", f"/api/requests/{req['id']}/return/0", make_pdf(1))[0].status == 404  # no such signer
    bad = {"signers": [{"name": "A", "email": "not-an-email"}], "fields": []}
    assert call(server, "POST", f"/api/requests/{req['id']}/draft", bad)[0].status == 422


def test_outlook_draft(server, monkeypatch):
    class Mail:
        def __init__(self):
            self.attached, self.shown = [], False
            self.Attachments = type("A", (), {"Add": lambda _s, path: self.attached.append(path)})()

        def Display(self, modal):
            self.shown = modal is False

    mails = []

    class App:
        def CreateItem(self, kind):
            assert kind == 0
            mails.append(Mail())
            return mails[-1]

    monkeypatch.setattr(outlook, "_outlook", lambda: App())
    res, req = call(server, "POST", "/api/requests?name=Deal.pdf&sender=Bea", make_pdf(1))
    rid = req["id"]
    call(server, "POST", f"/api/requests/{rid}/draft", {"signers": [{"name": "Ada Lovelace", "email": "ada@example.org"}], "fields": [dict(FIELDS[0])]})
    call(server, "POST", f"/api/requests/{rid}/send")
    assert call(server, "POST", f"/api/requests/{rid}/email/0")[0].status == 200
    [mail] = mails
    assert mail.To == "ada@example.org" and mail.Subject == "Please sign: Deal" and mail.shown
    assert Path(mail.attached[0]).name == "Deal - for Ada Lovelace to sign.pdf" and Path(mail.attached[0]).exists()

    def broken():
        raise RuntimeError("Outlook isn't set up")

    monkeypatch.setattr(outlook, "_outlook", broken)
    res, err = call(server, "POST", f"/api/requests/{rid}/email/0")
    assert res.status == 502 and "Outlook couldn't create the email" in err["error"]


@pytest.mark.slow
def test_get_signatures_tab_in_a_browser(server, tmp_path):
    import os

    sync_api = pytest.importorskip("playwright.sync_api")
    pdf_path = tmp_path / "Deal.pdf"
    pdf_path.write_bytes(make_pdf(1))
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=os.environ.get("ROTARY_TEST_CHROMIUM"))
        except Exception as exc:
            pytest.skip(f"no browser available: {exc}")
        page = browser.new_page(accept_downloads=True)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
        page.click("#tab-requests")
        page.fill("#req-me", "Bea Boss")
        page.set_input_files("#req-picker", str(pdf_path))
        page.wait_for_selector(".add-signer")
        page.fill(".add-signer input[type=text]", "Ada Lovelace")
        page.fill(".add-signer input[type=email]", "ada@example.org")
        page.click(".add-signer .tool")
        page.wait_for_selector(".rs-page img")
        layer = page.locator(".rs-layer").first
        box = layer.bounding_box()
        page.click("#req-side .tool[data-kind=signature]")
        layer.click(position={"x": box["width"] * 0.3, "y": box["height"] * 0.7})  # scrolls it into view first
        page.wait_for_timeout(1000)  # autosave
        page.click("#req-send")
        page.wait_for_selector(".req-card")
        with page.expect_download() as download:
            page.click(".req-card button:has-text('Download copy')")
        copy = Path(download.value.path()).read_bytes()
        reader = PdfReader(io.BytesIO(copy))
        [name] = reader.get_fields()
        signed = tmp_path / "back.pdf"
        signed.write_bytes(fill(copy, 1, {name: "Ada Lovelace"}))
        page.set_input_files("#req-up-0", str(signed))
        page.wait_for_function("() => document.querySelector('.req-card .req-chip')?.textContent === 'Signed'")
        with page.expect_download() as download:
            page.click("#req-side button.go")
        final = Path(download.value.path()).read_bytes()
        assert "Ada Lovelace" in text_of(final, 0) and "Certificate of completion" in text_of(final, 1)
        assert not errors
        browser.close()
