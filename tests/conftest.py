import shutil
import subprocess
from pathlib import Path

import pytest

from rotary.engine import Engine

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
HAVE_PANDOC = shutil.which("pandoc") is not None
HAVE_SOFFICE = shutil.which("soffice") is not None or shutil.which("libreoffice") is not None

needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="FFmpeg not installed")
needs_pandoc = pytest.mark.skipif(not HAVE_PANDOC, reason="Pandoc not installed")
needs_soffice = pytest.mark.skipif(not HAVE_SOFFICE, reason="LibreOffice not installed")


@pytest.fixture(scope="session")
def engine() -> Engine:
    return Engine()


@pytest.fixture
def rgba_png(tmp_path: Path) -> Path:
    from PIL import Image, ImageDraw

    im = Image.new("RGBA", (64, 48), (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle((8, 8, 40, 30), fill=(200, 30, 30, 255))
    path = tmp_path / "logo.png"
    im.save(path)
    return path


@pytest.fixture
def animated_gif(tmp_path: Path) -> Path:
    from PIL import Image

    frames = [Image.new("RGB", (40, 30), (i * 40, 80, 200 - i * 40)) for i in range(5)]
    path = tmp_path / "anim.gif"
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=80, loop=0)
    return path


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    if not HAVE_FFMPEG:
        pytest.skip("FFmpeg not installed")
    path = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
        ],
        check=True,
    )
    return path


def probe(path: Path) -> list[str]:
    """Codec names of the streams in a media file."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return [line.strip().rstrip(",") for line in out.splitlines() if line.strip()]
