"""Conversions that need FFmpeg, Pandoc, LibreOffice or pdfium. Each skips if its tool is missing."""

import pytest
from PIL import Image

from rotary.converters import ConversionError, Options

from .conftest import needs_ffmpeg, needs_pandoc, needs_soffice, probe

# ------------------------------------------------------------------ FFmpeg


@needs_ffmpeg
def test_video_to_audio(engine, clip):
    assert probe(engine.convert(clip, "mp3").output) == ["mp3"]


@needs_ffmpeg
def test_video_to_webm(engine, clip):
    codecs = probe(engine.convert(clip, "webm").output)
    assert codecs[0] in ("vp9", "vp8", "av1") and codecs[1] in ("opus", "vorbis")


@needs_ffmpeg
def test_video_to_gif_and_thumbnail(engine, clip):
    with Image.open(engine.convert(clip, "gif").output) as im:
        assert im.n_frames > 1
    with Image.open(engine.convert(clip, "png").output) as im:
        assert im.size == (160, 120)


@needs_ffmpeg
def test_animated_gif_to_mp4_with_odd_size(engine, tmp_path):
    frames = [Image.new("RGB", (41, 31), (i * 50, 0, 0)) for i in range(4)]
    src = tmp_path / "odd.gif"
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=100)
    assert probe(engine.convert(src, "mp4").output) == ["h264"]


@needs_ffmpeg
def test_audio_to_audio(engine, clip, tmp_path):
    wav = engine.convert(clip, "wav", out_dir=tmp_path / "a").output
    assert probe(engine.convert(wav, "flac").output) == ["flac"]


@needs_ffmpeg
def test_corrupt_media_reports_ffmpeg_error(engine, tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(ConversionError, match="FFmpeg failed"):
        engine.convert(bad, "mp3")


# ------------------------------------------------------------------ Pandoc

MARKDOWN = "# Title\n\nSome *emphasis* and a list:\n\n- one\n- two\n"


@needs_pandoc
def test_markdown_to_html_is_standalone(engine, tmp_path):
    src = tmp_path / "doc.md"
    src.write_text(MARKDOWN)
    html = engine.convert(src, "html").output.read_text()
    assert "<!DOCTYPE html>" in html and "<h1" in html and "<em>emphasis</em>" in html


@needs_pandoc
def test_markdown_docx_round_trip(engine, tmp_path):
    src = tmp_path / "doc.md"
    src.write_text(MARKDOWN)
    docx = engine.convert(src, "docx").output
    back = engine.convert(docx, "md", out_dir=tmp_path / "back").output.read_text()
    assert "# Title" in back and "*emphasis*" in back


# ------------------------------------------------------------------ pdfium / ReportLab


@pytest.fixture
def pdf(engine, tmp_path):
    pytest.importorskip("reportlab")
    src = tmp_path / "letter.txt"
    src.write_text("Dear reader,\n\nThis is page one.\n\fAnd this is page two.\n")
    out = tmp_path / "letter.pdf"
    converter = next(c for c in engine.available if c.name == "pdf")
    converter.convert(src, out, "txt", "pdf", Options())  # ReportLab directly, LibreOffice not needed
    return out


def test_pdf_text_extraction(engine, pdf):
    pytest.importorskip("pypdfium2")
    text = engine.convert(pdf, "txt", out_dir=pdf.parent / "out").output.read_text()
    assert "This is page one." in text and "And this is page two." in text


def test_pdf_page_render(engine, pdf, tmp_path):
    pytest.importorskip("pypdfium2")
    out = engine.convert(pdf, "png", options=Options(page=2, dpi=72)).output
    with Image.open(out) as im:
        assert im.size[0] in (595, 596) and im.size[1] == 842  # A4 (595.3 x 841.9 pt) at 72 dpi
    with pytest.raises(ConversionError, match="no page 3"):
        engine.convert(pdf, "jpg", options=Options(page=3))


# ------------------------------------------------------------------ LibreOffice


@pytest.mark.slow
@needs_soffice
def test_docx_to_pdf(engine, tmp_path):
    pytest.importorskip("pypdfium2")
    if not any(c.name == "documents" for c in engine.available):
        pytest.skip("needs Pandoc to make the test DOCX")
    src = tmp_path / "doc.md"
    src.write_text(MARKDOWN)
    result = engine.convert(src, "pdf")
    assert result.route.formats == ["markdown", "docx", "pdf"]
    text = engine.convert(result.output, "txt").output.read_text()
    assert "Title" in text and "emphasis" in text


@pytest.mark.slow
@needs_soffice
def test_spreadsheet_formats(engine, tmp_path):
    src = tmp_path / "t.csv"
    src.write_text("name,qty\ntea,2\n")
    xlsx = engine.convert(src, "xlsx").output
    ods = engine.convert(xlsx, "ods").output
    assert ods.read_bytes()[:2] == b"PK"
    back = engine.convert(ods, "csv", out_dir=tmp_path / "back").output.read_text()
    assert back.splitlines() == ["name,qty", "tea,2"]
