"""Signing core (rotary/sign/pdf.py) and the desktop app's Sign tab endpoints."""

import base64
import http.client
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("pypdf")

from rotary.sign import pdf as P  # noqa: E402


def make_pdf(pages=2, rotate_second=False, password=None) -> bytes:
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for i in range(pages):
        c.drawString(72, 720, f"Page {i + 1}")
        c.showPage()
    c.save()
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(buf.getvalue())))
    if rotate_second:
        writer.pages[1].rotate(90)
    if password:
        writer.encrypt(password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def signature_url(size=(300, 90)) -> str:
    from PIL import Image, ImageDraw

    im = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(im).line((5, size[1] - 10, size[0] // 2, 10, size[0] - 5, size[1] - 10), fill=(10, 20, 80, 255), width=8)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def ink(png: bytes, box) -> int:
    """Dark pixels inside a (left, top, right, bottom) fraction box of a rendered page."""
    from PIL import Image

    im = Image.open(io.BytesIO(png)).convert("L")
    w, h = im.size
    crop = im.crop((int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)))
    return sum(crop.histogram()[:128])


def text_of(data: bytes, page: int) -> str:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(data)
    try:
        return doc[page].get_textpage().get_text_range()
    finally:
        doc.close()


# ------------------------------------------------------------------ core


def test_prepare_rejects_bad_input():
    with pytest.raises(P.SignError, match="isn't a readable PDF"):
        P.prepare(b"hello")
    with pytest.raises(P.SignError, match="password-protected"):
        P.prepare(make_pdf(password="secret"))


def test_rotated_pages_are_normalised():
    doc = P.prepare(make_pdf(rotate_second=True))
    assert P.page_sizes(doc) == [(612.0, 792.0), (792.0, 612.0)]
    stamped = P.stamp(doc, [P.Field("text", 1, 0.1, 0.1, 0.5, 0.05, "Upright text")])
    assert "Upright text" in text_of(stamped, 1)
    # Stamped where it was placed: near the top-left of the landscape page.
    assert ink(P.render_page(stamped, 1, 800), (0.08, 0.08, 0.6, 0.17)) > 50


def test_signature_lands_inside_its_box_only():
    doc = P.prepare(make_pdf())
    field = P.Field("signature", 0, 0.5, 0.7, 0.3, 0.08, signature_url())
    png = P.render_page(P.stamp(doc, [field]), 0, 800)
    assert ink(png, (0.5, 0.7, 0.8, 0.78)) > 200
    assert ink(png, (0.0, 0.3, 0.45, 0.65)) == 0


def test_checkbox_and_empty_fields():
    doc = P.prepare(make_pdf(1))
    ticked = P.stamp(doc, [P.Field("checkbox", 0, 0.2, 0.2, 0.04, 0.03, "true")])
    unticked = P.stamp(doc, [P.Field("checkbox", 0, 0.2, 0.2, 0.04, 0.03, "false"), P.Field("text", 0, 0.5, 0.5, 0.2, 0.03, "")])
    assert ink(P.render_page(ticked, 0, 600), (0.2, 0.2, 0.24, 0.23)) > 5
    assert ink(P.render_page(unticked, 0, 600), (0.2, 0.2, 0.7, 0.55)) == 0


def test_field_validation():
    f = P.Field.from_dict({"kind": "date", "page": 0, "x": 0.95, "y": -0.1, "w": 0.2, "h": 0.05, "value": "today"})
    assert (round(f.x, 6), f.y) == (0.8, 0.0)  # pulled back onto the page
    for bad in ({"kind": "stamp", "page": 0, "x": 0, "y": 0, "w": 0.1, "h": 0.1}, {"kind": "text", "page": 0, "x": 0, "y": 0, "w": 2, "h": 0.1}, {"kind": "text"}):
        with pytest.raises(P.SignError):
            P.Field.from_dict(bad)
    doc = P.prepare(make_pdf(1))
    with pytest.raises(P.SignError, match="PNG"):
        P.stamp(doc, [P.Field("signature", 0, 0.1, 0.1, 0.2, 0.1, "data:image/svg+xml;base64,PHN2Zz4=")])
    with pytest.raises(P.SignError, match="on page 5"):
        P.stamp(doc, [P.Field("text", 4, 0.1, 0.1, 0.2, 0.1, "x")])


def test_certificate_and_seal(tmp_path):
    pytest.importorskip("pyhanko")
    doc = P.prepare(make_pdf(1))
    cert = P.certificate(
        {
            "title": "NDA <draft>", "envelope_id": "e1", "original_sha256": P.sha256(doc), "sender": "a@co.com",
            "signers": [{"name": "Ada", "email": "ada@x.com", "signed_at": "2026-01-01 10:00", "ip": "10.0.0.2", "user_agent": "Edge"}],
            "events": [{"at": "2026-01-01 09:00", "actor": "a@co.com", "action": "Sent", "ip": "10.0.0.1"}],
        }
    )
    full = P.append(doc, cert)
    assert "Certificate of completion" in text_of(full, 1) and "NDA <draft>" in text_of(full, 1)

    key, crt = P.create_seal_certificate(tmp_path, "Acme")
    sealed = P.seal(full, P.load_sealer(key=key, cert=crt), reason="Signed")
    [result] = P.verify(sealed, [crt.read_bytes()])
    assert result["intact"] and result["trusted"] and result["covers_whole_file"]
    assert P.verify(sealed)[0]["trusted"] is False  # an unknown certificate is reported, not trusted

    tampered = bytearray(sealed)
    tampered[sealed.index(b"stream") + 12] ^= 0x01  # inside the first page's content
    assert P.verify(bytes(tampered), [crt.read_bytes()])[0]["intact"] is False


# ------------------------------------------------------------------ desktop Sign tab


@pytest.fixture(scope="module")
def server():
    from rotary.engine import Engine
    from rotary.web.server import make_server

    srv = make_server(Engine(), "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request(method, path, body=body, headers=headers or {})
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res, data


def test_sign_tab_round_trip(server):
    res, body = call(server, "POST", "/api/sign/open?name=deal.pdf", make_pdf(), {"X-Rotary": "1"})
    assert res.status == 200
    doc = json.loads(body)
    assert doc["name"] == "deal.pdf" and len(doc["pages"]) == 2

    res, png = call(server, "GET", f"/api/sign/page?doc={doc['doc']}&n=1&w=400")
    assert res.status == 200 and png[:4] == b"\x89PNG"

    fields = [{"kind": "signature", "page": 0, "x": 0.1, "y": 0.8, "w": 0.3, "h": 0.06, "value": signature_url()},
              {"kind": "name", "page": 1, "x": 0.1, "y": 0.1, "w": 0.4, "h": 0.04, "value": "Ada Lovelace"}]
    res, signed = call(server, "POST", f"/api/sign/apply?doc={doc['doc']}", json.dumps({"fields": fields}), {"X-Rotary": "1"})
    assert res.status == 200 and res.getheader("X-Rotary-Filename") == "deal-signed.pdf"
    assert "Ada Lovelace" in text_of(signed, 1)


def test_sign_tab_errors(server):
    res, body = call(server, "POST", "/api/sign/open?name=x.pdf", b"not a pdf", {"X-Rotary": "1"})
    assert res.status == 422 and "readable PDF" in json.loads(body)["error"]
    res, _ = call(server, "POST", "/api/sign/open?name=x.pdf", make_pdf())  # no X-Rotary header
    assert res.status == 403
    res, _ = call(server, "GET", "/api/sign/page?doc=nope&n=0")
    assert res.status == 404
    res, body = call(server, "POST", "/api/sign/open?name=x.pdf", make_pdf(1), {"X-Rotary": "1"})
    doc = json.loads(body)["doc"]
    res, body = call(server, "POST", f"/api/sign/apply?doc={doc}", json.dumps({"fields": [{"kind": "bogus"}]}), {"X-Rotary": "1"})
    assert res.status == 422


def test_parallel_page_renders_do_not_crash(server):
    # PDFium isn't thread-safe; browsers fetch several page images at once.
    res, body = call(server, "POST", "/api/sign/open?name=many.pdf", make_pdf(6), {"X-Rotary": "1"})
    doc = json.loads(body)["doc"]
    with ThreadPoolExecutor(12) as pool:
        statuses = list(pool.map(lambda n: call(server, "GET", f"/api/sign/page?doc={doc}&n={n % 6}&w=300")[0].status, range(36)))
    assert statuses == [200] * 36


def test_sign_static_files_are_served(server):
    res, body = call(server, "GET", "/sign-static/rsign.js")
    assert res.status == 200 and b"window.RSign" in body
    res, _ = call(server, "GET", "/sign-static/../server.py")
    assert res.status == 404
