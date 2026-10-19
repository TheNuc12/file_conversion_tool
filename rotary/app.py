"""The desktop launcher: what the Rotary icon runs.

Starts the web interface in the background, opens it in its own browser window
and quits when that window is closed. Clicking the icon while Rotary is
already running just opens another window onto the same instance.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

from . import __version__


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    path = Path(base) / "Rotary" if base else Path.home() / ".rotary"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _attach_log() -> None:
    # A windowed build has no console, so sys.stdout/stderr are None and a bare print()
    # would crash. Send output to a log file instead.
    if sys.stdout is None or sys.stderr is None:
        log = open(data_dir() / "rotary.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log


def _use_bundled_tools() -> None:
    """Put FFmpeg/Pandoc shipped inside the packaged app ahead of anything on PATH."""
    if not getattr(sys, "frozen", False):
        return
    roots = [Path(getattr(sys, "_MEIPASS", "")) / "tools", Path(sys.executable).parent / "tools"]
    found = [str(p) for p in roots if p.is_dir()]
    if found:
        os.environ["PATH"] = os.pathsep.join(found + [os.environ.get("PATH", "")])


def _message(text: str, error: bool = False) -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "Rotary", 0x10 if error else 0x40)
    else:
        print(text, file=sys.stderr)


def _app_browsers() -> list[str]:
    """Chromium browsers that can open a page as a standalone app window."""
    if sys.platform == "win32":
        found = []
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                found += [
                    Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                    Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
                ]
        return [str(p) for p in found if p.exists()]
    if sys.platform == "darwin":
        apps = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
        return [a for a in apps if Path(a).exists()]
    names = ("microsoft-edge", "google-chrome", "chromium", "chromium-browser")
    return [p for p in (shutil.which(n) for n in names) if p]


def open_window(url: str) -> bool:
    """Open Rotary in a window without tabs or an address bar; fall back to a normal tab."""
    for browser in _app_browsers():
        try:
            subprocess.Popen(
                [browser, f"--app={url}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except OSError:
            continue
    return webbrowser.open(url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="Rotary", description="Open the Rotary file converter.")
    parser.add_argument("--port", type=int, help="port to serve on (default 8765)")
    parser.add_argument("--no-window", action="store_true", help="start the server without opening a window")
    parser.add_argument("--selftest", metavar="REPORT", help="run a quick conversion check, write a report, exit")
    args = parser.parse_args(argv)

    _attach_log()
    _use_bundled_tools()
    try:
        if args.selftest:
            from .selftest import run

            return run(Path(args.selftest))
        return _run(args)
    except Exception:
        traceback.print_exc()
        _message(
            f"Rotary couldn't start:\n\n{traceback.format_exc(limit=3)}\n"
            f"Details are in {data_dir() / 'rotary.log'}",
            error=True,
        )
        return 1


def _run(args) -> int:
    from .engine import Engine
    from .web.server import DEFAULT_PORT, find_running, make_server, watch_idle

    port = args.port or DEFAULT_PORT
    running = find_running(port)
    if running:
        if not args.no_window:
            open_window(running)
        return 0

    engine = Engine()
    try:
        server = make_server(engine, "127.0.0.1", port)
    except OSError:
        server = make_server(engine, "127.0.0.1", 0)  # another program has our port
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    threading.Thread(target=lambda: engine.graph, daemon=True).start()  # probe backends early
    threading.Thread(target=watch_idle, args=(server,), daemon=True).start()
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} Rotary {__version__} serving {url}")

    if not args.no_window and not open_window(url):
        _message(f"Rotary is running, but no browser window could be opened.\n\nOpen this address yourself:\n{url}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} Rotary stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
