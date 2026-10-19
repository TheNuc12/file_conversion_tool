"""PDF rendering and text extraction with pdfium, and plain text to PDF with ReportLab."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

from .._pdfium import LOCK
from .base import ConversionError, Converter, Edge, Options
from .images import save_image, writable_formats


def _have(module: str) -> bool:
    try:
        __import__(module)
    except ImportError:
        return False
    return True


class PdfConverter(Converter):
    name = "pdf"
    label = "pdfium/ReportLab"
    requirement = "pip install pypdfium2 reportlab"

    def is_available(self) -> bool:
        return _have("pypdfium2") or _have("reportlab")

    @cached_property
    def _edges(self) -> list[Edge]:
        edges = []
        if _have("pypdfium2"):
            edges += self._pairs(["pdf"], writable_formats())
            # Extracted text has lost all structure; feeding it on to a document format
            # would dress that up as a faithful conversion.
            edges += self._pairs(["pdf"], ["txt"], terminal=True)
        if _have("reportlab"):
            # Fallback only: LibreOffice lays out text (and non-Latin scripts) better. Only
            # for real text files: after DOCX -> TXT it would pass off a formatting-free
            # PDF as "Word to PDF".
            edges += self._pairs(["txt"], ["pdf"], cost=4, initial=True)
        return edges

    def edges(self) -> list[Edge]:
        return self._edges

    def convert(self, src: Path, dst: Path, src_fmt: str, dst_fmt: str, options: Options) -> None:
        if src_fmt == "txt":
            self._text_to_pdf(src, dst)
        elif dst_fmt == "txt":
            self._pdf_to_text(src, dst)
        else:
            self._render(src, dst, dst_fmt, options)

    def _open(self, src: Path):
        import pypdfium2 as pdfium

        try:
            return pdfium.PdfDocument(src)
        except pdfium.PdfiumError as exc:
            raise ConversionError(f"Couldn't open {src.name} as a PDF: {exc}") from exc

    def _render(self, src: Path, dst: Path, dst_fmt: str, options: Options) -> None:
        with LOCK:
            pdf = self._open(src)
            try:
                page_no = options.page or 1
                if not 1 <= page_no <= len(pdf):
                    raise ConversionError(f"{src.name} has {len(pdf)} page(s); there is no page {page_no}")
                scale = (options.dpi or 150) / 72
                image = pdf[page_no - 1].render(scale=scale).to_pil()
            finally:
                pdf.close()
        save_image(image, dst, dst_fmt, options)

    def _pdf_to_text(self, src: Path, dst: Path) -> None:
        with LOCK:
            pdf = self._open(src)
            try:
                pages = []
                for page in pdf:
                    textpage = page.get_textpage()
                    pages.append(textpage.get_text_range().replace("\r\n", "\n").strip())
                    textpage.close()
                    page.close()
            finally:
                pdf.close()
        dst.write_text("\n\n\f".join(pages) + "\n", encoding="utf-8")

    def _text_to_pdf(self, src: Path, dst: Path) -> None:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.pdfbase.pdfmetrics import stringWidth
        from reportlab.pdfgen import canvas

        text = src.read_text(encoding="utf-8", errors="replace")
        font, size, leading, margin = "Helvetica", 10.5, 14, 20 * mm
        width, height = A4
        usable = width - 2 * margin

        c = canvas.Canvas(str(dst), pagesize=A4)
        c.setTitle(src.stem)
        y = height - margin

        def emit(line: str) -> None:
            nonlocal y
            if y < margin:
                c.showPage()
                y = height - margin
            c.setFont(font, size)
            c.drawString(margin, y, line)
            y -= leading

        for paragraph in text.expandtabs(4).split("\n"):
            if paragraph.startswith("\f"):
                c.showPage()
                y = height - margin
                paragraph = paragraph.lstrip("\f")
            line = ""
            for word in paragraph.split(" "):
                candidate = f"{line} {word}" if line else word
                if stringWidth(candidate, font, size) <= usable:
                    line = candidate
                    continue
                if line:
                    emit(line)
                # Break words that are wider than the page on their own.
                while stringWidth(word, font, size) > usable:
                    cut = len(word)
                    while cut > 1 and stringWidth(word[:cut], font, size) > usable:
                        cut -= 1
                    emit(word[:cut])
                    word = word[cut:]
                line = word
            emit(line)
        c.save()
