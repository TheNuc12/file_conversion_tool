"""Repack archives between ZIP and the tar family.

Members are streamed from one archive straight into the other, so nothing is
ever extracted to disk and hostile member names can't escape anywhere.
"""

from __future__ import annotations

import shutil
import stat
import tarfile
import time
import zipfile
from pathlib import Path

from .base import ConversionError, Converter, Edge, Options

TAR_MODES = {"tar": "", "tar.gz": "gz", "tar.bz2": "bz2", "tar.xz": "xz"}
ARCHIVES = ["zip", *TAR_MODES]
ZIP_EPOCH = 315532800  # 1980-01-01, the earliest timestamp ZIP can store


def _zip_time(ts: float) -> tuple:
    return time.localtime(max(ts, ZIP_EPOCH))[:6]


class ArchiveConverter(Converter):
    name = "archives"
    label = "built-in archives"

    def edges(self) -> list[Edge]:
        return self._pairs(ARCHIVES, ARCHIVES)

    def convert(self, src: Path, dst: Path, src_fmt: str, dst_fmt: str, options: Options) -> None:
        try:
            if src_fmt == "zip":
                self._zip_to_tar(src, dst, TAR_MODES[dst_fmt])
            elif dst_fmt == "zip":
                self._tar_to_zip(src, dst)
            else:
                self._tar_to_tar(src, dst, TAR_MODES[dst_fmt])
        except (zipfile.BadZipFile, tarfile.TarError, RuntimeError, EOFError, OSError) as exc:
            raise ConversionError(f"Couldn't repack {src.name}: {exc}") from exc

    def _zip_to_tar(self, src: Path, dst: Path, compression: str) -> None:
        with zipfile.ZipFile(src) as zf, tarfile.open(dst, f"w:{compression}") as tf:
            for info in zf.infolist():
                member = tarfile.TarInfo(info.filename.rstrip("/"))
                member.mtime = time.mktime(info.date_time + (0, 0, -1))
                perms = (info.external_attr >> 16) & 0o7777
                if info.is_dir():
                    member.type = tarfile.DIRTYPE
                    member.mode = perms or 0o755
                    tf.addfile(member)
                else:
                    member.size = info.file_size
                    member.mode = perms or 0o644
                    with zf.open(info) as fh:
                        tf.addfile(member, fh)

    def _tar_to_zip(self, src: Path, dst: Path) -> None:
        with tarfile.open(src, "r:*") as tf, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
            for member in tf:
                if member.isdir():
                    info = zipfile.ZipInfo(member.name.rstrip("/") + "/", _zip_time(member.mtime))
                    info.external_attr = ((stat.S_IFDIR | (member.mode & 0o7777)) << 16) | 0x10
                    zf.writestr(info, b"")
                elif member.isfile():
                    info = zipfile.ZipInfo(member.name, _zip_time(member.mtime))
                    info.external_attr = (stat.S_IFREG | (member.mode & 0o7777)) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.file_size = member.size
                    with tf.extractfile(member) as fh, zf.open(info, "w", force_zip64=member.size > 2**31) as out:
                        shutil.copyfileobj(fh, out)
                # ZIP has no portable way to store links or device files; they're skipped.

    def _tar_to_tar(self, src: Path, dst: Path, compression: str) -> None:
        with tarfile.open(src, "r:*") as tin, tarfile.open(dst, f"w:{compression}") as tout:
            for member in tin:
                tout.addfile(member, tin.extractfile(member) if member.isfile() else None)
