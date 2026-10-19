"""Raster image conversion with Pillow."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

from .base import ConversionError, Converter, Edge, Options

# Rotary format id -> Pillow format name.
PIL_FORMATS = {
    "png": "PNG",
    "jpeg": "JPEG",
    "webp": "WEBP",
    "gif": "GIF",
    "bmp": "BMP",
    "tiff": "TIFF",
    "ico": "ICO",
    "icns": "ICNS",
    "avif": "AVIF",
    "jp2": "JPEG2000",
    "tga": "TGA",
    "ppm": "PPM",
    "pcx": "PCX",
    "qoi": "QOI",
    "heic": "HEIF",  # only with the optional pillow-heif plugin
}

# Formats that can't store transparency; images are flattened onto white.
NO_ALPHA = {"jpeg", "ppm", "pcx", "pdf"}
# Formats that can hold several frames (animation or pages).
MULTI_FRAME = {"gif", "webp", "png", "tiff", "avif", "pdf"}
ANIMATED = {"gif", "webp", "png", "avif"}
LOSSY = {"jpeg", "webp", "avif", "jp2", "heic"}


def _pil():
    from PIL import Image

    try:  # HEIC/HEIF support is optional.
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except ImportError:
        pass
    Image.init()
    return Image


def readable_formats() -> list[str]:
    try:
        Image = _pil()
    except ImportError:
        return []
    return [fmt for fmt, pil in PIL_FORMATS.items() if pil in Image.OPEN]


def writable_formats() -> list[str]:
    try:
        Image = _pil()
    except ImportError:
        return []
    return [fmt for fmt, pil in PIL_FORMATS.items() if pil in Image.SAVE]


def _has_alpha(img) -> bool:
    return img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info
    )


def _flatten(img):
    """Composite onto a white background, dropping transparency."""
    from PIL import Image

    rgba = img.convert("RGBA")
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def _prepare(img, fmt: str):
    if fmt in NO_ALPHA:
        if _has_alpha(img):
            return _flatten(img)
        if img.mode == "CMYK" and fmt == "jpeg":
            return img
        if img.mode not in ("RGB", "L"):
            return img.convert("RGB")
        return img
    if img.mode == "CMYK" and fmt != "tiff":
        return img.convert("RGB")
    if fmt in ("ico", "icns"):
        return img.convert("RGBA")
    if fmt in ("webp", "avif", "jp2", "heic", "qoi") and img.mode not in ("RGB", "RGBA", "L"):
        return img.convert("RGBA" if _has_alpha(img) else "RGB")
    return img


def _fallback_mode(img):
    return img.convert("RGBA" if _has_alpha(img) else "RGB")


def save_image(img, dst: Path, fmt: str, options: Options, frames: list | None = None, info=None) -> None:
    """Save a Pillow image (and optional extra frames) as ``fmt``. Shared with the PDF renderer."""
    pil_name = "PDF" if fmt == "pdf" else PIL_FORMATS[fmt]
    info = info or {}
    kwargs: dict = {}
    if fmt in LOSSY:
        kwargs["quality"] = options.quality or (90 if fmt == "jpeg" else 85)
    if fmt == "jpeg":
        kwargs["optimize"] = True
    if fmt == "png":
        kwargs["optimize"] = True
    if fmt == "pdf":
        kwargs["resolution"] = float(options.dpi or 150)
    first = _prepare(img, fmt)
    if frames:
        kwargs["save_all"] = True
        kwargs["append_images"] = [_prepare(f, fmt) for f in frames]
        if fmt in ANIMATED:
            if "duration" in info:
                kwargs["duration"] = info["duration"]
            kwargs["loop"] = info.get("loop", 0)
    try:
        first.save(dst, format=pil_name, **kwargs)
    except Exception:
        # Some mode/format combinations aren't supported; plain RGB(A) almost always is.
        first = _fallback_mode(first)
        if frames:
            kwargs["append_images"] = [_fallback_mode(f) for f in kwargs["append_images"]]
        try:
            first.save(dst, format=pil_name, **kwargs)
        except Exception as exc:
            raise ConversionError(f"Pillow couldn't write {fmt.upper()}: {exc}") from exc


class ImageConverter(Converter):
    name = "images"
    label = "Pillow"
    requirement = "pip install Pillow  (add pillow-heif for HEIC)"

    def is_available(self) -> bool:
        try:
            _pil()
        except ImportError:
            return False
        return True

    @cached_property
    def _edges(self) -> list[Edge]:
        readable = readable_formats()
        writable = writable_formats()
        edges = self._pairs(readable, writable)
        # Wrapping an image in a PDF is useful, but there's nothing in that PDF for a
        # later PDF step (like text extraction) to work with, so it must end the route.
        edges += self._pairs(readable, ["pdf"], terminal=True)
        return edges

    def edges(self) -> list[Edge]:
        return self._edges

    def convert(self, src: Path, dst: Path, src_fmt: str, dst_fmt: str, options: Options) -> None:
        Image = _pil()
        from PIL import ImageOps, ImageSequence

        try:
            with Image.open(src) as im:
                n_frames = getattr(im, "n_frames", 1)
                if n_frames > 1 and dst_fmt in MULTI_FRAME:
                    seq = [frame.copy() for frame in ImageSequence.Iterator(im)]
                    if dst_fmt != "gif":
                        # Palette frames trip up Pillow's APNG writer; full colour is safe everywhere.
                        seq = [frame.convert("RGBA") for frame in seq]
                    save_image(seq[0], dst, dst_fmt, options, frames=seq[1:], info=im.info)
                else:
                    img = ImageOps.exif_transpose(im)
                    img.load()
                    save_image(img, dst, dst_fmt, options)
        except ConversionError:
            raise
        except Exception as exc:
            raise ConversionError(f"Couldn't read {src.name} as {src_fmt.upper()}: {exc}") from exc
