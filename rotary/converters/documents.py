"""Markup and document conversion with Pandoc."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

from .base import Converter, Edge, Options, run, which

# Rotary format id -> Pandoc reader / writer name.
READERS = {
    "markdown": "markdown",
    "html": "html",
    "docx": "docx",
    "odt": "odt",
    "rtf": "rtf",
    "rst": "rst",
    "latex": "latex",
    "epub": "epub",
    "org": "org",
    "asciidoc": "asciidoc",
    "mediawiki": "mediawiki",
    "ipynb": "ipynb",
}
WRITERS = {
    "markdown": "gfm",
    "html": "html5",
    "docx": "docx",
    "odt": "odt",
    "rtf": "rtf",
    "rst": "rst",
    "latex": "latex",
    "epub": "epub3",
    "org": "org",
    "asciidoc": "asciidoc",
    "mediawiki": "mediawiki",
    "ipynb": "ipynb",
    "txt": "plain",
    "pptx": "pptx",
}
STANDALONE = {"html", "latex", "rtf", "txt"}
# Between word-processor formats LibreOffice keeps far more of the layout than Pandoc,
# so make Pandoc the fallback there.
OFFICE = {"docx", "odt", "rtf", "doc"}
# PDF engines Pandoc can drive, best Unicode support first.
PDF_ENGINES = ["tectonic", "xelatex", "lualatex", "typst", "weasyprint", "wkhtmltopdf", "pdflatex"]


class DocumentConverter(Converter):
    name = "documents"
    label = "Pandoc"
    requirement = "install Pandoc (https://pandoc.org/installing.html)"

    @cached_property
    def pandoc(self) -> str | None:
        return which("pandoc")

    def is_available(self) -> bool:
        return self.pandoc is not None

    @cached_property
    def pdf_engine(self) -> str | None:
        return next((e for e in PDF_ENGINES if which(e)), None)

    def _supported(self, flag: str) -> set[str]:
        return set(run([self.pandoc, flag], what="pandoc", timeout=30).split())

    @cached_property
    def _edges(self) -> list[Edge]:
        inputs, outputs = self._supported("--list-input-formats"), self._supported("--list-output-formats")
        readers = [f for f, r in READERS.items() if r in inputs]
        writers = [f for f, w in WRITERS.items() if w in outputs]
        edges = [
            Edge(s, t, self, cost=3.5 if (s in OFFICE and t in OFFICE) else 1.0)
            for s in readers
            for t in writers
            if s != t
        ]
        if self.pdf_engine:
            edges += self._pairs(readers, ["pdf"], cost=3.5)
        return edges

    def edges(self) -> list[Edge]:
        return self._edges

    def convert(self, src: Path, dst: Path, src_fmt: str, dst_fmt: str, options: Options) -> None:
        cmd = [self.pandoc, str(src), "-f", READERS[src_fmt], "-o", str(dst)]
        cmd.append(f"--resource-path={src.parent}")
        if dst_fmt == "pdf":
            cmd += ["--standalone", f"--pdf-engine={self.pdf_engine}"]
        else:
            cmd += ["-t", WRITERS[dst_fmt]]
            if dst_fmt in STANDALONE:
                cmd.append("--standalone")
            if dst_fmt == "html":
                # One self-contained file, with any images inlined.
                cmd.append("--embed-resources")
        run(cmd, what="Pandoc", timeout=600)
