"""Getting signatures without a server: the desktop app's "Get signatures" tab.

1. ``make_copy`` turns the document into one fillable PDF per signer: a page of
   instructions, then the document with that signer's fields as PDF form fields.
2. Signers fill it in with any PDF reader (Adobe Reader, Edge...) and email it back.
3. ``read_return`` reads a returned copy. Typed values come from the form; anything
   drawn (a signature from Fill & Sign or a pen tool) comes from the pixels inside
   that signer's boxes. Comparing the pages with the copy that was sent also shows
   whether anything outside the boxes was changed.
4. ``finish`` stamps every signer's values onto the untouched original, adds the
   certificate of completion and, if wanted, seals the file.

``RequestStore`` keeps each request (original, copies sent, copies returned, the
finished PDF and a JSON record) in its own folder.
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
import re
import secrets
import shutil
import threading
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path

from . import pdf
from .pdf import Field, SignError

FIELD_NAME = re.compile(r"^rotary_([0-9a-f]+)_(\d+)_([A-Za-z0-9-]+)$")
CHECK_SCALE = 1.5  # ~108 dpi: enough to spot an edited word outside the boxes
INK_SCALE = 3.0  # ~216 dpi for drawn signatures
INK_LEVEL = 48  # how different a pixel must be to count as new ink
ACCENT = (0.85, 0.28, 0.12)
FILL = (1.0, 0.95, 0.7)


def field_name(request_id: str, signer: int, field_id: str) -> str:
    return f"rotary_{request_id}_{signer}_{field_id}"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- the copy a signer gets


def _instructions(width: float, height: float, title: str, sender: str, signer: str) -> bytes:
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer

    styles = getSampleStyleSheet()
    body = styles["BodyText"].clone("body", fontSize=11, leading=15)
    esc = pdf._escape
    story = [
        Paragraph(f"Please sign: {esc(title)}", styles["Title"]),
        Paragraph(f"For {esc(signer)}, from {esc(sender)}", body),
        Spacer(1, 6 * mm),
        Paragraph("How to sign", styles["Heading2"]),
        ListFlowable(
            [
                ListItem(Paragraph("Open this file in <b>Adobe Acrobat Reader</b> (free) or <b>Microsoft Edge</b>. "
                                   "Most PDF readers that can fill in forms work too.", body)),
                ListItem(Paragraph("On the following pages, fill in each <b>yellow box</b>. In a signature box, "
                                   "<b>type your full name</b>, or <b>draw your signature</b> over the box "
                                   "(Adobe Reader: <i>Fill &amp; Sign</i>; Edge: <i>Draw</i>).", body)),
                ListItem(Paragraph(f"<b>Save</b> the file and email it back to {esc(sender)}.", body)),
            ],
            bulletType="1",
        ),
        Spacer(1, 6 * mm),
        Paragraph("Please don't change anything else in the document: the sender's copy of Rotary checks the "
                  "returned file against this one, and other changes will be flagged.", body),
        Spacer(1, 4 * mm),
        Paragraph("By signing and returning this document you agree to sign it electronically, and that your "
                  "electronic signature is as legally binding as one made by hand.", body),
    ]
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=(width, height), leftMargin=22 * mm, rightMargin=22 * mm, title=title).build(story)
    return buf.getvalue()


def _overlay(width: float, height: float, fields: list[dict], request_id: str, signer: int, signer_name: str) -> bytes:
    """One page the size of a document page, carrying this signer's form fields."""
    from reportlab.lib.colors import Color, black
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    accent, fill = Color(*ACCENT), Color(*FILL)
    labels = {"signature": "Sign here", "initials": "Initials", "date": "Date", "name": "Name", "text": "Fill in", "checkbox": ""}
    for f in fields:
        x, w, h = f["x"] * width, f["w"] * width, f["h"] * height
        y = height - (f["y"] + f["h"]) * height
        name = field_name(request_id, signer, f["id"])
        if labels[f["kind"]]:
            c.setFont("Helvetica-Bold", 6.5)
            c.setFillColor(accent)
            c.drawString(x, y + h + 1.5, labels[f["kind"]].upper())
        if f["kind"] == "checkbox":
            c.acroForm.checkbox(
                name=name, tooltip="Tick if you agree", x=x, y=y, size=min(w, h), buttonStyle="check",
                borderColor=accent, fillColor=fill, textColor=black, forceBorder=True, checked=False,
            )
            continue
        tips = {
            "signature": "Type your full name, or draw your signature over this box",
            "initials": "Type or draw your initials",
            "date": "Today's date (left blank, the day you return it is used)",
            "name": "Your name",
            "text": "Fill this in",
        }
        flags = "readOnly" if f["kind"] == "name" else ""
        size = max(min(h * 0.6, 16 if f["kind"] in ("signature", "initials") else 12), 5)
        c.acroForm.textfield(
            name=name, tooltip=tips[f["kind"]], x=x, y=y, width=w, height=h, borderStyle="solid",
            borderColor=accent, fillColor=fill, textColor=black, forceBorder=True,
            fontName="Helvetica", fontSize=size, value=signer_name if f["kind"] == "name" else "", fieldFlags=flags,
        )
    c.showPage()
    c.save()
    return buf.getvalue()


