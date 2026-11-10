"""HTTP endpoints for the desktop app's "Get signatures" tab (signing without a server).

Mixed into the web server's request handler. Everything lives under
/api/requests; see rotary/sign/local.py for what each step does to the PDFs.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, quote

from ..sign import local, outlook
from ..sign import pdf as sign_pdf

EMAIL = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
MAX_PDF = 100 * 1024**2


def _one_line(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _can_seal() -> bool:
    try:
        import pyhanko  # noqa: F401
    except ImportError:
        return False
    return True


def _signer_mail(request: dict, signer: dict) -> dict:
    first = signer["name"].split()[0] if signer["name"].split() else signer["name"]
    sender = request.get("sender") or ""
    return {
        "to": signer.get("email", ""),
        "subject": f"Please sign: {request['title']}",
        "body": (
            f"Hi {first},\n\nPlease sign the attached document, \"{request['title']}\".\n\n"
            "1. Open the attached PDF in Adobe Acrobat Reader or Microsoft Edge.\n"
            "2. Fill in the yellow boxes. In a signature box, type your full name or draw your signature.\n"
            "3. Save the file and email it back to me.\n\n"
            f"Thanks,\n{sender}"
        ).rstrip(),
    }


def _finished_mail(request: dict) -> dict:
    return {
        "to": "; ".join(s["email"] for s in request["signers"] if s.get("email")),
        "subject": f"Signed: {request['title']}",
        "body": f"Hi all,\n\nEveryone has signed \"{request['title']}\". The completed copy is attached.\n\nThanks,\n{request.get('sender') or ''}".rstrip(),
    }


def _summary(request: dict) -> dict:
    signers = request["signers"]
    return {
        "id": request["id"],
        "title": request["title"],
        "status": request["status"],
        "created_at": request["created_at"],
        "signers": len(signers),
        "signed": sum(1 for s in signers if s.get("status") == "signed"),
    }


def _reveal(path: Path) -> None:
    """Show a file in Explorer/Finder, so it can be dragged into an email."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


