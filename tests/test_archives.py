import io
import tarfile
import zipfile


def make_zip(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("docs/", b"")
        zf.writestr("docs/readme.txt", b"hello")
        zf.writestr("data.bin", bytes(range(256)) * 10)
    return path


def test_zip_to_tar_gz_and_back(engine, tmp_path):
    src = make_zip(tmp_path / "bundle.zip")
    tgz = engine.convert(src, "tar.gz").output
    assert tgz.name == "bundle.tar.gz"
    with tarfile.open(tgz) as tf:
        assert tf.getmember("docs").isdir()
        assert tf.extractfile("docs/readme.txt").read() == b"hello"

    back = engine.convert(tgz, "zip", out_dir=tmp_path / "back").output
    with zipfile.ZipFile(back) as zf:
        assert set(zf.namelist()) == {"docs/", "docs/readme.txt", "data.bin"}
        assert zf.read("data.bin") == bytes(range(256)) * 10


def test_tar_recompression_keeps_links(engine, tmp_path):
    src = tmp_path / "a.tar"
    with tarfile.open(src, "w") as tf:
        data = b"content"
        info = tarfile.TarInfo("file.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("link.txt")
        link.type = tarfile.SYMTYPE
        link.linkname = "file.txt"
        tf.addfile(link)
    out = engine.convert(src, "tar.xz").output
    with tarfile.open(out) as tf:
        assert tf.getmember("link.txt").issym()
    # ZIP can't hold the link, so it's left out rather than failing.
    with zipfile.ZipFile(engine.convert(src, "zip").output) as zf:
        assert zf.namelist() == ["file.txt"]
