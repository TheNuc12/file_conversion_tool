"""Office document conversion with LibreOffice running headless."""

from __future__ import annotations

import atexit
import shutil
import tempfile
import threading
from functools import cached_property
from pathlib import Path

from .base import ConversionError, Converter, Edge, Options, run, which

WRITER_IN = ["doc", "docx", "odt", "rtf", "txt", "html"]
WRITER_OUT = ["pdf", "docx", "doc", "odt", "rtf", "txt", "html", "epub"]
CALC_IN = ["xls", "xlsx", "ods", "csv"]
CALC_OUT = ["pdf", "xlsx", "xls", "ods", "csv", "html"]
IMPRESS_IN = ["ppt", "pptx", "odp"]
IMPRESS_OUT = ["pdf", "pptx", "ppt", "odp", "png"]
DRAW_IN = ["svg"]
DRAW_OUT = ["png", "pdf"]
# PDFs open as positioned text boxes: they look right in Word, but other tools can't
# read them back, so these are final steps only.
PDF_IN = ["pdf"]
PDF_OUT = ["docx", "odt"]

CALC = set(CALC_IN)

# Export filters where LibreOffice's default for the extension isn't what we want.
WRITER_FILTERS = {"txt": "txt:Text (encoded):UTF8", "html": "html:XHTML Writer File:UTF8"}
# CSV options: comma, double quotes, UTF-8, from line 1, ..., don't quote every text cell.
CALC_FILTERS = {"csv": "csv:Text - txt - csv (StarCalc):44,34,76,1,,0,false,true,true", "html": "html:HTML (StarCalc)"}
PDF_FILTERS = {"docx": "docx:MS Word 2007 XML", "odt": "odt:writer8"}
IMPORT_FILTERS = {"csv": "CSV:44,34,76,1", "txt": "Text (encoded):UTF8", "pdf": "writer_pdf_import"}

KNOWN_PATHS = [
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
]

# LibreOffice is slow to start, so prefer any other backend that can do the same job.
COST = 3.0


class OfficeConverter(Converter):
    name = "office"
    label = "LibreOffice"
    requirement = "install LibreOffice (https://www.libreoffice.org/download/)"

    _lock = threading.Lock()  # one LibreOffice process at a time per profile

    @cached_property
    def soffice(self) -> str | None:
        found = which("soffice", "libreoffice")
        if found:
            return found
        return next((p for p in KNOWN_PATHS if Path(p).exists()), None)

    def is_available(self) -> bool:
        return self.soffice is not None

    @cached_property
    def _profile(self) -> Path:
        # A private profile keeps us from clashing with a LibreOffice window the user has open.
        path = Path(tempfile.mkdtemp(prefix="rotary-lo-"))
        atexit.register(shutil.rmtree, path, ignore_errors=True)
        return path

    def edges(self) -> list[Edge]:
        groups = [
            (WRITER_IN, WRITER_OUT),
            (CALC_IN, CALC_OUT),
            (IMPRESS_IN, IMPRESS_OUT),
            (DRAW_IN, DRAW_OUT),
            (PDF_IN, PDF_OUT),
        ]
        edges = []
        for sources, targets in groups:
            for src in sources:
                for dst in targets:
                    if src == dst:
                        continue
                    # A deck rendered to PNG is only a picture of its first slide.
                    terminal = (dst == "png" and src in IMPRESS_IN) or src in PDF_IN
                    # Pages laid out from Word files look better than from web pages, so
                    # when Pandoc can produce either, steer it to DOCX.
                    cost = COST + 0.25 if src == "html" else COST
                    if terminal and dst == "png":
                        cost = COST + 1.5  # going via PDF can pick the slide and resolution
                    edges.append(Edge(src, dst, self, cost, terminal))
        return edges

    def convert(self, src: Path, dst: Path, src_fmt: str, dst_fmt: str, options: Options) -> None:
        if src_fmt in CALC:
            filters = CALC_FILTERS
        elif src_fmt in PDF_IN:
            filters = PDF_FILTERS
        elif src_fmt in WRITER_IN:
            filters = WRITER_FILTERS
        else:
            filters = {}
        target = filters.get(dst_fmt, dst.suffix.lstrip("."))
        with tempfile.TemporaryDirectory(prefix="rotary-lo-out-") as outdir:
            cmd = [
                self.soffice,
                f"-env:UserInstallation={self._profile.as_uri()}",
                "--headless",
                "--norestore",
                "--nolockcheck",
                "--convert-to",
                target,
                "--outdir",
                outdir,
            ]
            if src_fmt in IMPORT_FILTERS:
                cmd.append(f"--infilter={IMPORT_FILTERS[src_fmt]}")
            cmd.append(str(src))
            with self._lock:
                log = run(cmd, what="LibreOffice", timeout=600)
            produced = sorted(Path(outdir).iterdir())
            if not produced:
                raise ConversionError(
                    f"LibreOffice didn't produce a {dst_fmt.upper()} file. {log.strip()}".strip()
                )
            shutil.move(str(produced[0]), dst)
