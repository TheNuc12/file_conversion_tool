import http.client
import json
import os
import threading
from pathlib import Path
from urllib.parse import quote

import pytest

from rotary.web.server import make_server


@pytest.fixture(scope="module")
def server():
    from rotary.engine import Engine

    srv = make_server(Engine(), "127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request(method, path, body=body, headers=headers or {})
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res, data


def test_index_and_formats(server):
    res, body = request(server, "GET", "/")
    assert res.status == 200 and b'id="dial"' in body
    res, body = request(server, "GET", "/api/formats")
    data = json.loads(body)
    assert data["formats"]["jpeg"]["extension"] == "jpg"


def test_targets(server):
    res, body = request(server, "GET", "/api/targets?file=" + quote("holiday photo.PNG"))
    data = json.loads(body)
    assert data["source"] == "png"
    ids = [t["id"] for t in data["targets"]]
    assert "jpeg" in ids and ids.index("jpeg") < ids.index("pcx")  # common formats first
    res, _ = request(server, "GET", "/api/targets?file=README")
    assert res.status == 422


def test_convert_round_trip(server, rgba_png):
    res, body = request(
        server, "POST", "/api/convert?name=logo.png&to=jpg&quality=50",
        body=rgba_png.read_bytes(), headers={"X-Rotary": "1"},
    )
    assert res.status == 200
    assert res.getheader("X-Rotary-Filename") == "logo.jpg"
    assert body[:3] == b"\xff\xd8\xff"


def test_convert_errors(server, rgba_png):
    payload = rgba_png.read_bytes()
    res, body = request(server, "POST", "/api/convert?name=logo.png&to=mp3", body=payload, headers={"X-Rotary": "1"})
    assert res.status == 422 and "No way to convert" in json.loads(body)["error"]
    res, body = request(server, "POST", "/api/convert?name=x.png&to=jpg", body=b"junk", headers={"X-Rotary": "1"})
    assert res.status == 500 and "Couldn't read" in json.loads(body)["error"]


def test_cross_site_protections(server, rgba_png):
    res, _ = request(server, "POST", "/api/convert?name=a.png&to=jpg", body=rgba_png.read_bytes())
    assert res.status == 403
    res, _ = request(server, "GET", "/api/formats", headers={"Host": "attacker.example:80"})
    assert res.status == 403


def test_upload_names_cannot_escape(server, rgba_png):
    res, _ = request(
        server, "POST", "/api/convert?name=" + quote("../../etc/evil.png") + "&to=webp",
        body=rgba_png.read_bytes(), headers={"X-Rotary": "1"},
    )
    assert res.status == 200 and res.getheader("X-Rotary-Filename") == "evil.webp"
    assert not Path("/etc/evil.webp").exists()


@pytest.mark.slow
def test_dial_in_a_browser(server, rgba_png, tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            # ROTARY_TEST_CHROMIUM points at a browser when Playwright's own download is missing.
            browser = p.chromium.launch(executable_path=os.environ.get("ROTARY_TEST_CHROMIUM"))
        except Exception as exc:
            pytest.skip(f"no browser available: {exc}")
        page = browser.new_page(accept_downloads=True)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{server}/")
        page.set_input_files("#picker", str(rgba_png))
        page.wait_for_selector("#labels .label")
        assert page.text_content("#big") == "JPG"
        page.focus("#dial")
        page.keyboard.press("ArrowRight")
        assert page.text_content("#big") == "WEBP"
        assert page.get_attribute("#dial", "aria-valuetext") == "WEBP, WebP image"
        with page.expect_download() as download:
            page.keyboard.press("Enter")
        assert download.value.suggested_filename == "logo.webp"
        assert not errors
        browser.close()
