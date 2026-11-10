"""A quick end-to-end check of the installed backends.

``Rotary.exe --selftest report.txt`` converts a handful of generated sample
files and writes what worked. The Windows build runs it in CI, and it's a
handy thing to send IT if a conversion misbehaves on a locked-down PC.
"""

from __future__ import annotations

import json
import math
import platform
import struct
import sys
import tempfile
import wave
import zipfile
from pathlib import Path

from . import __version__
from .engine import Engine

# Each check may use the output of an earlier one as its input (markdown -> docx -> pdf).
CHECKS = [
    ("png", "jpeg"),
    ("png", "webp"),
    ("png", "pdf"),
    ("pdf", "png"),
    ("png", "heic"),
    ("heic", "jpeg"),
    ("json", "yaml"),
    ("json", "xlsx"),
    ("xlsx", "csv"),
    ("zip", "tar.gz"),
    ("markdown", "html"),
    ("markdown", "docx"),
    ("docx", "pdf"),
    ("wav", "mp3"),
    ("wav", "flac"),
]


def _samples(folder: Path) -> dict[str, Path]:
    from PIL import Image, ImageDraw

    png = folder / "sample.png"
    im = Image.new("RGBA", (96, 64), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((8, 8, 88, 56), fill=(217, 72, 31, 255))
    im.save(png)

    data = folder / "sample.json"
    data.write_text(json.dumps([{"name": "tea", "qty": 2}, {"name": "jam", "qty": 1}]), encoding="utf-8")

    archive = folder / "sample.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("hello.txt", "hello")

    markdown = folder / "sample.md"
    markdown.write_text("# Rotary\n\nA *quick* self-test.\n", encoding="utf-8")

    audio = folder / "sample.wav"
    with wave.open(str(audio), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 8))) for i in range(16000)))

    return {"png": png, "json": data, "zip": archive, "markdown": markdown, "wav": audio}


def _text_pdf() -> bytes:
    import io

    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 760, "Rotary self-test")
    c.save()
    return buf.getvalue()


def _request_round_trip(folder: Path) -> str:
    import io

    from pypdf import PdfReader, PdfWriter

    from .sign import local
    from .sign import pdf as sign_pdf

    original = sign_pdf.prepare(_text_pdf())
    fields = [{"id": "s", "kind": "signature", "page": 0, "x": 0.1, "y": 0.5, "w": 0.4, "h": 0.06, "signer": 0, "required": True}]
    copy = local.make_copy(original, fields, request_id="0123456789ab", signer=0, signer_name="Test Signer", sender="Self-test", title="Self-test")
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(copy)))
    writer.update_page_form_field_values(writer.pages[1], {"rotary_0123456789ab_0_s": "Test Signer"}, auto_regenerate=False)
    back = io.BytesIO()
    writer.write(back)
    result = local.read_return(copy, back.getvalue(), fields, request_id="0123456789ab", signer=0, signer_names=["Test Signer"])
    if result.issues or result.values.get("s", {}).get("value") != "Test Signer":
        raise RuntimeError(f"read back {result.values} with issues {result.issues}")
    request = {
        "id": "0123456789ab", "title": "Self-test", "sender": "Self-test", "created_at": local.now(), "fields": fields, "events": [],
        "original_sha256": sign_pdf.sha256(original),
        "signers": [{"name": "Test Signer", "email": "", "returned_at": local.now(), "values": result.values}],
    }
    try:
        key, cert = sign_pdf.create_seal_certificate(folder / "seal", "Self-test")
        sealer = sign_pdf.load_sealer(key=key, cert=cert)
    except ImportError:
        if getattr(sys, "frozen", False):
            raise  # the packaged app must be able to seal
        sealer = None
    final = local.finish(original, request, seal_with=sealer)
    if sealer is None:
        return "sealing skipped: pyHanko isn't installed"
    if not sign_pdf.verify(final, [cert.read_bytes()])[0]["intact"]:
        raise RuntimeError("the seal didn't verify")
    return "sealed and verified"


def run(report: Path) -> int:
    engine = Engine()
    lines = [f"Rotary {__version__}", f"Python {sys.version.split()[0]} on {platform.platform()}", ""]
    for converter in engine.converters:
        lines.append(f"backend {converter.label:<18} {'ok' if converter in engine.available else 'missing'}")
    lines.append("")

    failures = 0
    if sys.platform == "win32":
        # Needed to drive Microsoft Office. Checked even without Office installed, so a
        # packaging mistake shows up here rather than as "Office not found" on someone's PC.
        try:
            import pythoncom  # noqa: F401
            import win32com.client  # noqa: F401

            lines.append("ok   Office automation (pywin32) is available")
        except ImportError as exc:
            failures += 1
            lines.append(f"FAIL Office automation (pywin32): {exc}")
    with tempfile.TemporaryDirectory(prefix="rotary-selftest-") as tmp:
        samples = _samples(Path(tmp))
        for src, dst in CHECKS:
            label = f"{src} -> {dst}"
            if src not in samples:
                lines.append(f"skip {label} (no {src} sample: an earlier step was skipped)")
            elif dst not in engine.routes_from(src):
                lines.append(f"skip {label} (needs a backend that isn't installed)")
            else:
                try:
                    result = engine.convert(samples[src], dst, out_dir=Path(tmp) / f"{src}-{dst}")
                    samples.setdefault(dst, result.output)
                    lines.append(f"ok   {label}  ({result.seconds:.1f}s via {', '.join(result.route.backends)})")
                except Exception as exc:
                    failures += 1
                    lines.append(f"FAIL {label}: {exc}")

        # The desktop Sign tab: place a field on a PDF and stamp it.
        try:
            from .sign import pdf as sign_pdf

            doc = sign_pdf.prepare(_text_pdf())
            stamped = sign_pdf.stamp(doc, [sign_pdf.Field("text", 0, 0.1, 0.1, 0.5, 0.05, "Signed by selftest")])
            sign_pdf.render_page(stamped, 0, 300)
            lines.append("ok   sign a PDF")
        except Exception as exc:
            failures += 1
            lines.append(f"FAIL sign a PDF: {exc}")

        # Get signatures: make a signer's copy, fill it in as their PDF reader would, read it back, finish.
        try:
            lines.append(f"ok   get signatures  ({_request_round_trip(Path(tmp))})")
        except Exception as exc:
            failures += 1
            lines.append(f"FAIL get signatures: {exc}")

    lines += ["", "all good" if not failures else f"{failures} check(s) failed"]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if failures else 0
