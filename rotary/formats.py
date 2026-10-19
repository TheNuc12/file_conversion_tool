"""The catalogue of file formats Rotary knows about.

A format is identified by a short canonical id (``"jpeg"``, ``"markdown"``,
``"tar.gz"``) and carries the file extensions that map to it. The first
extension listed is the one used when Rotary names an output file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

IMAGE = "image"
AUDIO = "audio"
VIDEO = "video"
DOCUMENT = "document"
SPREADSHEET = "spreadsheet"
PRESENTATION = "presentation"
DATA = "data"
ARCHIVE = "archive"

CATEGORIES = (IMAGE, AUDIO, VIDEO, DOCUMENT, SPREADSHEET, PRESENTATION, DATA, ARCHIVE)


@dataclass(frozen=True)
class Format:
    id: str
    label: str
    category: str
    extensions: tuple[str, ...]

    @property
    def extension(self) -> str:
        return self.extensions[0]


_FORMATS = [
    # Images
    Format("png", "PNG image", IMAGE, ("png",)),
    Format("jpeg", "JPEG image", IMAGE, ("jpg", "jpeg", "jpe", "jfif")),
    Format("webp", "WebP image", IMAGE, ("webp",)),
    Format("gif", "GIF image", IMAGE, ("gif",)),
    Format("bmp", "Bitmap image", IMAGE, ("bmp", "dib")),
    Format("tiff", "TIFF image", IMAGE, ("tiff", "tif")),
    Format("ico", "Windows icon", IMAGE, ("ico",)),
    Format("icns", "macOS icon", IMAGE, ("icns",)),
    Format("avif", "AVIF image", IMAGE, ("avif",)),
    Format("jp2", "JPEG 2000 image", IMAGE, ("jp2", "j2k", "jpf")),
    Format("tga", "Targa image", IMAGE, ("tga",)),
    Format("ppm", "Netpbm image", IMAGE, ("ppm", "pgm", "pbm", "pnm")),
    Format("pcx", "PCX image", IMAGE, ("pcx",)),
    Format("qoi", "QOI image", IMAGE, ("qoi",)),
    Format("heic", "HEIC image", IMAGE, ("heic", "heif")),
    Format("svg", "SVG vector image", IMAGE, ("svg",)),
    # Audio
    Format("mp3", "MP3 audio", AUDIO, ("mp3",)),
    Format("wav", "WAV audio", AUDIO, ("wav",)),
    Format("flac", "FLAC audio", AUDIO, ("flac",)),
    Format("ogg", "Ogg Vorbis audio", AUDIO, ("ogg", "oga")),
    Format("opus", "Opus audio", AUDIO, ("opus",)),
    Format("aac", "AAC audio", AUDIO, ("aac",)),
    Format("m4a", "M4A audio", AUDIO, ("m4a",)),
    Format("wma", "Windows Media audio", AUDIO, ("wma",)),
    Format("aiff", "AIFF audio", AUDIO, ("aiff", "aif")),
    Format("ac3", "AC-3 audio", AUDIO, ("ac3",)),
    # Video
    Format("mp4", "MP4 video", VIDEO, ("mp4", "m4v")),
    Format("mkv", "Matroska video", VIDEO, ("mkv",)),
    Format("webm", "WebM video", VIDEO, ("webm",)),
    Format("mov", "QuickTime video", VIDEO, ("mov", "qt")),
    Format("avi", "AVI video", VIDEO, ("avi",)),
    Format("flv", "Flash video", VIDEO, ("flv",)),
    Format("wmv", "Windows Media video", VIDEO, ("wmv",)),
    Format("mpeg", "MPEG video", VIDEO, ("mpeg", "mpg")),
    Format("ogv", "Ogg video", VIDEO, ("ogv",)),
    Format("3gp", "3GP video", VIDEO, ("3gp",)),
    Format("ts", "MPEG transport stream", VIDEO, ("ts", "mts", "m2ts")),
    # Documents
    Format("pdf", "PDF document", DOCUMENT, ("pdf",)),
    Format("docx", "Word document", DOCUMENT, ("docx",)),
    Format("doc", "Word 97-2003 document", DOCUMENT, ("doc",)),
    Format("odt", "OpenDocument text", DOCUMENT, ("odt",)),
    Format("rtf", "Rich Text Format", DOCUMENT, ("rtf",)),
    Format("txt", "Plain text", DOCUMENT, ("txt", "text")),
    Format("markdown", "Markdown", DOCUMENT, ("md", "markdown", "mkd")),
    Format("html", "HTML page", DOCUMENT, ("html", "htm", "xhtml")),
    Format("rst", "reStructuredText", DOCUMENT, ("rst",)),
    Format("latex", "LaTeX", DOCUMENT, ("tex", "latex")),
    Format("epub", "EPUB e-book", DOCUMENT, ("epub",)),
    Format("org", "Org mode", DOCUMENT, ("org",)),
    Format("asciidoc", "AsciiDoc", DOCUMENT, ("adoc", "asciidoc")),
    Format("mediawiki", "MediaWiki markup", DOCUMENT, ("wiki", "mediawiki")),
    Format("ipynb", "Jupyter notebook", DOCUMENT, ("ipynb",)),
    # Spreadsheets
    Format("xlsx", "Excel workbook", SPREADSHEET, ("xlsx",)),
    Format("xls", "Excel 97-2003 workbook", SPREADSHEET, ("xls",)),
    Format("ods", "OpenDocument spreadsheet", SPREADSHEET, ("ods",)),
    # Presentations
    Format("pptx", "PowerPoint presentation", PRESENTATION, ("pptx",)),
    Format("ppt", "PowerPoint 97-2003 presentation", PRESENTATION, ("ppt",)),
    Format("odp", "OpenDocument presentation", PRESENTATION, ("odp",)),
    # Structured data
    Format("json", "JSON", DATA, ("json",)),
    Format("yaml", "YAML", DATA, ("yaml", "yml")),
    Format("toml", "TOML", DATA, ("toml",)),
    Format("xml", "XML", DATA, ("xml",)),
    Format("csv", "CSV table", DATA, ("csv",)),
    Format("tsv", "TSV table", DATA, ("tsv", "tab")),
    # Archives
    Format("zip", "ZIP archive", ARCHIVE, ("zip",)),
    Format("tar", "Tar archive", ARCHIVE, ("tar",)),
    Format("tar.gz", "Gzipped tar archive", ARCHIVE, ("tar.gz", "tgz")),
    Format("tar.bz2", "Bzip2 tar archive", ARCHIVE, ("tar.bz2", "tbz2", "tbz")),
    Format("tar.xz", "XZ tar archive", ARCHIVE, ("tar.xz", "txz")),
]

FORMATS: dict[str, Format] = {f.id: f for f in _FORMATS}

_BY_EXTENSION: dict[str, Format] = {}
for _fmt in _FORMATS:
    for _ext in _fmt.extensions:
        _BY_EXTENSION[_ext] = _fmt

# Names people commonly type that are neither an id nor an extension.
_ALIASES = {
    "jpg": "jpeg",
    "md": "markdown",
    "yml": "yaml",
    "htm": "html",
    "tex": "latex",
    "text": "txt",
    "tgz": "tar.gz",
    "excel": "xlsx",
    "word": "docx",
    "powerpoint": "pptx",
}


class UnknownFormatError(ValueError):
    pass


def get(name: str) -> Format:
    """Look up a format by id, extension or alias (case-insensitive, leading dot allowed)."""
    key = name.strip().lower().lstrip(".")
    if key in FORMATS:
        return FORMATS[key]
    if key in _ALIASES:
        return FORMATS[_ALIASES[key]]
    if key in _BY_EXTENSION:
        return _BY_EXTENSION[key]
    raise UnknownFormatError(f"Unknown format: {name!r}")


def detect(path: str | Path) -> Format:
    """Work out a file's format from its name, honouring double extensions like ``.tar.gz``."""
    name = Path(path).name.lower()
    parts = name.split(".")
    # Try the longest suffix first so "x.tar.gz" wins over "x.gz".
    for i in range(1, len(parts)):
        suffix = ".".join(parts[i:])
        if suffix in _BY_EXTENSION:
            return _BY_EXTENSION[suffix]
    raise UnknownFormatError(f"Can't tell what kind of file {Path(path).name!r} is from its name")


def strip_extension(path: str | Path, fmt: Format) -> str:
    """Return the file name without the extension that identified ``fmt``."""
    name = Path(path).name
    lower = name.lower()
    for ext in sorted(fmt.extensions, key=len, reverse=True):
        if lower.endswith("." + ext):
            return name[: -(len(ext) + 1)]
    return Path(path).stem
