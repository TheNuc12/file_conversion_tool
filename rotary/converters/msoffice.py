"""Word, Excel and PowerPoint conversion with Microsoft Office, on Windows.

Drives the installed Office apps through COM automation (pywin32), invisibly
and with macros disabled. Two safeguards matter on a real desktop:

* Rotary never closes or kills an Office app the user already has documents
  open in; it only quits instances it started itself.
* Each conversion has a watchdog. Office sometimes waits on a dialog nobody
  can see (a password prompt, a repair offer); when that happens the watchdog
  ends that Office process, so Rotary reports an error instead of hanging.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import threading
import uuid
from functools import cached_property
from pathlib import Path

from .base import ConversionError, Converter, Edge, Options

# Source formats each app opens, and target format -> SaveAs file format code
# (None: exported with ExportAsFixedFormat instead).
WORD_IN = ["doc", "docx", "odt", "rtf", "txt", "html"]
WORD_OUT = {"pdf": None, "docx": 16, "doc": 0, "odt": 23, "rtf": 6, "txt": 7, "html": 10}
EXCEL_IN = ["xls", "xlsx", "ods"]
EXCEL_OUT = {"pdf": None, "xlsx": 51, "xls": 56, "ods": 60, "csv": 62, "html": 44}
POWERPOINT_IN = ["ppt", "pptx", "odp"]
POWERPOINT_OUT = {"pdf": 32, "pptx": 24, "ppt": 1, "odp": 35}

APPS = {"word": "Word.Application", "excel": "Excel.Application", "powerpoint": "PowerPoint.Application"}
LABELS = {"word": "Microsoft Word", "excel": "Microsoft Excel", "powerpoint": "Microsoft PowerPoint"}
FORMATS = {"word": (WORD_IN, WORD_OUT), "excel": (EXCEL_IN, EXCEL_OUT), "powerpoint": (POWERPOINT_IN, POWERPOINT_OUT)}

# Cheaper than LibreOffice (which handles Office formats less faithfully), dearer than
# the quick built-in converters.
COST = 2.5
TIMEOUT = 240
# Supplying any password stops Office prompting for one; unprotected files ignore it.
NO_PASSWORD = "rotary-no-password"
DISABLE_MACROS = 3  # msoAutomationSecurityForceDisable
UTF8 = 65001  # msoEncodingUTF8
PDF_EXPORT = 17  # wdExportFormatPDF


def _registered(progid: str) -> bool:
    """Whether an Office app is installed, without starting it."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{progid}\CLSID"):
            return True
    except (ImportError, OSError):
        return False


def _com_message(exc: BaseException) -> str:
    """The human part of a pywintypes.com_error, e.g. "The password is incorrect"."""
    args = getattr(exc, "args", ())
    if len(args) >= 3 and args[2] and len(args[2]) >= 3 and args[2][2]:
        return str(args[2][2]).strip()
    if len(args) >= 2 and args[1]:
        return str(args[1])
    return str(exc)


