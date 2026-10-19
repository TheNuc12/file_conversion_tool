from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


class ConversionError(RuntimeError):
    """A backend failed to convert a file."""


@dataclass
class Options:
    """Knobs that some converters understand. Converters ignore what doesn't apply to them."""

    quality: int | None = None  # 1-100, for lossy image formats
    page: int | None = None  # 1-based page/slide to render when turning a document into an image
    dpi: int | None = None  # resolution when rendering documents to images (and images to PDF)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Edge:
    """One direct conversion a converter can perform."""

    src: str
    dst: str
    converter: Converter
    cost: float = 1.0
    # A terminal edge may only be the last step of a route. Use it for conversions whose
    # output is a dead end for further processing, e.g. a PNG wrapped in a PDF has no text
    # for a PDF-to-text step to extract.
    terminal: bool = False
    # An initial edge may only be the first step, for conversions that only make sense on
    # the user's original file, e.g. treating a GIF as a video (a PNG turned into a GIF
    # is not a movie).
    initial: bool = False


class Converter:
    name: str = "converter"
    #: Human-friendly backend name, e.g. "FFmpeg".
    label: str = "converter"
    #: Shown by ``rotary backends`` when the converter is unavailable.
    requirement: str = ""

    def is_available(self) -> bool:
        return True

    def edges(self) -> Iterable[Edge]:
        raise NotImplementedError

    def convert(self, src: Path, dst: Path, src_fmt: str, dst_fmt: str, options: Options) -> None:
        raise NotImplementedError

    def _pairs(
        self,
        sources: Iterable[str],
        targets: Iterable[str],
        cost: float = 1.0,
        terminal: bool = False,
        initial: bool = False,
    ) -> list[Edge]:
        targets = list(targets)
        return [
            Edge(s, t, self, cost, terminal, initial) for s in sources for t in targets if s != t
        ]


# The desktop app has no console, and without this flag Windows would flash one up
# for every FFmpeg or Pandoc call.
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def run(cmd: list[str], *, what: str, timeout: float | None = None, cwd: Path | None = None) -> str:
    """Run an external tool, turning failures into a readable ConversionError."""
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            **NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise ConversionError(f"{what} is not installed ({cmd[0]} not found)") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConversionError(f"{what} timed out after {timeout:.0f}s") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        tail = "\n".join(detail[-8:]) if detail else f"exit code {proc.returncode}"
        raise ConversionError(f"{what} failed:\n{tail}")
    return proc.stdout


def which(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None
