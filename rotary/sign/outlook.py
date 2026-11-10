"""Open a ready-to-send Outlook email with attachments (classic Outlook for Windows).

Rotary only *opens* the draft; the person reviews it and presses Send. The new
Outlook for Windows has no automation interface, so this reports unavailable
there and the app falls back to a download plus a mailto: link.
"""

from __future__ import annotations

import sys
from pathlib import Path


class OutlookError(RuntimeError):
    pass


def available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import pythoncom  # noqa: F401
        import win32com.client  # noqa: F401
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Outlook.Application\CLSID"):
            return True
    except (ImportError, OSError):
        return False


def _outlook():  # replaced in tests
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    return win32com.client.Dispatch("Outlook.Application")


def draft(to: list[str], subject: str, body: str, attachments: list[Path]) -> None:
    """Show a new email in Outlook, addressed and with files attached, for the user to send."""
    try:
        app = _outlook()
        mail = app.CreateItem(0)  # olMailItem
        mail.To = "; ".join(to)
        mail.Subject = subject
        mail.Body = body
        for path in attachments:
            mail.Attachments.Add(str(Path(path).resolve()))
        mail.Display(False)  # not modal: Rotary keeps working
    except Exception as exc:
        from ..converters.msoffice import _com_message

        raise OutlookError(f"Outlook couldn't create the email: {_com_message(exc)}") from exc