def make_copy(original: bytes, fields: list[dict], *, request_id: str, signer: int, signer_name: str, sender: str, title: str) -> bytes:
    """The fillable PDF for one signer. ``fields`` are that signer's only."""
    from pypdf import PdfReader, PdfWriter, Transformation
    from pypdf.generic import ArrayObject, BooleanObject, DictionaryObject, NameObject, TextStringObject

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(original)))
    form_fields = ArrayObject()
    resources = None
    for index, page in enumerate(writer.pages):
        mine = [f for f in fields if f["page"] == index]
        if not mine:
            continue
        box = page.cropbox
        layer = PdfReader(io.BytesIO(_overlay(float(box.width), float(box.height), mine, request_id, signer, signer_name)))
        page.merge_transformed_page(layer.pages[0], Transformation().translate(float(box.left), float(box.bottom)))
        for annot in page.get("/Annots", []):
            if "/FT" in annot.get_object() and str(annot.get_object().get("/T", "")).startswith("rotary_"):
                form_fields.append(annot)
        if resources is None and "/DR" in layer.trailer["/Root"]["/AcroForm"]:
            resources = layer.trailer["/Root"]["/AcroForm"]["/DR"].clone(writer)
    acroform = DictionaryObject()
    acroform[NameObject("/Fields")] = form_fields
    acroform[NameObject("/NeedAppearances")] = BooleanObject(True)  # let readers redraw boxes as they're filled
    acroform[NameObject("/DA")] = TextStringObject("/Helv 0 Tf 0 g")
    if resources is not None:
        acroform[NameObject("/DR")] = resources
    writer._root_object[NameObject("/AcroForm")] = acroform

    first = writer.pages[0]
    cover = PdfReader(io.BytesIO(_instructions(float(first.cropbox.width), float(first.cropbox.height), title, sender, signer_name)))
    writer.insert_page(cover.pages[0], 0)
    writer.add_metadata({"/Title": f"To sign: {title}", "/RotaryRequest": request_id, "/RotarySigner": str(signer)})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# --------------------------------------------------------------------------- reading a returned copy


@dataclass
class ReturnResult:
    values: dict[str, dict] = dataclass_field(default_factory=dict)  # field id -> {"value", "rect"?}
    issues: list[str] = dataclass_field(default_factory=list)
    form_found: bool = True


def _without_widgets(data: bytes) -> bytes:
    """The file with form fields removed, so a render shows only what's printed or drawn on the page."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, NameObject

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    for page in writer.pages:
        if "/Annots" in page:
            kept = ArrayObject(a for a in page["/Annots"] if a.get_object().get("/Subtype") != "/Widget")
            page[NameObject("/Annots")] = kept
    writer._root_object.pop(NameObject("/AcroForm"), None)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class _Pages:
    """Cached page renders of one PDF."""

    def __init__(self, data: bytes):
        import pypdfium2 as pdfium

        from .._pdfium import LOCK

        self._lock = LOCK
        with LOCK:
            self._pdf = pdfium.PdfDocument(data)
            self.count = len(self._pdf)
        self._cache: dict = {}

    def render(self, index: int, scale: float):
        key = (index, scale)
        if key not in self._cache:
            with self._lock:
                self._cache[key] = self._pdf[index].render(scale=scale, draw_annots=True).to_pil().convert("RGB")
        return self._cache[key]

    def close(self):
        with self._lock:
            self._pdf.close()


def _box(f: dict, kind_margin: bool = True) -> tuple[float, float, float, float]:
    """A field's rectangle (fractions, top-left origin), grown where people tend to overrun it."""
    x, y, w, h = f["x"], f["y"], f["w"], f["h"]
    if kind_margin and f["kind"] in ("signature", "initials"):
        mx, my = w * 0.12, h * 0.6  # handwritten signatures spill out of their box
    elif kind_margin:
        mx, my = w * 0.05, h * 0.25
    else:
        mx = my = 0.0
    return max(x - mx, 0.0), max(y - my, 0.0), min(x + w + mx, 1.0), min(y + h + my, 1.0)


