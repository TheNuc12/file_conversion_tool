"""PDF work for signing: page images, stamping fields, the completion certificate and the seal.

Field positions are fractions of the page (0-1, origin top-left), exactly as
the browser sees them over a page image, so the UI never needs to know about
PDF points. ``prepare`` bakes any page rotation into the content first, which
keeps page images, field positions and stamping in one upright coordinate system.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import io
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

FIELD_KINDS = ("signature", "initials", "date", "name", "text", "checkbox")
MAX_SIGNATURE_BYTES = 2_000_000
MAX_SIGNATURE_PIXELS = 4000 * 4000


class SignError(ValueError):
    """Something wrong with the document or the fields that the user can fix."""


@dataclass
class Field:
    kind: str
    page: int  # 0-based
    x: float  # left edge, fraction of page width
    y: float  # top edge, fraction of page height
    w: float
    h: float
    value: str | None = None  # text; "true" for a ticked checkbox; a data:image/png URL for signatures
    signer: int | None = None  # which recipient fills it in (server only)
    required: bool = True
    id: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> Field:
        try:
            kind = str(data["kind"])
            page = int(data["page"])
            x, y, w, h = (float(data[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError) as exc:
            raise SignError(f"Malformed field: {exc}") from exc
        if kind not in FIELD_KINDS:
            raise SignError(f"Unknown field type {kind!r}")
        if page < 0 or not (0 < w <= 1 and 0 < h <= 1):
            raise SignError("Field is outside the page")
        # Keep the box on the page even if the browser rounded it slightly off the edge.
        x, y = min(max(x, 0.0), 1 - w), min(max(y, 0.0), 1 - h)
        value = data.get("value")
        signer = data.get("signer")
        return cls(
            kind=kind,
            page=page,
            x=x,
            y=y,
            w=w,
            h=h,
            value=None if value is None else str(value),
            signer=None if signer is None else int(signer),
            required=bool(data.get("required", True)),
            id=None if data.get("id") is None else str(data["id"])[:64],
        )

    def to_dict(self) -> dict:
        return asdict(self)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- reading


def prepare(data: bytes) -> bytes:
    """Check a PDF can be signed and normalise it (see module docstring)."""
    from pypdf import PdfReader, PdfWriter

    try:
        reader = PdfReader(io.BytesIO(data))
        encrypted = reader.is_encrypted
    except Exception as exc:
        raise SignError("That file isn't a readable PDF") from exc
    if encrypted:
        try:
            unlocked = reader.decrypt("")
        except Exception:
            unlocked = 0
        if not unlocked:
            raise SignError("This PDF is password-protected. Remove the password and try again.")
    try:
        if not reader.pages:
            raise SignError("This PDF has no pages")
        writer = PdfWriter(clone_from=reader)
        for page in writer.pages:
            if page.rotation % 360:
                page.transfer_rotation_to_content()
        out = io.BytesIO()
        writer.write(out)
    except SignError:
        raise
    except Exception as exc:
        raise SignError(f"Couldn't read this PDF: {exc}") from exc
    return out.getvalue()


def page_sizes(data: bytes) -> list[tuple[float, float]]:
    """Width and height of each page in points."""
    from pypdf import PdfReader

    return [(float(p.cropbox.width), float(p.cropbox.height)) for p in PdfReader(io.BytesIO(data)).pages]


def render_page(data: bytes, index: int, width: int = 1100) -> bytes:
    """A PNG of one page, ``width`` pixels wide, for placing or reviewing fields."""
    import pypdfium2 as pdfium

    from .._pdfium import LOCK

    with LOCK:
        pdf = pdfium.PdfDocument(data)
        try:
            if not 0 <= index < len(pdf):
                raise SignError(f"There is no page {index + 1}")
            page = pdf[index]
            image = page.render(scale=width / page.get_width()).to_pil()
        finally:
            pdf.close()
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- stamping


def signature_image(value: str):
    """Decode and check a data:image/png signature, raising SignError if it isn't acceptable."""
    from PIL import Image
    from reportlab.lib.utils import ImageReader

    prefix = "data:image/png;base64,"
    if not value.startswith(prefix):
        raise SignError("A signature must be a PNG image")
    try:
        raw = base64.b64decode(value[len(prefix):], validate=True)
    except ValueError as exc:
        raise SignError("The signature image is corrupt") from exc
    if len(raw) > MAX_SIGNATURE_BYTES:
        raise SignError("The signature image is too large")
    try:
        image = Image.open(io.BytesIO(raw))
        if image.format != "PNG" or image.width * image.height > MAX_SIGNATURE_PIXELS:
            raise SignError("The signature image is too large or not a PNG")
        image.load()
    except SignError:
        raise
    except Exception as exc:
        raise SignError("The signature image is corrupt") from exc
    return ImageReader(image.convert("RGBA"))


