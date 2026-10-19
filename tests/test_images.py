import pytest
from PIL import Image

from rotary.converters import Options


def test_transparent_png_to_jpeg_is_flattened_on_white(engine, rgba_png):
    out = engine.convert(rgba_png, "jpg").output
    with Image.open(out) as im:
        assert im.format == "JPEG" and im.mode == "RGB"
        assert im.getpixel((60, 44)) == pytest.approx((255, 255, 255), abs=4)
        r, g, b = im.getpixel((20, 20))
        assert r > 150 and g < 80


@pytest.mark.parametrize("target, pil_format", [("webp", "WEBP"), ("bmp", "BMP"), ("tiff", "TIFF"), ("ico", "ICO"), ("gif", "GIF")])
def test_common_targets(engine, rgba_png, target, pil_format):
    out = engine.convert(rgba_png, target).output
    with Image.open(out) as im:
        assert im.format == pil_format


def test_animation_survives_gif_to_webp(engine, animated_gif):
    out = engine.convert(animated_gif, "webp").output
    with Image.open(out) as im:
        assert im.n_frames == 5


def test_animated_gif_to_png_keeps_frames_but_jpeg_takes_the_first(engine, animated_gif, tmp_path):
    with Image.open(engine.convert(animated_gif, "png").output) as im:
        assert getattr(im, "n_frames", 1) == 5
    with Image.open(engine.convert(animated_gif, "jpg").output) as im:
        assert im.size == (40, 30)


def test_quality_changes_jpeg_size(engine, tmp_path):
    from PIL import ImageDraw

    im = Image.new("RGB", (200, 200))
    draw = ImageDraw.Draw(im)
    for i in range(0, 200, 4):
        draw.line((0, i, 200, 200 - i), fill=(i, 255 - i, (i * 7) % 255), width=2)
    src = tmp_path / "noise.png"
    im.save(src)
    low = engine.convert(src, "jpg", out_dir=tmp_path / "low", options=Options(quality=10)).output
    high = engine.convert(src, "jpg", out_dir=tmp_path / "high", options=Options(quality=95)).output
    assert low.stat().st_size < high.stat().st_size


def test_image_to_pdf(engine, rgba_png):
    out = engine.convert(rgba_png, "pdf").output
    assert out.read_bytes().startswith(b"%PDF")


def test_not_really_an_image(engine, tmp_path):
    from rotary.converters import ConversionError

    fake = tmp_path / "fake.png"
    fake.write_text("hello")
    with pytest.raises(ConversionError, match="Couldn't read fake.png"):
        engine.convert(fake, "jpg")
