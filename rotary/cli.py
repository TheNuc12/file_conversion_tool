"""Command-line interface: ``rotary photo.heic -t jpg``."""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

from . import __version__, formats
from .converters import ConversionError, Options
from .engine import Engine, RotaryError
from .formats import CATEGORIES, FORMATS

COMMANDS = {"convert", "formats", "route", "backends", "ui"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rotary",
        description="Rotary: convert files between image, audio, video, document, data and archive formats.",
        epilog="Shortcut: 'rotary FILE... -t FORMAT' is the same as 'rotary convert FILE... -t FORMAT'.",
    )
    parser.add_argument("--version", action="version", version=f"rotary {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    convert = sub.add_parser("convert", help="convert one or more files")
    convert.add_argument("inputs", nargs="+", metavar="FILE", help="files, folders or glob patterns")
    convert.add_argument("-t", "--to", metavar="FORMAT", help="target format, e.g. png, mp3, pdf, yaml")
    convert.add_argument("-o", "--output", metavar="PATH", help="output file (single input only); its extension can stand in for --to")
    convert.add_argument("-d", "--out-dir", metavar="DIR", help="write outputs here instead of next to each input")
    convert.add_argument("-f", "--from", dest="from_format", metavar="FORMAT", help="treat inputs as this format instead of guessing from the name")
    convert.add_argument("-r", "--recursive", action="store_true", help="include files in sub-folders of folder inputs")
    convert.add_argument("-q", "--quality", type=int, metavar="1-100", help="quality for lossy image formats")
    convert.add_argument("--page", type=int, metavar="N", help="page to render when turning a document into an image (default 1)")
    convert.add_argument("--dpi", type=int, metavar="N", help="resolution for document-to-image and image-to-PDF (default 150)")
    convert.add_argument("--overwrite", action="store_true", help="replace output files that already exist")
    convert.add_argument("-n", "--dry-run", action="store_true", help="show what would happen without converting")

    fmts = sub.add_parser("formats", help="list formats, or what one format can become")
    fmts.add_argument("format", nargs="?", help="show the targets reachable from this format (or file)")

    route = sub.add_parser("route", help="explain how one format gets converted to another")
    route.add_argument("source", help="source format or file")
    route.add_argument("target", help="target format")

    sub.add_parser("backends", help="show which conversion backends are installed")

    ui = sub.add_parser("ui", help="open the rotary-dial web interface")
    ui.add_argument("--host", default="127.0.0.1", help="interface to listen on (default 127.0.0.1)")
    ui.add_argument("--port", type=int, default=None, help="port (default 8765, or any free port if that's taken)")
    ui.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv.insert(0, "convert")
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    engine = Engine()
    try:
        if args.command == "convert":
            return _convert(engine, args, parser)
        if args.command == "formats":
            return _formats(engine, args.format)
        if args.command == "route":
            route = engine.route(_format_of(args.source).id, args.target)
            print(route.describe())
            return 0
        if args.command == "backends":
            return _backends(engine)
        if args.command == "ui":
            from .web.server import serve

            serve(engine, host=args.host, port=args.port, open_browser=not args.no_browser)
            return 0
    except (RotaryError, formats.UnknownFormatError) as exc:
        print(f"rotary: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


def _format_of(name: str) -> formats.Format:
    """Accept either a format name or a file path."""
    try:
        return formats.get(name)
    except formats.UnknownFormatError:
        return formats.detect(name)


def _expand(inputs: list[str], recursive: bool) -> list[tuple[Path, bool]]:
    """Resolve inputs to ``(file, named_directly)``; files found by scanning a folder or glob are not."""
    files: list[tuple[Path, bool]] = []
    for item in inputs:
        path = Path(item)
        if path.is_dir():
            pattern = "**/*" if recursive else "*"
            files.extend((p, False) for p in sorted(path.glob(pattern)) if p.is_file())
        elif not path.exists() and glob.has_magic(item):  # Windows shells don't expand wildcards
            matches = sorted(Path(p) for p in glob.glob(item, recursive=recursive))
            files.extend((p, False) for p in matches if p.is_file())
        else:
            files.append((path, True))
    return files


def _convert(engine: Engine, args, parser) -> int:
    if not args.to and not args.output:
        parser.error("say which format to convert to with -t/--to (or give -o/--output)")
    if args.quality is not None and not 1 <= args.quality <= 100:
        parser.error("--quality must be between 1 and 100")

    files = _expand(args.inputs, args.recursive)
    if args.output and len(files) != 1:
        parser.error("-o/--output works with a single input; use -d/--out-dir for several")
    target = formats.get(args.to) if args.to else formats.detect(args.output)
    options = Options(quality=args.quality, page=args.page, dpi=args.dpi)
    out_dir = Path(args.out_dir) if args.out_dir else None

    failures = skipped = done = 0
    for path, named in files:
        def fail(message: str) -> None:
            nonlocal failures
            print(f"  x {path}: {message}", file=sys.stderr)
            failures += 1

        if not path.is_file():
            fail("no such file")
            continue
        try:
            src = formats.get(args.from_format) if args.from_format else formats.detect(path)
            route = engine.route(src.id, target.id)
        except (RotaryError, formats.UnknownFormatError) as exc:
            # Folder scans pick up all sorts of files; only complain about ones asked for by name.
            if named:
                fail(str(exc))
            else:
                skipped += 1
            continue

        if args.dry_run:
            out = Path(args.output) if args.output else engine.output_path(path, src, target, out_dir)
            print(f"  - {path} -> {out}   {route.describe()}")
            done += 1
            continue
        try:
            result = engine.convert(
                path,
                target.id,
                output=args.output,
                out_dir=out_dir,
                from_format=args.from_format,
                options=options,
                overwrite=args.overwrite,
            )
        except (RotaryError, ConversionError) as exc:
            fail(str(exc))
            continue
        print(f"  ok {path} -> {_display(result.output)}   ({', '.join(result.route.backends)}, {result.seconds:.1f}s)")
        done += 1

    if not files:
        print("rotary: no input files found", file=sys.stderr)
        return 1
    if len(files) > 1 or skipped:
        summary = f"{done} {'to convert' if args.dry_run else 'converted'}"
        if failures:
            summary += f", {failures} failed"
        if skipped:
            summary += f", {skipped} skipped (can't become {target.extension.upper()})"
        print(summary)
    return 1 if failures else 0


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _formats(engine: Engine, name: str | None) -> int:
    if name:
        src = _format_of(name)
        targets = engine.targets(src.id)
        if not targets:
            print(f"Nothing installed can convert {src.label} files. Try 'rotary backends'.")
            return 1
        print(f"{src.label} ({src.extension}) can become:")
        _print_grouped(targets)
        return 0
    usable = set(engine.graph) | {e.dst for edges in engine.graph.values() for e in edges}
    print("Formats Rotary knows (* = needs a backend that isn't installed):")
    _print_grouped(list(FORMATS.values()), mark=lambda f: "" if f.id in usable else "*")
    return 0


def _print_grouped(items: list[formats.Format], mark=lambda f: "") -> None:
    for category in CATEGORIES:
        group = [f for f in items if f.category == category]
        if group:
            names = ", ".join(f.extension + mark(f) for f in group)
            print(f"  {category:<13} {names}")


def _backends(engine: Engine) -> int:
    for converter in engine.converters:
        ok = converter in engine.available
        count = len(list(converter.edges())) if ok else 0
        status = f"ready ({count} conversions)" if ok else f"missing - {converter.requirement}"
        print(f"  {'+' if ok else '-'} {converter.label:<18} {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