def _draw(c, field: Field, width: float, height: float) -> None:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    if field.value in (None, ""):
        return
    x, w, h = field.x * width, field.w * width, field.h * height
    y = height - (field.y + field.h) * height  # PDF measures from the bottom
    if field.kind in ("signature", "initials"):
        image = signature_image(field.value)
        iw, ih = image.getSize()
        scale = min(w / iw, h / ih)
        dw, dh = iw * scale, ih * scale
        c.drawImage(image, x + (w - dw) / 2, y + (h - dh) / 2, dw, dh, mask="auto")
    elif field.kind == "checkbox":
        if field.value == "true":
            # Drawn as lines: symbol fonts aren't embedded and some viewers show a box instead.
            size = min(w, h) * 0.8
            left, bottom = x + (w - size) / 2, y + (h - size) / 2
            c.setLineWidth(max(size * 0.14, 0.8))
            c.setLineCap(1)
            c.setLineJoin(1)
            tick = c.beginPath()
            tick.moveTo(left + size * 0.12, bottom + size * 0.5)
            tick.lineTo(left + size * 0.4, bottom + size * 0.18)
            tick.lineTo(left + size * 0.9, bottom + size * 0.85)
            c.drawPath(tick, stroke=1, fill=0)
    else:
        text = field.value.replace("\n", " ")
        size = min(h * 0.68, 13)
        while size > 4 and stringWidth(text, "Helvetica", size) > w - 2:
            size -= 0.25
        c.setFont("Helvetica", size)
        c.drawString(x + 1, y + (h - size * 0.7) / 2, text)


def stamp(data: bytes, fields: list[Field]) -> bytes:
    """Draw filled-in fields onto the pages. The result is flat: plain page content, not form fields."""
    from pypdf import PdfReader, PdfWriter, Transformation
    from reportlab.pdfgen import canvas

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    by_page: dict[int, list[Field]] = {}
    for field in fields:
        blank = field.value in (None, "") or (field.kind == "checkbox" and field.value != "true")
        if not blank:
            by_page.setdefault(field.page, []).append(field)
    for index, page_fields in sorted(by_page.items()):
        if index >= len(writer.pages):
            raise SignError(f"A field is on page {index + 1}, but the document has {len(writer.pages)}")
        page = writer.pages[index]
        box = page.cropbox
        width, height = float(box.width), float(box.height)
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(width, height))
        for field in page_fields:
            _draw(c, field, width, height)
        c.save()
        overlay = PdfReader(buf).pages[0]
        page.merge_transformed_page(overlay, Transformation().translate(float(box.left), float(box.bottom)))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# --------------------------------------------------------------------------- certificate


