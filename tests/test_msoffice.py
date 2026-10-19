"""Microsoft Office conversions, against a simulated Office (real Office can't run in CI).

The fakes record every COM call, so these tests pin down what Rotary asks Office
to do: the file-format codes, macros off, no password prompts, never quitting
or killing an Office window the user has open, and the watchdog.
"""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from rotary.converters import ConversionError, Options
from rotary.converters.msoffice import MicrosoftOfficeConverter, _com_message
from rotary.engine import Engine


class FakeComError(Exception):
    """Shaped like pywintypes.com_error: (hresult, text, excepinfo, argerr)."""

    def __init__(self, description):
        super().__init__(-2147352567, "Exception occurred.", (0, "Microsoft Office", description, None, 0, -2146822880), None)


class FakeFile:
    def __init__(self, app, options):
        self.app, self.options = app, options

    def _write(self, path):
        self.app.calls.append(("write", path))
        if self.app.produce:
            Path(path).write_bytes(b"%PDF-1.7 fake" if str(path).endswith(".pdf") else b"fake")

    def ExportAsFixedFormat(self, **kw):
        self.app.calls.append(("export", kw))
        self._write(kw.get("OutputFileName") or kw["Filename"])

    def SaveAs2(self, **kw):
        self.app.calls.append(("saveas2", kw))
        self._write(kw["FileName"])

    def SaveAs(self, **kw):
        self.app.calls.append(("saveas", kw))
        self._write(kw.get("FileName") or kw["Filename"])

    def Close(self, **kw):
        self.app.calls.append(("close", kw))


class FakeFiles:
    def __init__(self, app, count):
        self.app, self.Count = app, count

    def Open(self, **kw):
        app = self.app
        app.calls.append(("open", kw, app.AutomationSecurity))
        if app.hang:
            app.hang.wait(5)  # stuck on an invisible dialog until the process is killed
            raise FakeComError("The RPC server is unavailable.")
        if app.open_error:
            raise app.open_error
        return FakeFile(app, kw)


class FakeApp:
    def __init__(self, kind, open_files=0):
        self.kind = kind
        self.calls = []
        self.quit = None
        self.hang = None
        self.open_error = None
        self.produce = True
        self.AutomationSecurity = 1
        self.DisplayAlerts = 2
        self.Visible = True
        self.NormalTemplate = SimpleNamespace(Saved=False)
        files = FakeFiles(self, open_files)
        self.Documents = self.Workbooks = self.Presentations = files

    def Quit(self, **kw):
        self.quit = kw


@pytest.fixture
def office():
    conv = MicrosoftOfficeConverter()
    conv.__dict__["apps"] = {"word", "excel", "powerpoint"}
    conv.started = []
    conv.killed = []
    conv.active = None
    conv.next_app = None
    conv._com_init = lambda: None
    conv._get_active = lambda progid: conv.active

    def dispatch(progid):
        conv.started.append(progid)
        return conv.next_app

    conv._dispatch = dispatch
    conv._pid = lambda kind, app: 4242
    conv._kill = conv.killed.append
    return conv


def run(conv, app, tmp_path, src_fmt, dst_fmt, name=None):
    conv.next_app = app
    src = tmp_path / (name or f"input.{src_fmt}")
    src.write_bytes(b"source")
    dst = tmp_path / f"output.{dst_fmt}"
    conv.convert(src, dst, src_fmt, dst_fmt, Options())
    return src, dst


def test_word_to_pdf(office, tmp_path):
    app = FakeApp("word")
    src, dst = run(office, app, tmp_path, "docx", "pdf")
    assert office.started == ["Word.Application"]
    _, opened, security_at_open = app.calls[0]
    assert opened["FileName"] == str(src) and opened["ReadOnly"] is True and opened["ConfirmConversions"] is False
    assert opened["PasswordDocument"]  # stops Word prompting for a password
    assert security_at_open == 3  # macros were already off when the file opened
    assert ("export", {"OutputFileName": str(dst), "ExportFormat": 17, "OpenAfterExport": False}) in app.calls
    assert ("close", {"SaveChanges": 0}) in app.calls
    assert app.quit == {"SaveChanges": 0} and app.NormalTemplate.Saved is True
    assert app.Visible is False and app.DisplayAlerts == 0
    assert dst.read_bytes().startswith(b"%PDF")


@pytest.mark.parametrize("src_fmt, dst_fmt, file_format, encoding", [
    ("docx", "odt", 23, None), ("doc", "docx", 16, None), ("rtf", "doc", 0, None),
    ("docx", "txt", 7, 65001), ("docx", "html", 10, 65001), ("odt", "rtf", 6, None),
])
def test_word_save_formats(office, tmp_path, src_fmt, dst_fmt, file_format, encoding):
    app = FakeApp("word")
    run(office, app, tmp_path, src_fmt, dst_fmt)
    [(_, saved)] = [c for c in app.calls if c[0] == "saveas2"]
    assert saved["FileFormat"] == file_format and saved.get("Encoding") == encoding


def test_word_reads_text_files_as_utf8(office, tmp_path):
    app = FakeApp("word")
    run(office, app, tmp_path, "txt", "docx")
    assert app.calls[0][1]["Encoding"] == 65001


