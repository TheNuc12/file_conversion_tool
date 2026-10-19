from .archives import ArchiveConverter
from .base import ConversionError, Converter, Edge, Options
from .data import DataConverter
from .documents import DocumentConverter
from .images import ImageConverter
from .media import MediaConverter
from .office import OfficeConverter
from .pdf import PdfConverter


def default_converters() -> list[Converter]:
    """Every built-in backend. When two can do the same job at the same cost, the earlier one wins."""
    return [
        DataConverter(),
        ImageConverter(),
        ArchiveConverter(),
        PdfConverter(),
        MediaConverter(),
        DocumentConverter(),
        OfficeConverter(),
    ]


__all__ = [
    "ConversionError",
    "Converter",
    "Edge",
    "Options",
    "default_converters",
]