def _ink(sent, returned, box) -> tuple[int, object]:
    """Pixels inside ``box`` that differ between two renders, as a transparent PNG of just the new marks."""
    from PIL import Image, ImageChops

    W, H = returned.size
    crop = (int(box[0] * W), int(box[1] * H), int(box[2] * W), int(box[3] * H))
    before, after = sent.crop(crop), returned.crop(crop)
    mask = ImageChops.difference(before, after).convert("L").point(lambda v: 255 if v > INK_LEVEL else 0)
    count = sum(mask.histogram()[255:])
    image = Image.new("RGBA", after.size, (0, 0, 0, 0))
    image.paste(after, (0, 0), mask)
    return count, image


def _png_data_url(image) -> str:
    buf = io.BytesIO()
    image.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _form_values(returned: bytes) -> tuple[dict[str, tuple[str, int, str]], bool]:
    """Rotary's fields in a returned copy: name -> (request id, signer, value)."""
    from pypdf import PdfReader

    try:
        found = PdfReader(io.BytesIO(returned)).get_fields() or {}
    except Exception:
        found = {}
    values = {}
    for name, data in found.items():
        match = FIELD_NAME.match(str(name))
        if match:
            raw = data.get("/V")
            values[match.group(3)] = (match.group(1), int(match.group(2)), "" if raw is None else str(raw))
    return values, bool(values)


def read_return(sent: bytes, returned: bytes, fields: list[dict], *, request_id: str, signer: int, signer_names: list[str]) -> ReturnResult:
    """Check a signer's returned copy against the one sent, and read what they filled in.

    ``fields`` are this signer's fields; pages are counted in the original document.
    """
    try:
        from pypdf import PdfReader

        PdfReader(io.BytesIO(returned)).pages[0]
    except Exception as exc:
        raise SignError("That file isn't a readable PDF") from exc

    form, form_found = _form_values(returned)
    others = {(req, who) for req, who, _ in form.values()}
    if form_found and all(req != request_id for req, _ in others):
        raise SignError("This file is a copy for a different signing request.")
    wrong = {who for req, who in others if req == request_id and who != signer}
    if wrong and not any(who == signer for _, who in others):
        who = next(iter(wrong))
        name = signer_names[who] if who < len(signer_names) else f"signer {who + 1}"
        raise SignError(f"This is {name}'s copy, not {signer_names[signer]}'s.")

    result = ReturnResult(form_found=form_found)
    sent_pages, back_pages = _Pages(_without_widgets(sent)), _Pages(_without_widgets(returned))
    try:
        doc_pages = sent_pages.count - 1  # the sent copy starts with the instructions page
        if back_pages.count == sent_pages.count:
            offset = 1
        elif back_pages.count == doc_pages:
            offset = 0  # they deleted the instructions page; fine
        else:
            raise SignError(f"This file has {back_pages.count} pages, but the copy sent had {sent_pages.count}. "
                            "It may be a different document, or pages were added or removed.")

        # 1. Anything changed outside this signer's boxes?
        from PIL import ImageChops

        for page in range(doc_pages):
            before, after = sent_pages.render(page + 1, CHECK_SCALE), back_pages.render(page + offset, CHECK_SCALE)
            if before.size != after.size:
                result.issues.append(f"Page {page + 1} has changed size.")
                continue
            diff = ImageChops.difference(before, after).convert("L").point(lambda v: 255 if v > INK_LEVEL else 0)
            W, H = diff.size
            for f in fields:
                if f["page"] == page:
                    x0, y0, x1, y1 = _box(f)
                    diff.paste(0, (int(x0 * W) - 2, int(y0 * H) - 2, int(x1 * W) + 2, int(y1 * H) + 2))
            changed = sum(diff.histogram()[255:])
            if changed > max(40, W * H * 0.0002):
                result.issues.append(f"Page {page + 1} has changes outside the signing fields.")

        # 2. What did they fill in?
        for f in fields:
            typed = form.get(f["id"], (None, None, ""))[2].strip()
            box = _box(f)
            count, image = _ink(sent_pages.render(f["page"] + 1, INK_SCALE), back_pages.render(f["page"] + offset, INK_SCALE), box)
            has_ink = count > max(25, 0.004 * image.width * image.height)
            region = {"x": box[0], "y": box[1], "w": box[2] - box[0], "h": box[3] - box[1]}
            value = None
            if f["kind"] in ("signature", "initials"):
                if has_ink:
                    value = {"value": _png_data_url(image), "rect": region}
                elif typed:
                    value = {"value": typed}
            elif f["kind"] == "checkbox":
                if typed not in ("", "/Off", "Off") or has_ink:
                    value = {"value": "true"}
            elif f["kind"] == "name":
                value = None  # always the signer's name from the request
            elif typed:
                value = {"value": typed}
            elif has_ink and f["kind"] in ("text", "date"):
                value = {"value": _png_data_url(image), "rect": region}
            if value:
                result.values[f["id"]] = value
            elif f.get("required", True) and f["kind"] in ("signature", "initials", "text", "checkbox"):
                what = {"signature": "a signature", "initials": "initials", "text": "a text field", "checkbox": "a required tick box"}[f["kind"]]
                result.issues.append(f"Missing {what} on page {f['page'] + 1}.")
    finally:
        sent_pages.close()
        back_pages.close()
    return result