class MicrosoftOfficeConverter(Converter):
    name = "msoffice"
    label = "Microsoft Office"
    requirement = "Windows with Microsoft Office (Word, Excel, PowerPoint) installed"

    _lock = threading.Lock()  # one Office conversion at a time
    timeout = TIMEOUT

    # ------------------------------------------------------------------ discovery

    @cached_property
    def apps(self) -> set[str]:
        if sys.platform != "win32":
            return set()
        try:
            import pythoncom  # noqa: F401
            import win32com.client  # noqa: F401
        except ImportError:
            return set()
        return {kind for kind, progid in APPS.items() if _registered(progid)}

    def is_available(self) -> bool:
        return bool(self.apps)

    def edges(self) -> list[Edge]:
        edges = []
        for kind, (sources, targets) in FORMATS.items():
            if kind in self.apps:
                edges += self._pairs(sources, targets, cost=COST)
        return edges

    # ------------------------------------------------------------------ COM plumbing (replaced in tests)

    def _com_init(self) -> None:
        import pythoncom

        # Each web-server thread needs COM initialised; doing it again is harmless.
        pythoncom.CoInitialize()

    def _dispatch(self, progid: str):
        import win32com.client

        return win32com.client.DispatchEx(progid)  # a new instance where the app allows it

    def _get_active(self, progid: str):
        import win32com.client

        try:
            return win32com.client.GetActiveObject(progid)
        except Exception:
            return None

    def _pid(self, kind: str, app) -> int | None:
        try:
            import win32gui
            import win32process

            if kind == "word":  # Word has no window handle property: find it by a unique caption
                caption = f"Rotary-{uuid.uuid4().hex}"
                app.Caption = caption
                hwnd = win32gui.FindWindow("OpusApp", caption)
            else:
                hwnd = app.Hwnd if kind == "excel" else app.HWND
            return win32process.GetWindowThreadProcessId(hwnd)[1] if hwnd else None
        except Exception:
            return None

    def _kill(self, pid: int) -> None:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows

    # ------------------------------------------------------------------ conversion

    def convert(self, src: Path, dst: Path, src_fmt: str, dst_fmt: str, options: Options) -> None:
        kind = next(k for k, (sources, _) in FORMATS.items() if src_fmt in sources)
        if not self._lock.acquire(timeout=self.timeout + 60):
            raise ConversionError("Microsoft Office is still busy with an earlier file. Close Rotary and open it again.")
        try:
            self._com_init()
            try:
                with self._session(kind) as (app, state):
                    try:
                        getattr(self, f"_{kind}")(app, Path(src), Path(dst), src_fmt, dst_fmt)
                    except Exception as exc:
                        if state["timed_out"]:
                            raise ConversionError(
                                f"{LABELS[kind]} stopped responding while converting {Path(src).name}, so Rotary closed it. "
                                "It may have been waiting on a dialog, for example a password prompt."
                            ) from exc
                        raise ConversionError(f"{LABELS[kind]} couldn't convert {Path(src).name}: {_com_message(exc)}") from exc
            except ConversionError:
                raise
            except Exception as exc:
                raise ConversionError(f"Couldn't start {LABELS[kind]}: {_com_message(exc)}") from exc
        finally:
            self._lock.release()
        if not Path(dst).exists():
            raise ConversionError(f"{LABELS[kind]} didn't produce a {dst_fmt.upper()} file")

    @contextlib.contextmanager
    def _session(self, kind: str):
        """An Office app for one conversion: ours to quit afterwards, or the user's to leave alone."""
        progid = APPS[kind]
        app = self._get_active(progid) if kind == "powerpoint" else None  # PowerPoint runs one copy only
        owned = app is None
        if owned:
            app = self._dispatch(progid)
            if self._open_count(kind, app):  # we were handed a copy someone is working in
                owned = False
        saved = self._configure(kind, app)
        state = {"timed_out": False}
        pid = self._pid(kind, app) if owned else None

        def expire():
            state["timed_out"] = True
            if pid:
                self._kill(pid)

        timer = threading.Timer(self.timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            yield app, state
        finally:
            timer.cancel()
            if state["timed_out"]:
                pass  # the process is gone (or unreachable): nothing left to tidy
            elif owned:
                with contextlib.suppress(Exception):
                    if kind == "word":
                        app.NormalTemplate.Saved = True  # never prompt to save Normal.dotm
                        app.Quit(SaveChanges=0)
                    else:
                        app.Quit()
            else:
                for name, value in saved.items():  # put the user's settings back
                    with contextlib.suppress(Exception):
                        setattr(app, name, value)

    @staticmethod
    def _open_count(kind: str, app) -> int:
        try:
            return {"word": lambda: app.Documents.Count, "excel": lambda: app.Workbooks.Count, "powerpoint": lambda: app.Presentations.Count}[kind]()
        except Exception:
            return 0

    @staticmethod
    def _configure(kind: str, app) -> dict:
        """Invisible, silent, macros off. Returns the previous values so they can be restored."""
        settings = {"AutomationSecurity": DISABLE_MACROS}
        if kind == "word":
            settings.update(DisplayAlerts=0, Visible=False)
        elif kind == "excel":
            settings.update(DisplayAlerts=False, Visible=False, AskToUpdateLinks=False, EnableEvents=False)
        else:
            settings.update(DisplayAlerts=1)  # ppAlertsNone
        saved = {}
        for name, value in settings.items():
            with contextlib.suppress(Exception):
                saved[name] = getattr(app, name)
                setattr(app, name, value)
        return saved

    def _word(self, app, src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
        options = dict(
            FileName=str(src), ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False,
            PasswordDocument=NO_PASSWORD, Visible=False, NoEncodingDialog=True,
        )
        if src_fmt == "txt":
            options["Encoding"] = UTF8
        doc = app.Documents.Open(**options)
        try:
            if dst_fmt == "pdf":
                doc.ExportAsFixedFormat(OutputFileName=str(dst), ExportFormat=PDF_EXPORT, OpenAfterExport=False)
            else:
                save = dict(FileName=str(dst), FileFormat=WORD_OUT[dst_fmt], AddToRecentFiles=False)
                if dst_fmt in ("txt", "html"):
                    save["Encoding"] = UTF8
                doc.SaveAs2(**save)
        finally:
            doc.Close(SaveChanges=0)

    def _excel(self, app, src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
        book = app.Workbooks.Open(
            Filename=str(src), UpdateLinks=0, ReadOnly=True, Password=NO_PASSWORD, AddToMru=False, IgnoreReadOnlyRecommended=True,
        )
        try:
            if dst_fmt == "pdf":
                book.ExportAsFixedFormat(Type=0, Filename=str(dst), OpenAfterPublish=False)  # every sheet
            else:
                book.SaveAs(Filename=str(dst), FileFormat=EXCEL_OUT[dst_fmt])  # CSV: the active sheet
        finally:
            book.Close(SaveChanges=False)

    def _powerpoint(self, app, src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
        deck = app.Presentations.Open(FileName=str(src), ReadOnly=-1, Untitled=0, WithWindow=0)
        try:
            deck.SaveAs(FileName=str(dst), FileFormat=POWERPOINT_OUT[dst_fmt])
        finally:
            deck.Close()
