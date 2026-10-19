import pytest

from rotary import formats


@pytest.mark.parametrize(
    "name, expected",
    [("jpg", "jpeg"), (".JPG", "jpeg"), ("jpeg", "jpeg"), ("md", "markdown"), ("yml", "yaml"),
     ("tgz", "tar.gz"), ("tar.gz", "tar.gz"), ("htm", "html"), ("word", "docx")],
)
def test_lookup_by_id_extension_or_alias(name, expected):
    assert formats.get(name).id == expected


@pytest.mark.parametrize(
    "filename, expected",
    [("photo.JPG", "jpeg"), ("backup.tar.gz", "tar.gz"), ("v1.2.release.tar.xz", "tar.xz"),
     ("my.notes.md", "markdown"), ("clip.m4v", "mp4"), ("archive.tgz", "tar.gz")],
)
def test_detect_from_filename(filename, expected):
    assert formats.detect(filename).id == expected


def test_unknown_names():
    with pytest.raises(formats.UnknownFormatError):
        formats.get("definitely-not-a-format")
    with pytest.raises(formats.UnknownFormatError):
        formats.detect("README")


def test_strip_extension_keeps_inner_dots():
    assert formats.strip_extension("v1.2.tar.gz", formats.get("tar.gz")) == "v1.2"
    assert formats.strip_extension("Photo.JPEG", formats.get("jpeg")) == "Photo"


def test_every_format_has_unique_extensions():
    seen = {}
    for fmt in formats.FORMATS.values():
        for ext in fmt.extensions:
            assert ext not in seen, f"{ext} claimed by {seen.get(ext)} and {fmt.id}"
            seen[ext] = fmt.id
