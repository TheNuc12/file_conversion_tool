"""Route planning and execution.

Every converter contributes edges (``png -> webp``, ``docx -> pdf`` ...) to one
graph of formats. Converting a file means finding the cheapest path through
that graph and running each step in a scratch directory, so a format with no
direct converter can still be reached in two or three hops.
"""

from __future__ import annotations

import heapq
import os
import shutil
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from . import formats
from .converters import ConversionError, Converter, Edge, Options, default_converters
from .formats import FORMATS, Format


# Longer chains than this are almost always a sign of a silly route.
MAX_STEPS = 4


class RotaryError(Exception):
    """Something the user can fix: an unknown format, an impossible conversion, a clash."""


class NoRouteError(RotaryError):
    pass


@dataclass(frozen=True)
class Route:
    src: str
    dst: str
    steps: tuple[Edge, ...]

    @property
    def cost(self) -> float:
        return sum(step.cost for step in self.steps)

    @property
    def formats(self) -> list[str]:
        return [self.src] + [step.dst for step in self.steps]

    @property
    def backends(self) -> list[str]:
        seen: dict[str, None] = {}
        for step in self.steps:
            seen.setdefault(step.converter.label, None)
        return list(seen)

    def describe(self) -> str:
        chain = " -> ".join(FORMATS[f].extension.upper() for f in self.formats)
        return f"{chain}  (via {', '.join(self.backends)})"


@dataclass(frozen=True)
class Result:
    source: Path
    output: Path
    route: Route
    seconds: float


