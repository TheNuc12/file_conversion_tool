from pathlib import Path

import pytest

from rotary.converters import ConversionError, Converter, Edge, Options
from rotary.engine import MAX_STEPS, Engine, NoRouteError, RotaryError


class Fake(Converter):
    """Copies bytes and records each call, with edges supplied by the test."""

    def __init__(self, label, specs, fail_on=None):
        self.label = label
        self.specs = specs
        self.fail_on = fail_on
        self.calls = []

    def edges(self):
        return [Edge(s, d, self, **kw) for s, d, kw in self.specs]

    def convert(self, src: Path, dst: Path, src_fmt, dst_fmt, options: Options):
        self.calls.append((src.name, dst.name))
        if (src_fmt, dst_fmt) == self.fail_on:
            raise ConversionError("boom")
        dst.write_bytes(src.read_bytes() + f"|{dst_fmt}".encode())


def e(src, dst, **kw):
    return (src, dst, kw)


def test_prefers_cheapest_route():
    direct = Fake("slow", [e("docx", "pdf", cost=5)])
    hop = Fake("fast", [e("docx", "odt"), e("odt", "pdf")])
    route = Engine([direct, hop]).route("docx", "pdf")
    assert route.formats == ["docx", "odt", "pdf"]
    assert route.backends == ["fast"]


def test_equal_cost_goes_to_first_registered_converter():
    a = Fake("a", [e("png", "jpeg")])
    b = Fake("b", [e("png", "jpeg")])
    assert Engine([a, b]).route("png", "jpeg").backends == ["a"]
    assert Engine([b, a]).route("png", "jpeg").backends == ["b"]


def test_terminal_edges_only_end_routes():
    conv = Fake("x", [e("png", "pdf", terminal=True), e("pdf", "txt")])
    engine = Engine([conv])
    assert engine.route("png", "pdf").formats == ["png", "pdf"]
    with pytest.raises(NoRouteError):
        engine.route("png", "txt")


def test_initial_edges_only_start_routes():
    conv = Fake("x", [e("gif", "mp4", initial=True), e("png", "gif")])
    engine = Engine([conv])
    assert engine.route("gif", "mp4").formats == ["gif", "mp4"]
    with pytest.raises(NoRouteError):
        engine.route("png", "mp4")


def test_routes_longer_than_the_limit_are_dropped():
    chain = ["json", "yaml", "toml", "xml", "csv", "tsv", "xlsx"]
    conv = Fake("x", [e(a, b) for a, b in zip(chain, chain[1:])])
    reachable = Engine([conv]).routes_from("json")
    assert set(reachable) == set(chain[1 : MAX_STEPS + 1])


def test_same_format_and_unknown_formats_are_rejected():
    engine = Engine([Fake("x", [e("png", "jpeg")])])
    with pytest.raises(RotaryError, match="already"):
        engine.route("png", "png")
    with pytest.raises(ValueError, match="Unknown format"):
        engine.route("png", "nope")


def test_unavailable_backends_are_named_in_no_route_errors():
    class Missing(Fake):
        requirement = "install the thing"

        def is_available(self):
            return False

    engine = Engine([Fake("x", []), Missing("Thing", [e("png", "mp3")])])
    with pytest.raises(NoRouteError, match="install the thing"):
        engine.route("png", "mp3")


def test_convert_runs_each_step_with_real_extensions(tmp_path):
    conv = Fake("x", [e("markdown", "docx"), e("docx", "pdf", cost=3), e("markdown", "html"), e("html", "pdf", cost=4)])
    src = tmp_path / "Report.md"
    src.write_text("hi")
    result = Engine([conv]).convert(src, "pdf")
    assert result.output == tmp_path / "Report.pdf"
    assert result.output.read_bytes() == b"hi|docx|pdf"
    assert conv.calls == [("Report.md", "Report.docx"), ("Report.docx", "Report.pdf")]


def test_convert_output_options(tmp_path):
    conv = Fake("x", [e("png", "jpeg")])
    engine = Engine([conv])
    src = tmp_path / "a.png"
    src.write_bytes(b"img")

    out = engine.convert(src, "jpg", out_dir=tmp_path / "out").output
    assert out == tmp_path / "out" / "a.jpg"

    explicit = engine.convert(src, output=tmp_path / "custom.jpeg").output
    assert explicit.name == "custom.jpeg"

    with pytest.raises(RotaryError, match="already exists"):
        engine.convert(src, "jpg", out_dir=tmp_path / "out")
    engine.convert(src, "jpg", out_dir=tmp_path / "out", overwrite=True)


def test_from_format_overrides_detection(tmp_path):
    conv = Fake("x", [e("json", "yaml")])
    src = tmp_path / "data.txt"
    src.write_text("{}")
    result = Engine([conv]).convert(src, "yaml", from_format="json")
    assert result.output.name == "data.yaml"
    # The backend sees a correctly named file, not the misleading .txt.
    assert conv.calls[0][0] == "data.json"


def test_failure_mid_route_says_which_step(tmp_path):
    conv = Fake("x", [e("markdown", "docx"), e("docx", "pdf")], fail_on=("docx", "pdf"))
    src = tmp_path / "a.md"
    src.write_text("x")
    with pytest.raises(ConversionError, match=r"Step 2 of 2 \(docx -> pdf\) failed: boom"):
        Engine([conv]).convert(src, "pdf")
    assert not (tmp_path / "a.pdf").exists()


def test_real_graph_has_no_nonsense_routes(engine):
    if not {"media", "images"} <= {c.name for c in engine.available}:
        pytest.skip("needs Pillow and FFmpeg")
    assert "mp3" not in engine.routes_from("png")  # no PNG -> GIF -> MP4 -> MP3
    assert "ico" not in engine.routes_from("mp4")  # video only reaches GIF/PNG/JPEG as images
    assert "mp4" in engine.routes_from("gif")