# --------------------------------------------------------------------------- finishing


def finish(original: bytes, request: dict, *, seal_with=None) -> bytes:
    """The completed document: everyone's values stamped onto the original, plus the certificate."""
    stamps = []
    for f in request["fields"]:
        who = request["signers"][f["signer"]]
        value = who.get("values", {}).get(f["id"])
        if f["kind"] == "name":
            stamps.append(Field("name", f["page"], f["x"], f["y"], f["w"], f["h"], who["name"]))
        elif f["kind"] == "date" and not value:
            day = dt.datetime.strptime(who["returned_at"], "%Y-%m-%d %H:%M:%S").strftime("%d %b %Y")
            stamps.append(Field("date", f["page"], f["x"], f["y"], f["w"], f["h"], day))
        elif value:
            rect = value.get("rect") or f
            kind = "signature" if value["value"].startswith("data:") else f["kind"]
            stamps.append(Field(kind, f["page"], rect["x"], rect["y"], rect["w"], rect["h"], value["value"]))
    stamped = pdf.stamp(original, stamps)

    exceptions = [s for s in request["signers"] if s.get("accepted_reason")]
    note = ("Each signer was emailed their own copy, filled it in, and returned it to the sender, who added it to "
            "Rotary. Rotary compared each returned copy with the one sent")
    note += (" and found no changes outside the signing fields." if not exceptions else
             "; " + "; ".join(f"{s['name']}'s copy differed and the sender accepted it: {s['accepted_reason']}" for s in exceptions) + ".")
    note += " By returning their copy, each signer agreed to sign electronically."
    if seal_with is not None:
        note += " The document is sealed with a digital signature: any later change breaks the seal."
    record = {
        "title": request["title"],
        "envelope_id": request["id"],
        "original_sha256": request["original_sha256"],
        "sender": request.get("sender") or "",
        "created_at": f"{request['created_at']} UTC",
        "completed_at": f"{now()} UTC",
        "ip_heading": "Returned file (SHA-256)",
        "agent_heading": "How",
        "signers": [
            {"name": s["name"], "email": s["email"], "signed_at": f"{s['returned_at']} UTC (copy returned)",
             "ip": (s.get("returned_sha256") or "")[:16] + "…",
             "user_agent": "Signed copy returned by email" + ("; accepted with differences" if s.get("accepted_reason") else "")}
            for s in request["signers"]
        ],
        "events": request.get("events", []),
        "note": note,
    }
    full = pdf.append(stamped, pdf.certificate(record))
    if seal_with is not None:
        full = pdf.seal(full, seal_with, reason=f"Signed by {', '.join(s['name'] for s in request['signers'])} via Rotary"[:200])
    return full


# --------------------------------------------------------------------------- storage


class RequestStore:
    """Signature requests on disk: one folder each, holding the PDFs and request.json."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.RLock()

    def folder(self, request_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12}", request_id or ""):
            raise KeyError(request_id)
        return self.root / request_id

    def create(self, data: bytes, filename: str, sender: str) -> dict:
        prepared = pdf.prepare(data)
        request = {
            "id": secrets.token_hex(6),
            "title": Path(filename).stem[:200] or "Document",
            "filename": filename,
            "sender": sender,
            "status": "draft",
            "created_at": now(),
            "original_sha256": pdf.sha256(data),
            "pages": pdf.page_sizes(prepared),
            "signers": [],
            "fields": [],
            "events": [],
        }
        folder = self.root / request["id"]
        folder.mkdir(parents=True)
        (folder / "original.pdf").write_bytes(prepared)
        self.event(request, sender or "You", f"Created from {filename}")
        self.save(request)
        return request

    def get(self, request_id: str) -> dict:
        path = self.folder(request_id) / "request.json"
        if not path.exists():
            raise KeyError(request_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, request: dict) -> None:
        with self._lock:
            path = self.folder(request["id"]) / "request.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(request, indent=1), encoding="utf-8")
            tmp.replace(path)

    def list(self) -> list[dict]:
        found = []
        if self.root.exists():
            for item in self.root.iterdir():
                try:
                    found.append(self.get(item.name))
                except (KeyError, ValueError, OSError):
                    continue
        return sorted(found, key=lambda r: r["created_at"], reverse=True)

    def delete(self, request_id: str) -> None:
        shutil.rmtree(self.folder(request_id), ignore_errors=True)

    def path(self, request: dict, name: str) -> Path:
        return self.folder(request["id"]) / name

    @staticmethod
    def event(request: dict, actor: str, action: str) -> None:
        request.setdefault("events", []).append({"at": now(), "actor": actor, "action": action, "ip": ""})
