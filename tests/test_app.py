"""The desktop launcher: single instance, and quitting when its window closes."""

import http.client
import threading
import time

import pytest

from rotary import app
from rotary.engine import Engine
from rotary.web.server import find_running, make_server, watch_idle


def start(**idle):
    server = make_server(Engine(), "127.0.0.1", 0)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    watcher = threading.Thread(target=watch_idle, args=(server,), kwargs={"poll": 0.05, **idle}, daemon=True)
    watcher.start()
    return server, serving


def call(server, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    res = conn.getresponse()
    res.read()
    conn.close()
    return res.status


def test_quits_when_the_last_window_says_goodbye():
    server, serving = start(startup_grace=30, close_grace=0.3)
    assert call(server, "GET", "/api/ping?tab=a") == 200
    assert call(server, "GET", "/api/ping?tab=b") == 200
    assert call(server, "POST", "/api/bye?tab=a") == 204
    time.sleep(0.6)
    assert serving.is_alive()  # tab b is still open
    assert call(server, "POST", "/api/bye?tab=b") == 204
    serving.join(timeout=3)
    assert not serving.is_alive()


def test_quits_if_no_window_ever_shows_up():
    server, serving = start(startup_grace=0.3)
    serving.join(timeout=3)
    assert not serving.is_alive()


def test_forgets_windows_that_stop_pinging():
    server, serving = start(startup_grace=30, close_grace=0.1, stale_after=0.3)
    call(server, "GET", "/api/ping?tab=crashed")
    serving.join(timeout=3)
    assert not serving.is_alive()


def test_never_quits_mid_conversion(rgba_png, monkeypatch):
    server, serving = start(startup_grace=0.2, close_grace=0.1)
    engine = server.RequestHandlerClass.engine
    real_convert = engine.convert

    def slow_convert(*args, **kwargs):
        time.sleep(0.8)
        return real_convert(*args, **kwargs)

    monkeypatch.setattr(engine, "convert", slow_convert)
    status = call(server, "POST", "/api/convert?name=a.png&to=jpg", body=rgba_png.read_bytes(), headers={"X-Rotary": "1"})
    assert status == 200
    serving.join(timeout=3)
    assert not serving.is_alive()


def test_bye_cannot_be_used_to_close_other_peoples_windows():
    server, serving = start(startup_grace=30, close_grace=0.1)
    call(server, "GET", "/api/ping?tab=secret")
    call(server, "POST", "/api/bye?tab=guess")
    time.sleep(0.4)
    assert serving.is_alive()
    server.shutdown()


def test_second_launch_reuses_the_running_instance(monkeypatch):
    server, serving = start(startup_grace=30)
    port = server.server_address[1]
    assert find_running(port) == f"http://127.0.0.1:{port}/"
    opened = []
    monkeypatch.setattr(app, "open_window", lambda url: opened.append(url) or True)
    started = time.monotonic()
    assert app.main(["--port", str(port)]) == 0
    assert time.monotonic() - started < 5
    assert opened == [f"http://127.0.0.1:{port}/"]
    server.shutdown()


def test_find_running_ignores_other_programs():
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Other(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *a):
            pass

    other = HTTPServer(("127.0.0.1", 0), Other)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        assert find_running(other.server_address[1]) is None
    finally:
        other.shutdown()


def test_selftest_writes_a_report(tmp_path):
    report = tmp_path / "report.txt"
    assert app.main(["--selftest", str(report)]) == 0
    text = report.read_text()
    assert "ok   png -> jpeg" in text and text.rstrip().endswith("all good")


@pytest.mark.slow
def test_closing_the_browser_window_quits_the_app(rgba_png):
    sync_api = pytest.importorskip("playwright.sync_api")
    import os

    server, serving = start(startup_grace=30, close_grace=0.5)
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=os.environ.get("ROTARY_TEST_CHROMIUM"))
        except Exception as exc:
            server.shutdown()
            pytest.skip(f"no browser available: {exc}")
        page = browser.new_page()
        page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
        page.wait_for_selector("footer .be")
        time.sleep(0.3)
        assert serving.is_alive() and server.tabs
        page.close()
        serving.join(timeout=5)
        assert not serving.is_alive()
        browser.close()