def test_excel(office, tmp_path):
    app = FakeApp("excel")
    src, dst = run(office, app, tmp_path, "xlsx", "pdf")
    _, opened, security = app.calls[0]
    assert opened["ReadOnly"] is True and opened["UpdateLinks"] == 0 and opened["Password"] and security == 3
    assert ("export", {"Type": 0, "Filename": str(dst), "OpenAfterPublish": False}) in app.calls
    assert ("close", {"SaveChanges": False}) in app.calls and app.quit == {}
    assert app.Visible is False and app.DisplayAlerts is False

    app = FakeApp("excel")
    run(office, app, tmp_path, "xls", "ods")
    assert [c[1]["FileFormat"] for c in app.calls if c[0] == "saveas"] == [60]


def test_powerpoint(office, tmp_path):
    app = FakeApp("powerpoint")
    src, dst = run(office, app, tmp_path, "pptx", "pdf")
    _, opened, _ = app.calls[0]
    assert opened == {"FileName": str(src), "ReadOnly": -1, "Untitled": 0, "WithWindow": 0}
    assert ("saveas", {"FileName": str(dst), "FileFormat": 32}) in app.calls
    assert app.quit == {}


def test_powerpoint_the_user_has_open_is_left_alone(office, tmp_path):
    theirs = FakeApp("powerpoint", open_files=2)
    office.active = theirs
    run(office, None, tmp_path, "pptx", "pdf")
    assert office.started == []  # used the running copy rather than starting another
    assert theirs.quit is None
    assert (theirs.AutomationSecurity, theirs.DisplayAlerts) == (1, 2)  # their settings restored


def test_shared_word_instance_is_not_quit(office, tmp_path):
    app = FakeApp("word", open_files=1)
    run(office, app, tmp_path, "docx", "pdf")
    assert app.quit is None and app.Visible is True and app.AutomationSecurity == 1


def test_office_errors_are_readable(office, tmp_path):
    app = FakeApp("word")
    app.open_error = FakeComError("The password is incorrect. Word cannot open the document.")
    with pytest.raises(ConversionError, match="Microsoft Word couldn't convert secret.docx: The password is incorrect"):
        run(office, app, tmp_path, "docx", "pdf", name="secret.docx")
    assert app.quit == {"SaveChanges": 0}  # still tidied up


def test_missing_output_is_an_error(office, tmp_path):
    app = FakeApp("excel")
    app.produce = False
    with pytest.raises(ConversionError, match="didn't produce a PDF"):
        run(office, app, tmp_path, "xlsx", "pdf")


def test_watchdog_ends_a_hung_office(office, tmp_path):
    app = FakeApp("word")
    app.hang = threading.Event()
    office.timeout = 0.3
    office._kill = lambda pid: (office.killed.append(pid), app.hang.set())
    with pytest.raises(ConversionError, match="stopped responding"):
        run(office, app, tmp_path, "docx", "pdf")
    assert office.killed == [4242]
    assert app.quit is None  # nothing to quit: the process was ended


def test_watchdog_never_kills_the_users_office(office, tmp_path):
    theirs = FakeApp("powerpoint", open_files=1)
    theirs.hang = threading.Event()
    office.active = theirs
    office.timeout = 0.2
    threading.Timer(0.6, theirs.hang.set).start()
    with pytest.raises(ConversionError):
        run(office, None, tmp_path, "pptx", "pdf")
    assert office.killed == []


def test_busy_office_gives_up_instead_of_queueing_forever(office, tmp_path):
    office.timeout = -59.8  # lock wait is timeout + 60 seconds
    MicrosoftOfficeConverter._lock.acquire()
    try:
        with pytest.raises(ConversionError, match="still busy"):
            run(office, FakeApp("word"), tmp_path, "docx", "pdf")
    finally:
        MicrosoftOfficeConverter._lock.release()


def test_routing_prefers_office_over_libreoffice(office, tmp_path):
    from rotary.converters import default_converters

    converters = [office if c.name == "msoffice" else c for c in default_converters()]
    engine = Engine(converters)
    assert engine.route("docx", "pdf").backends == ["Microsoft Office"]
    assert engine.route("pptx", "pdf").backends == ["Microsoft Office"]
    assert engine.route("xlsx", "ods").backends == ["Microsoft Office"]
    # Quick built-in converters still win where they can do the job.
    assert engine.route("xlsx", "csv").backends == ["built-in data"]
    if any(c.name == "documents" for c in engine.available):
        assert engine.route("docx", "html").backends == ["Pandoc"]
    # And through the engine end to end, with a real temp folder.
    office.next_app = FakeApp("word")
    src = tmp_path / "Report Q3.docx"
    src.write_bytes(b"x")
    result = Engine([office]).convert(src, "pdf")
    assert result.output.name == "Report Q3.pdf" and result.output.read_bytes().startswith(b"%PDF")


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows behaviour")
def test_unavailable_off_windows():
    assert MicrosoftOfficeConverter().is_available() is False


def test_com_message():
    assert _com_message(FakeComError("Document is locked.")) == "Document is locked."
    assert _com_message(Exception(-1, "Call was rejected by callee.", None, None)) == "Call was rejected by callee."
    assert _com_message(ValueError("plain")) == "plain"