class RequestsAPI:
    """Handler methods; ``self`` is the web server's request handler."""

    # ------------------------------------------------------------------ routing

    def _requests(self, method: str, url) -> None:
        store = self.server.request_store
        parts = [p for p in url.path.split("/") if p][2:]  # drop "api", "requests"
        try:
            if not parts:
                if method == "GET":
                    return self._json({
                        "requests": [_summary(r) for r in store.list()],
                        "outlook": outlook.available(),
                        "can_seal": _can_seal(),
                    })
                return self._req_create(parse_qs(url.query))
            request = store.get(parts[0])
            action = parts[1] if len(parts) > 1 else ""
            arg = parts[2] if len(parts) > 2 else None
            route = {
                ("GET", ""): self._req_detail,
                ("GET", "page"): self._req_page,
                ("GET", "copy"): self._req_copy,
                ("GET", "signed"): self._req_signed,
                ("POST", "draft"): self._req_draft,
                ("POST", "send"): self._req_send,
                ("POST", "email"): self._req_email,
                ("POST", "reveal"): self._req_reveal,
                ("POST", "return"): self._req_return,
                ("POST", "accept"): self._req_accept,
                ("POST", "finish"): self._req_finish,
                ("POST", "delete"): self._req_delete,
                ("POST", "email-signed"): self._req_email_signed,
            }.get((method, action))
            if route is None:
                return self._error("Not found", HTTPStatus.NOT_FOUND)
            return route(store, request, arg)
        except KeyError:
            return self._error("That request no longer exists", HTTPStatus.NOT_FOUND)
        except sign_pdf.SignError as exc:
            return self._error(str(exc), HTTPStatus.UNPROCESSABLE_ENTITY)
        except outlook.OutlookError as exc:
            return self._error(str(exc), HTTPStatus.BAD_GATEWAY)

    def _signer_index(self, request: dict, arg: str | None) -> int:
        try:
            index = int(arg or "")
        except ValueError:
            raise KeyError(arg) from None
        if not 0 <= index < len(request["signers"]):
            raise KeyError(arg)
        return index

    def _req_json(self) -> dict | None:
        body = self._body(MAX_PDF)
        if body is None:
            return None
        try:
            return json.loads(body or b"{}")
        except ValueError:
            self._error("Malformed request")
            return None

    def _req_detail(self, store, request: dict, _arg=None, status: int = 200) -> None:
        out = {k: v for k, v in request.items() if k != "signers"}
        out["signers"] = [
            {k: v for k, v in s.items() if k != "values"} | {"mail": _signer_mail(request, s)}
            for s in request["signers"]
        ]
        out["pages"] = [{"src": f"/api/requests/{request['id']}/page/{i}", "width": w, "height": h} for i, (w, h) in enumerate(request["pages"])]
        out["finished_mail"] = _finished_mail(request)
        out["outlook"] = outlook.available()
        out["can_seal"] = _can_seal()
        self._json(out, status)

    def _send_pdf(self, path: Path, filename: str) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        self.send_header("X-Rotary-Filename", quote(filename))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def _stem(request: dict) -> str:
        stem = Path(request["filename"]).stem or "document"
        return re.sub(r'[\\/:*?"<>|]+', "_", stem)[:120]

    # ------------------------------------------------------------------ steps

    def _req_create(self, query: dict) -> None:
        data = self._body(MAX_PDF)
        if data is None:
            return
        name = Path(query.get("name", ["document.pdf"])[0].replace("\\", "/")).name or "document.pdf"
        sender = _one_line(query.get("sender", [""])[0], 120)
        request = self.server.request_store.create(data, name[:200], sender)
        self._req_detail(None, request, status=201)

    def _req_page(self, store, request: dict, arg) -> None:
        try:
            index = int(arg or "")
        except ValueError:
            return self._error("Not found", HTTPStatus.NOT_FOUND)
        cache = store.path(request, f"pages/{index}.png")
        if not cache.exists():
            if not 0 <= index < len(request["pages"]):
                return self._error("Not found", HTTPStatus.NOT_FOUND)
            cache.parent.mkdir(exist_ok=True)
            cache.write_bytes(sign_pdf.render_page(store.path(request, "original.pdf").read_bytes(), index, 1100))
        png = cache.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(png)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(png)

    def _req_draft(self, store, request: dict, _arg) -> None:
        body = self._req_json()
        if body is None:
            return
        if request["status"] != "draft":
            return self._error("This request has already been sent", HTTPStatus.CONFLICT)
        signers = []
        for s in body.get("signers") or []:
            name, email = _one_line(s.get("name"), 120), _one_line(s.get("email"), 200)
            if not name or (email and not EMAIL.match(email)):
                return self._error(f"Each signer needs a name, and a valid email if you give one ({name or email or 'blank'})", HTTPStatus.UNPROCESSABLE_ENTITY)
            signers.append({"name": name, "email": email})
        fields, seen = [], set()
        for raw in body.get("fields") or []:
            f = sign_pdf.Field.from_dict(raw)
            if f.page >= len(request["pages"]) or f.signer is None or not 0 <= f.signer < len(signers):
                return self._error("Every field must be on the document and belong to a signer", HTTPStatus.UNPROCESSABLE_ENTITY)
            field_id = re.sub(r"[^A-Za-z0-9-]", "", f.id or "")[:40] or f"f{len(fields)}"
            while field_id in seen:
                field_id += "x"
            seen.add(field_id)
            fields.append({"id": field_id, "kind": f.kind, "page": f.page, "x": f.x, "y": f.y, "w": f.w, "h": f.h, "signer": f.signer, "required": f.required})
        request["title"] = _one_line(body.get("title"), 200) or request["title"]
        request["signers"], request["fields"] = signers, fields
        store.save(request)
        self._req_detail(store, request)

    def _req_send(self, store, request: dict, _arg) -> None:
        if request["status"] != "draft":
            return self._error("This request has already been sent", HTTPStatus.CONFLICT)
        if not request["signers"]:
            return self._error("Add at least one signer", HTTPStatus.UNPROCESSABLE_ENTITY)
        for i, s in enumerate(request["signers"]):
            if not any(f["signer"] == i for f in request["fields"]):
                return self._error(f"{s['name']} has no fields yet. Give them at least a signature.", HTTPStatus.UNPROCESSABLE_ENTITY)
        original = store.path(request, "original.pdf").read_bytes()
        for i, s in enumerate(request["signers"]):
            copy = local.make_copy(
                original, [f for f in request["fields"] if f["signer"] == i],
                request_id=request["id"], signer=i, signer_name=s["name"], sender=request.get("sender") or "the sender", title=request["title"],
            )
            store.path(request, f"copy-{i}.pdf").write_bytes(copy)
            s.update(status="waiting", sent_at=local.now())
        request["status"] = "out"
        store.event(request, request.get("sender") or "You", f"Made signing copies for {', '.join(s['name'] for s in request['signers'])}")
        store.save(request)
        self._req_detail(store, request)

    def _req_copy(self, store, request: dict, arg) -> None:
        i = self._signer_index(request, arg)
        path = store.path(request, f"copy-{i}.pdf")
        if not path.exists():
            return self._error("Make the signing copies first", HTTPStatus.CONFLICT)
        name = re.sub(r'[\\/:*?"<>|]+', "_", request["signers"][i]["name"])
        self._send_pdf(path, f"{self._stem(request)} - for {name} to sign.pdf")

    def _req_email(self, store, request: dict, arg) -> None:
        i = self._signer_index(request, arg)
        path = store.path(request, f"copy-{i}.pdf")
        if not path.exists():
            return self._error("Make the signing copies first", HTTPStatus.CONFLICT)
        signer = request["signers"][i]
        mail = _signer_mail(request, signer)
        # Attach it under a friendly name rather than copy-0.pdf.
        named = store.path(request, f"outbox/{self._stem(request)} - for {re.sub(r'[^A-Za-z0-9 ._-]+', '_', signer['name'])} to sign.pdf")
        named.parent.mkdir(exist_ok=True)
        named.write_bytes(path.read_bytes())
        outlook.draft([signer["email"]] if signer.get("email") else [], mail["subject"], mail["body"], [named])
        store.event(request, request.get("sender") or "You", f"Opened an Outlook email to {signer['name']}")
        store.save(request)
        self._json({"ok": True})

    def _req_reveal(self, store, request: dict, arg) -> None:
        if arg == "signed":
            path = store.path(request, "signed.pdf")
        else:
            path = store.path(request, f"copy-{self._signer_index(request, arg)}.pdf")
        if not path.exists():
            return self._error("That file doesn't exist yet", HTTPStatus.CONFLICT)
        _reveal(path)
        self._json({"ok": True})

    def _req_return(self, store, request: dict, arg) -> None:
        i = self._signer_index(request, arg)
        data = self._body(MAX_PDF)
        if data is None:
            return
        if request["status"] != "out":
            return self._error("This request isn't waiting for signatures", HTTPStatus.CONFLICT)
        signer = request["signers"][i]
        result = local.read_return(
            store.path(request, f"copy-{i}.pdf").read_bytes(), data,
            [f for f in request["fields"] if f["signer"] == i],
            request_id=request["id"], signer=i, signer_names=[s["name"] for s in request["signers"]],
        )
        store.path(request, f"returned-{i}.pdf").write_bytes(data)
        signer.update(
            status="check" if result.issues else "signed", returned_at=local.now(), returned_sha256=sign_pdf.sha256(data),
            values=result.values, issues=result.issues, accepted_reason=None,
        )
        action = f"Added {signer['name']}'s signed copy"
        if result.issues:
            action += ": " + " ".join(result.issues)
        if not result.form_found:
            action += " (its form fields were gone, so it was read from the page images)"
        store.event(request, request.get("sender") or "You", action)
        store.save(request)
        self._req_detail(store, request)

    def _req_accept(self, store, request: dict, arg) -> None:
        i = self._signer_index(request, arg)
        body = self._req_json()
        if body is None:
            return
        reason = _one_line(body.get("reason"), 300)
        signer = request["signers"][i]
        if signer.get("status") != "check":
            return self._error("There's nothing to accept for this signer", HTTPStatus.CONFLICT)
        if not reason:
            return self._error("Say why you're accepting it; the reason goes on the certificate", HTTPStatus.UNPROCESSABLE_ENTITY)
        signer.update(status="signed", accepted_reason=reason)
        store.event(request, request.get("sender") or "You", f"Accepted {signer['name']}'s copy despite: {' '.join(signer.get('issues') or [])} Reason: {reason}")
        store.save(request)
        self._req_detail(store, request)

    def _req_finish(self, store, request: dict, _arg) -> None:
        body = self._req_json()
        if body is None:
            return
        if request["status"] != "out" or not all(s.get("status") == "signed" for s in request["signers"]):
            return self._error("Everyone needs to have signed first", HTTPStatus.CONFLICT)
        sealer = None
        if body.get("seal") and _can_seal():
            from ..app import data_dir

            folder = data_dir() / "seal"
            key, cert = folder / "seal-key.pem", folder / "seal-cert.pem"
            if not key.exists():
                sign_pdf.create_seal_certificate(folder, request.get("sender") or "Rotary")
            sealer = sign_pdf.load_sealer(key=key, cert=cert)
        store.event(request, request.get("sender") or "You", "All signed; finished the document" + (" and sealed it" if sealer else ""))
        final = local.finish(store.path(request, "original.pdf").read_bytes(), request, seal_with=sealer)
        store.path(request, "signed.pdf").write_bytes(final)
        request.update(status="completed", completed_at=local.now(), final_sha256=sign_pdf.sha256(final), sealed=bool(sealer))
        store.save(request)
        self._req_detail(store, request)

    def _req_signed(self, store, request: dict, _arg) -> None:
        path = store.path(request, "signed.pdf")
        if not path.exists():
            return self._error("It isn't finished yet", HTTPStatus.CONFLICT)
        self._send_pdf(path, f"{self._stem(request)} - signed.pdf")

    def _req_delete(self, store, request: dict, _arg) -> None:
        store.delete(request["id"])
        self._json({"ok": True})

    def _req_email_signed(self, store, request: dict, _arg) -> None:
        path = store.path(request, "signed.pdf")
        if not path.exists():
            return self._error("It isn't finished yet", HTTPStatus.CONFLICT)
        mail = _finished_mail(request)
        named = store.path(request, f"outbox/{self._stem(request)} - signed.pdf")
        named.parent.mkdir(exist_ok=True)
        named.write_bytes(path.read_bytes())
        outlook.draft([s["email"] for s in request["signers"] if s.get("email")], mail["subject"], mail["body"], [named])
        self._json({"ok": True})
