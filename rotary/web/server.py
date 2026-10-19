"""A small local web server for the rotary-dial interface.

Uses only the standard library. Files are uploaded as the raw request body
(no multipart), converted into a scratch directory and streamed back.
"""

from __future__ import annotations

import errno
import json
import re
import shutil
import sys
import tempfile
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .. import __version__, formats
from ..converters import ConversionError, Options
from ..engine import Engine, RotaryError
from ..formats import CATEGORIES, FORMATS

DEFAULT_PORT = 8765
MAX_UPLOAD = 4 * 1024**3  # 4 GiB
CHUNK = 1024 * 1024
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", name).strip(" .")
    return name[:200] or "upload"


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = f"Rotary/{__version__}"
    engine: Engine
    allow_any_host = False

    # ------------------------------------------------------------------ plumbing

    def log_message(self, fmt, *args):  # keep the terminal quiet apart from errors
        pass

    def _host_ok(self) -> bool:
        # Refuse requests addressed to some other host name, which is what a DNS
        # rebinding attack against a localhost server looks like.
        if self.allow_any_host:
            return True
        host = self.headers.get("Host") or ""
        name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
        return name in LOOPBACK

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"error": message}, status)

    # ------------------------------------------------------------------ routes

    def do_GET(self):
        if not self._host_ok():
            return self._error("Unexpected host", HTTPStatus.FORBIDDEN)
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if url.path == "/api/formats":
            return self._formats()
        if url.path == "/api/targets":
            return self._targets(parse_qs(url.query))
        return self._error("Not found", HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if not self._host_ok():
            return self._error("Unexpected host", HTTPStatus.FORBIDDEN)
        # Browsers won't send a custom header cross-origin without a CORS preflight
        # (which we never approve), so other websites can't drive this endpoint.
        if self.headers.get("X-Rotary") != "1":
            return self._error("Missing X-Rotary header", HTTPStatus.FORBIDDEN)
        url = urlparse(self.path)
        if url.path == "/api/convert":
            return self._convert(parse_qs(url.query))
        return self._error("Not found", HTTPStatus.NOT_FOUND)

    def _static(self, name: str, content_type: str) -> None:
        body = resources.files("rotary.web").joinpath("static", name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' blob: data:",
        )
        self.end_headers()
        self.wfile.write(body)

    def _formats(self) -> None:
        sources = {f.id for f in self.engine.sources()}
        self._json(
            {
                "version": __version__,
                "categories": list(CATEGORIES),
                "formats": {
                    f.id: {
                        "label": f.label,
                        "category": f.category,
                        "extension": f.extension,
                        "extensions": list(f.extensions),
                        "readable": f.id in sources,
                    }
                    for f in FORMATS.values()
                },
                "backends": [
                    {"label": c.label, "available": c in self.engine.available, "requirement": c.requirement}
                    for c in self.engine.converters
                ],
            }
        )

    def _targets(self, query: dict) -> None:
        try:
            if "from" in query:
                src = formats.get(query["from"][0])
            elif "file" in query:
                src = formats.detect(query["file"][0])
            else:
                return self._error("Pass ?file=<name> or ?from=<format>")
        except formats.UnknownFormatError as exc:
            return self._error(str(exc), HTTPStatus.UNPROCESSABLE_ENTITY)
        routes = self.engine.routes_from(src.id)
        targets = [
            {
                "id": f.id,
                "steps": [
                    {"from": s.src, "to": s.dst, "via": s.converter.label} for s in routes[f.id].steps
                ],
            }
            for f in self.engine.targets(src.id)
        ]
        self._json({"source": src.id, "targets": targets})

    def _convert(self, query: dict) -> None:
        name = _safe_name(query.get("name", ["upload"])[0])
        to = query.get("to", [None])[0]
        if not to:
            return self._error("Missing ?to=<format>")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._error("Content-Length required", HTTPStatus.LENGTH_REQUIRED)
        if length > MAX_UPLOAD:
            return self._error("File too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        options = Options(
            quality=_int(query.get("quality", [None])[0]),
            page=_int(query.get("page", [None])[0]),
            dpi=_int(query.get("dpi", [None])[0]),
        )
        from_format = query.get("from", [None])[0] or None

        workdir = Path(tempfile.mkdtemp(prefix="rotary-web-"))
        try:
            upload = workdir / "in" / name
            upload.parent.mkdir()
            remaining = length
            with open(upload, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK, remaining))
                    if not chunk:
                        return self._error("Upload interrupted")
                    f.write(chunk)
                    remaining -= len(chunk)
            try:
                result = self.engine.convert(
                    upload, to, out_dir=workdir / "out", from_format=from_format, options=options
                )
            except (RotaryError, formats.UnknownFormatError) as exc:
                return self._error(str(exc), HTTPStatus.UNPROCESSABLE_ENTITY)
            except ConversionError as exc:
                return self._error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)

            size = result.output.stat().st_size
            filename = result.output.name
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
            self.send_header("X-Rotary-Filename", quote(filename))
            self.send_header("X-Rotary-Route", " > ".join(result.route.formats))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(result.output, "rb") as f:
                shutil.copyfileobj(f, self.wfile, CHUNK)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser went away
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def make_server(engine: Engine, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    handler = type(
        "RotaryHandler",
        (Handler,),
        {"engine": engine, "allow_any_host": host not in LOOPBACK},
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(engine: Engine, host: str = "127.0.0.1", port: int | None = None, open_browser: bool = True) -> None:
    try:
        server = make_server(engine, host, DEFAULT_PORT if port is None else port)
    except OSError as exc:
        if port is not None or exc.errno != errno.EADDRINUSE:
            raise
        server = make_server(engine, host, 0)  # default port taken: let the OS pick one

    # Warm up backend probing so the first request isn't slow.
    threading.Thread(target=lambda: engine.graph, daemon=True).start()

    shown_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    if ":" in shown_host and not shown_host.startswith("["):
        shown_host = f"[{shown_host}]"
    url = f"http://{shown_host}:{server.server_address[1]}/"
    print(f"Rotary is running at {url}  (Ctrl+C to stop)")
    if host not in LOOPBACK:
        print("Warning: listening beyond this computer; anyone on your network can use it.", file=sys.stderr)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
