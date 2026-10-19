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


def run(report: Path) -> int:
    engine = Engine()
    lines = [f"Rotary {__version__}", f"Python {sys.version.split()[0]} on {platform.platform()}", ""]
    for converter in engine.converters:
        lines.append(f"backend {converter.label:<18} {'ok' if converter in engine.available else 'missing'}")
    lines.append("")

    failures = 0
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

    lines += ["", "all good" if not failures else f"{failures} check(s) failed"]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if failures else 0
