"""Audio and video conversion with FFmpeg."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

from ..formats import AUDIO, FORMATS, VIDEO
from .base import Converter, Edge, Options, run, which

# Target -> (video encoders in order of preference, audio encoders in order of preference).
VIDEO_TARGETS: dict[str, tuple[list[str], list[str]]] = {
    "mp4": (["libx264", "libopenh264", "mpeg4"], ["aac", "libmp3lame"]),
    "mov": (["libx264", "mpeg4"], ["aac"]),
    "mkv": (["libx264", "libvpx-vp9", "mpeg4"], ["libopus", "libvorbis", "aac", "flac"]),
    "webm": (["libvpx-vp9", "libvpx", "libsvtav1", "libaom-av1"], ["libopus", "libvorbis"]),
    "avi": (["mpeg4", "libxvid", "mjpeg"], ["libmp3lame", "ac3", "pcm_s16le"]),
    "flv": (["libx264", "flv"], ["aac", "libmp3lame"]),
    "wmv": (["wmv2", "msmpeg4"], ["wmav2"]),
    "mpeg": (["mpeg2video", "mpeg1video"], ["mp2"]),
    "ogv": (["libtheora"], ["libvorbis", "libopus"]),
    "3gp": (["libx264", "h263"], ["aac", "libopencore_amrnb"]),
    "ts": (["libx264", "mpeg2video"], ["aac", "mp2"]),
}

AUDIO_TARGETS: dict[str, list[str]] = {
    "mp3": ["libmp3lame", "libshine"],
    "wav": ["pcm_s16le"],
    "flac": ["flac"],
    "ogg": ["libvorbis", "libopus", "flac"],
    "opus": ["libopus"],
    "aac": ["aac", "libfdk_aac"],
    "m4a": ["aac", "libfdk_aac", "alac"],
    "wma": ["wmav2", "wmav1"],
    "aiff": ["pcm_s16be"],
    "ac3": ["ac3"],
}

ENCODER_ARGS: dict[str, list[str]] = {
    "libx264": ["-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p"],
    "libopenh264": ["-pix_fmt", "yuv420p"],
    "mpeg4": ["-q:v", "4"],
    "libvpx-vp9": ["-crf", "33", "-b:v", "0", "-row-mt", "1", "-deadline", "good", "-cpu-used", "4"],
    "libvpx": ["-crf", "10", "-b:v", "2M"],
    "libsvtav1": ["-preset", "8", "-crf", "35"],
    "libaom-av1": ["-crf", "32", "-b:v", "0", "-cpu-used", "6"],
    "wmv2": ["-b:v", "2M"],
    "mpeg2video": ["-q:v", "4"],
    "libtheora": ["-q:v", "7"],
    "libmp3lame": ["-q:a", "2"],
    "libvorbis": ["-q:a", "5"],
    "libopus": ["-b:a", "128k"],
    "aac": ["-b:a", "192k"],
    "wmav2": ["-b:a", "192k"],
    "ac3": ["-b:a", "384k"],
}

# Most encoders need even frame dimensions (yuv420p); round down by a pixel when needed.
EVEN_SIZE = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
GIF_FILTER = (
    "fps=15,scale='min(640,iw)':-1:flags=lanczos,"
    "split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4"
)

VIDEO_SOURCES = [f.id for f in FORMATS.values() if f.category == VIDEO]
AUDIO_SOURCES = [f.id for f in FORMATS.values() if f.category == AUDIO]


class MediaConverter(Converter):
    name = "media"
    label = "FFmpeg"
    requirement = "install FFmpeg (https://ffmpeg.org/download.html)"

    @cached_property
    def ffmpeg(self) -> str | None:
        return which("ffmpeg")

    def is_available(self) -> bool:
        return self.ffmpeg is not None

    @cached_property
    def encoders(self) -> set[str]:
        out = run([self.ffmpeg, "-hide_banner", "-encoders"], what="ffmpeg", timeout=30)
        names = set()
        started = False
        for line in out.splitlines():
            if line.strip().startswith("------"):
                started = True
                continue
            parts = line.split()
            if started and len(parts) >= 2:
                names.add(parts[1])
        return names

    def _pick(self, candidates: list[str]) -> str | None:
        return next((c for c in candidates if c in self.encoders), None)

    @cached_property
    def _edges(self) -> list[Edge]:
        audio_targets = [t for t, enc in AUDIO_TARGETS.items() if self._pick(enc)]
        video_targets = [t for t, (venc, _) in VIDEO_TARGETS.items() if self._pick(venc)]
        edges = self._pairs(AUDIO_SOURCES + VIDEO_SOURCES, audio_targets)
        edges += self._pairs(VIDEO_SOURCES, video_targets)
        # An animated GIF the user hands us is a little video; a GIF made from a photo isn't.
        edges += self._pairs(["gif"], video_targets, initial=True)
        # Clips and still frames are fine results, but routing every image format through
        # them would offer nonsense like "MP4 to ICO".
        if "gif" in self.encoders:
            edges += self._pairs(VIDEO_SOURCES, ["gif"], terminal=True)
        thumbs = [t for t, enc in (("png", "png"), ("jpeg", "mjpeg")) if enc in self.encoders]
        edges += self._pairs(VIDEO_SOURCES, thumbs, terminal=True)
        return edges

    def edges(self) -> list[Edge]:
        return self._edges

    def convert(self, src: Path, dst: Path, src_fmt: str, dst_fmt: str, options: Options) -> None:
        cmd = [self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(src)]
        if dst_fmt in AUDIO_TARGETS:
            enc = self._pick(AUDIO_TARGETS[dst_fmt])
            cmd += ["-vn", "-sn", "-dn", "-c:a", enc, *ENCODER_ARGS.get(enc, [])]
        elif dst_fmt in VIDEO_TARGETS:
            venc_list, aenc_list = VIDEO_TARGETS[dst_fmt]
            venc, aenc = self._pick(venc_list), self._pick(aenc_list)
            cmd += ["-sn", "-dn", "-vf", EVEN_SIZE, "-c:v", venc, *ENCODER_ARGS.get(venc, [])]
            if aenc:
                cmd += ["-c:a", aenc, *ENCODER_ARGS.get(aenc, [])]
            else:
                cmd += ["-an"]
            if dst_fmt in ("mp4", "mov"):
                cmd += ["-movflags", "+faststart"]
        elif dst_fmt == "gif":
            cmd += ["-an", "-sn", "-dn", "-vf", GIF_FILTER, "-loop", "0"]
        elif dst_fmt in ("png", "jpeg"):
            cmd += ["-an", "-sn", "-dn", "-vf", "thumbnail", "-frames:v", "1", "-update", "1"]
            if dst_fmt == "jpeg":
                cmd += ["-q:v", "2"]
        cmd.append(str(dst))
        run(cmd, what="FFmpeg")