def certificate(record: dict) -> bytes:
    """The certificate-of-completion pages appended to a finished document.

    ``record`` holds: title, envelope_id, original_sha256, sender, created_at,
    completed_at, signers (name, email, signed_at, ip, user_agent) and events
    (at, actor, action, ip).
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=8, leading=10)
    cell = lambda text: Paragraph(_escape(str(text or "")), small)  # noqa: E731
    grid = TableStyle(
        [
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bbbbbb")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0eeea")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]
    )

    story = [
        Paragraph("Certificate of completion", styles["Title"]),
        Paragraph(_escape(record.get("title", "")), styles["Heading2"]),
        Spacer(1, 4 * mm),
    ]
    summary = [
        ["Envelope", record.get("envelope_id", "")],
        ["Sent by", record.get("sender", "")],
        ["Created", record.get("created_at", "")],
        ["Completed", record.get("completed_at", "")],
        ["Original document SHA-256", record.get("original_sha256", "")],
    ]
    t = Table([[cell(k), cell(v)] for k, v in summary], colWidths=[48 * mm, 122 * mm])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bbbbbb")), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [t, Spacer(1, 6 * mm), Paragraph("Signers", styles["Heading3"])]

    rows = [[cell("Name"), cell("Email"), cell("Signed (UTC)"), cell("IP address"), cell("Browser")]]
    for s in record.get("signers", []):
        rows.append([cell(s.get("name")), cell(s.get("email")), cell(s.get("signed_at")), cell(s.get("ip")), cell(s.get("user_agent"))])
    t = Table(rows, colWidths=[30 * mm, 42 * mm, 32 * mm, 24 * mm, 42 * mm], repeatRows=1)
    t.setStyle(grid)
    story += [t, Spacer(1, 6 * mm), Paragraph("Event history", styles["Heading3"])]

    rows = [[cell("Time (UTC)"), cell("Who"), cell("What"), cell("IP address")]]
    for e in record.get("events", []):
        rows.append([cell(e.get("at")), cell(e.get("actor")), cell(e.get("action")), cell(e.get("ip"))])
    t = Table(rows, colWidths=[34 * mm, 48 * mm, 64 * mm, 24 * mm], repeatRows=1)
    t.setStyle(grid)
    story += [
        t,
        Spacer(1, 6 * mm),
        Paragraph(
            "Each signer agreed to sign electronically before signing. The document is sealed with a digital "
            "signature: any change made after completion breaks the seal and is reported by PDF readers.",
            small,
        ),
    ]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title="Certificate of completion", leftMargin=20 * mm, rightMargin=20 * mm)
    doc.build(story)
    return buf.getvalue()


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def append(data: bytes, extra: bytes) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer.append(PdfReader(io.BytesIO(extra)))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# --------------------------------------------------------------------------- seal


def create_seal_certificate(folder: Path, organisation: str) -> tuple[Path, Path]:
    """Make a self-signed sealing key and certificate (used when IT hasn't supplied one)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    folder.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, f"{organisation} document seal"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, organisation),
        ]
    )
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=True, key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    key_path, cert_path = folder / "seal-key.pem", folder / "seal-cert.pem"
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    os.chmod(key_path, 0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


def load_sealer(*, key: Path | None = None, cert: Path | None = None, pkcs12: Path | None = None, passphrase: str | None = None):
    """A pyHanko signer from a PEM key + certificate, or from a .pfx/.p12 file."""
    from pyhanko.sign import signers

    secret = passphrase.encode() if passphrase else None
    if pkcs12:
        signer = signers.SimpleSigner.load_pkcs12(pfx_file=str(pkcs12), passphrase=secret)
    else:
        signer = signers.SimpleSigner.load(key_file=str(key), cert_file=str(cert), key_passphrase=secret)
    if signer is None:
        raise SignError("Couldn't load the sealing certificate (wrong file or passphrase?)")
    return signer


def seal(data: bytes, signer, *, reason: str, location: str = "", timestamp_url: str | None = None) -> bytes:
    """Digitally sign the whole file so any later change is detectable."""
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields, signers, timestamps

    meta = signers.PdfSignatureMetadata(
        field_name="DocumentSeal",
        reason=reason,
        location=location or None,
        md_algorithm="sha256",
        subfilter=fields.SigSeedSubFilter.PADES,
    )
    timestamper = timestamps.HTTPTimeStamper(timestamp_url) if timestamp_url else None
    pdf_signer = signers.PdfSigner(meta, signer=signer, timestamper=timestamper)
    out = pdf_signer.sign_pdf(IncrementalPdfFileWriter(io.BytesIO(data)))
    return out.getvalue()


def verify(data: bytes, trusted_cert_pems: list[bytes] = ()) -> list[dict]:
    """Check each digital signature in a PDF. ``intact`` is what matters for tampering."""
    from asn1crypto import pem, x509 as asn1_x509
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko.sign.validation.status import SignatureCoverageLevel
    from pyhanko_certvalidator import ValidationContext

    roots = []
    for blob in trusted_cert_pems:
        der = pem.unarmor(blob)[2] if pem.detect(blob) else blob
        roots.append(asn1_x509.Certificate.load(der))
    try:
        reader = PdfFileReader(io.BytesIO(data), strict=False)
        signatures = reader.embedded_signatures
    except Exception as exc:
        raise SignError("That file isn't a readable PDF") from exc

    results = []
    for sig in signatures:
        context = ValidationContext(trust_roots=roots, allow_fetching=False)
        try:
            with _quiet("pyhanko", "pyhanko_certvalidator"):  # an untrusted cert is an answer, not an error
                status = validate_pdf_signature(sig, context)
        except Exception:
            results.append({"field": sig.field_name, "intact": False, "trusted": False, "covers_whole_file": False, "signer": None, "signed_at": None})
            continue
        results.append(
            {
                "field": sig.field_name,
                "intact": bool(status.intact and status.valid),
                "trusted": bool(status.trusted),
                "covers_whole_file": status.coverage == SignatureCoverageLevel.ENTIRE_FILE,
                "signer": status.signing_cert.subject.human_friendly,
                "signed_at": status.signer_reported_dt.isoformat() if status.signer_reported_dt else None,
            }
        )
    return results


@contextlib.contextmanager
def _quiet(*names: str):
    loggers = [logging.getLogger(n) for n in names]
    levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        for lg, level in zip(loggers, levels):
            lg.setLevel(level)