class Engine:
    def __init__(self, converters: list[Converter] | None = None):
        self.converters = default_converters() if converters is None else converters
        self._routes: dict[str, dict[str, Route]] = {}

    # ------------------------------------------------------------------ graph

    @cached_property
    def available(self) -> list[Converter]:
        found = []
        for converter in self.converters:
            try:
                if converter.is_available():
                    found.append(converter)
            except Exception:
                pass
        return found

    @cached_property
    def graph(self) -> dict[str, list[Edge]]:
        graph: dict[str, list[Edge]] = defaultdict(list)
        for converter in self.available:
            try:
                edges = list(converter.edges())
            except Exception:
                continue  # a backend that fails to probe shouldn't take the others down
            for edge in edges:
                if edge.src in FORMATS and edge.dst in FORMATS:
                    graph[edge.src].append(edge)
        return dict(graph)

    def routes_from(self, src: str) -> dict[str, Route]:
        """The cheapest route from ``src`` to every format it can reach."""
        if src in self._routes:
            return self._routes[src]

        best: dict[str, tuple[float, int]] = {src: (0.0, 0)}
        via: dict[str, Edge] = {}
        heap: list[tuple[float, int, int, str]] = [(0.0, 0, 0, src)]
        counter = 1
        while heap:
            cost, hops, _, node = heapq.heappop(heap)
            if (cost, hops) > best[node]:
                continue
            if hops + 1 > MAX_STEPS:
                continue
            for edge in self.graph.get(node, []):
                if edge.terminal or (edge.initial and node != src):
                    continue
                candidate = (cost + edge.cost, hops + 1)
                if edge.dst not in best or candidate < best[edge.dst]:
                    best[edge.dst] = candidate
                    via[edge.dst] = edge
                    heapq.heappush(heap, (*candidate, counter, edge.dst))
                    counter += 1

        def path_to(node: str) -> list[Edge]:
            steps = []
            while node != src:
                steps.append(via[node])
                node = via[node].src
            return steps[::-1]

        routes = {node: Route(src, node, tuple(path_to(node))) for node in best if node != src}
        # Terminal edges may only finish a route, so try them last from every reached node.
        for node, (cost, hops) in best.items():
            if hops + 1 > MAX_STEPS:
                continue
            for edge in self.graph.get(node, []):
                if not edge.terminal or edge.dst == src or (edge.initial and node != src):
                    continue
                current = routes.get(edge.dst)
                if current is None or (cost + edge.cost, hops + 1) < (current.cost, len(current.steps)):
                    routes[edge.dst] = Route(src, edge.dst, tuple(path_to(node)) + (edge,))

        self._routes[src] = routes
        return routes

    def targets(self, src: str) -> list[Format]:
        """Reachable formats, grouped by category with the most common formats first."""
        category = {c: i for i, c in enumerate(formats.CATEGORIES)}
        popularity = {f: i for i, f in enumerate(FORMATS)}
        found = [FORMATS[f] for f in self.routes_from(src)]
        return sorted(found, key=lambda f: (category[f.category], popularity[f.id]))

    def sources(self) -> list[Format]:
        return [f for f in FORMATS.values() if f.id in self.graph]

    def route(self, src: str, dst: str) -> Route:
        src_fmt, dst_fmt = formats.get(src), formats.get(dst)
        if src_fmt.id == dst_fmt.id:
            raise RotaryError(f"That's already {src_fmt.label}")
        found = self.routes_from(src_fmt.id).get(dst_fmt.id)
        if found is None:
            raise NoRouteError(self._no_route_message(src_fmt, dst_fmt))
        return found

    def _no_route_message(self, src: Format, dst: Format) -> str:
        msg = f"No way to convert {src.label} to {dst.label}"
        missing = [c for c in self.converters if c not in self.available and c.requirement]
        if missing:
            hints = "; ".join(f"{c.label}: {c.requirement}" for c in missing)
            msg += f". Installing more backends may help ({hints})"
        return msg

    # ------------------------------------------------------------------ converting

    def output_path(self, source: Path, src: Format, dst: Format, out_dir: Path | None = None) -> Path:
        stem = formats.strip_extension(source, src) or "converted"
        return (out_dir or source.parent) / f"{stem}.{dst.extension}"

    def convert(
        self,
        source: str | os.PathLike,
        to: str | None = None,
        *,
        output: str | os.PathLike | None = None,
        out_dir: str | os.PathLike | None = None,
        from_format: str | None = None,
        options: Options | None = None,
        overwrite: bool = False,
    ) -> Result:
        started = time.monotonic()
        source = Path(source).resolve()
        if not source.is_file():
            raise RotaryError(f"{source} is not a file")
        options = options or Options()

        src = formats.get(from_format) if from_format else formats.detect(source)
        if to:
            dst = formats.get(to)
        elif output:
            dst = formats.detect(output)
        else:
            raise RotaryError("Say which format to convert to")
        route = self.route(src.id, dst.id)

        if output:
            target = Path(output).resolve()
        else:
            target = self.output_path(source, src, dst, Path(out_dir).resolve() if out_dir else None)
        if target == source:
            raise RotaryError(f"Refusing to overwrite the input file {source}")
        if target.exists() and not overwrite:
            raise RotaryError(f"{target} already exists (use --overwrite to replace it)")

        stem = formats.strip_extension(source, src) or "converted"
        with tempfile.TemporaryDirectory(prefix="rotary-") as tmp:
            current = self._with_proper_name(source, src, stem, Path(tmp))
            for i, step in enumerate(route.steps, start=1):
                step_dir = Path(tmp) / f"step{i}"
                step_dir.mkdir()
                out = step_dir / f"{stem}.{FORMATS[step.dst].extension}"
                try:
                    step.converter.convert(current, out, step.src, step.dst, options)
                except ConversionError as exc:
                    if len(route.steps) > 1:
                        raise ConversionError(
                            f"Step {i} of {len(route.steps)} ({step.src} -> {step.dst}) failed: {exc}"
                        ) from exc
                    raise
                if not out.exists():
                    raise ConversionError(f"{step.converter.label} didn't produce a {step.dst} file")
                current = out
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(current), str(target))
        return Result(source, target, route, time.monotonic() - started)

    @staticmethod
    def _with_proper_name(source: Path, fmt: Format, stem: str, tmp: Path) -> Path:
        """Backends like LibreOffice go by extension, so give mislabelled input a correct one."""
        try:
            if formats.detect(source).id == fmt.id:
                return source
        except formats.UnknownFormatError:
            pass
        alias = tmp / f"{stem}.{fmt.extension}"
        try:
            alias.symlink_to(source)
        except OSError:
            shutil.copyfile(source, alias)
        return alias
