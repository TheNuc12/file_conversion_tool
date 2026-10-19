/* Rotary Sign: page viewer with placeable fields, a signature pad and the
 * "adopt your signature" dialog. Shared by the desktop app's Sign tab and the
 * signing server. Plain script, no dependencies; exposes window.RSign.
 *
 * Field positions are fractions of the page (0-1, origin top-left), the same
 * convention rotary/sign/pdf.py stamps with.
 */
(function () {
  "use strict";

  const KINDS = {
    signature: { label: "Signature", w: 0.26, h: 0.055 },
    initials: { label: "Initials", w: 0.09, h: 0.045 },
    date: { label: "Date", w: 0.16, h: 0.028 },
    name: { label: "Name", w: 0.22, h: 0.028 },
    text: { label: "Text", w: 0.26, h: 0.028 },
    checkbox: { label: "Checkbox", w: 0.026, h: 0 }, // h is set to make it square
  };
  const SIGNER_COLORS = ["#d9481f", "#2565c4", "#18804f", "#7a4fd6", "#a87406", "#c43361", "#0b7f8a", "#6b6f78"];
  let nextId = 1;
  const uid = () => `f${Date.now().toString(36)}${(nextId++).toString(36)}`;

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const child of children) node.append(child);
    return node;
  }
  const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
  const today = () => new Date().toLocaleDateString();

  // ------------------------------------------------------------------ viewer

  /**
   * new Viewer(container, {
   *   pages: [{src, width, height}],        page images and their sizes in points
   *   mode: "design" | "self" | "fill",     place fields / place and fill / only fill
   *   signers: [{name}],                    design mode: who each field is for
   *   onSign(field): Promise<dataURL>,      fill a signature or initials field
   *   onChange(fields),
   * })
   */
  class Viewer {
    constructor(container, opts) {
      this.container = container;
      this.opts = Object.assign({ mode: "self", signers: [], onChange() {}, onSign: null }, opts);
      this.fields = [];
      this.tool = null;
      this.selected = null;
      this.layers = [];
      this.render();
    }

    render() {
      this.container.replaceChildren();
      this.container.classList.add("rs-doc", `rs-mode-${this.opts.mode}`);
      this.layers = this.opts.pages.map((page, index) => {
        const layer = el("div", { class: "rs-layer", "data-page": index });
        const box = el(
          "div",
          { class: "rs-page", style: `aspect-ratio: ${page.width} / ${page.height}` },
          el("img", { src: page.src, alt: `Page ${index + 1}`, loading: index < 2 ? "eager" : "lazy", draggable: "false" }),
          layer,
        );
        layer.addEventListener("pointerdown", (e) => this._placeAt(e, index, layer));
        this.container.append(el("div", { class: "rs-page-wrap" }, box, el("div", { class: "rs-page-no" }, `Page ${index + 1} of ${this.opts.pages.length}`)));
        return layer;
      });
      for (const field of this.fields) this._mount(field);
    }

    /** Arm a field type: the next click on a page drops one there. */
    setTool(kind, extra = {}) {
      this.tool = kind ? { kind, extra } : null;
      this.container.classList.toggle("rs-placing", !!kind);
    }

    setFields(fields) {
      this.fields = fields.map((f) => Object.assign({ id: uid(), required: true }, f));
      this.render();
    }

    add(field) {
      const f = Object.assign({ id: uid(), required: true, value: null }, field);
      this.fields.push(f);
      this._mount(f);
      this._changed();
      return f;
    }

    remove(field) {
      this.fields = this.fields.filter((f) => f !== field);
      field._el?.remove();
      if (this.selected === field) this.selected = null;
      this._changed();
    }

    /** Re-draw one field (or all) after its value changed. */
    refresh(field) {
      for (const f of field ? [field] : this.fields) this._paint(f);
    }

    _changed() {
      this.opts.onChange(this.fields);
    }

    _pageAspect(page) {
      const p = this.opts.pages[page];
      return p.width / p.height;
    }

    _placeAt(e, page, layer) {
      if (!this.tool || e.target !== layer) return;
      e.preventDefault();
      const r = layer.getBoundingClientRect();
      const spec = KINDS[this.tool.kind];
      const w = spec.w;
      const h = spec.h || spec.w * this._pageAspect(page);
      const x = clamp((e.clientX - r.left) / r.width - w / 2, 0, 1 - w);
      const y = clamp((e.clientY - r.top) / r.height - h / 2, 0, 1 - h);
      const field = this.add(Object.assign({ kind: this.tool.kind, page, x, y, w, h }, this.tool.extra));
      if (this.opts.mode === "self") this._defaultValue(field);
      this._select(field);
      if (!e.shiftKey) this.setTool(null); // hold Shift to drop several
      this.opts.onPlaced?.(field);
    }

    _defaultValue(field) {
      if (field.kind === "date") field.value = today();
      if (field.kind === "checkbox") field.value = "true";
      if (field.kind === "name") field.value = this.opts.defaultName?.() || "";
      if ((field.kind === "signature" || field.kind === "initials") && this.opts.onSign) this._sign(field);
      this._paint(field);
    }

    _editable() {
      return this.opts.mode === "design" || this.opts.mode === "self";
    }

    _mount(field) {
      const layer = this.layers[field.page];
      if (!layer) return;
      if (this.opts.mode === "fill" && !field.mine && !field.done) return; // other people's empty fields
      const node = el("div", { class: `rs-field rs-${field.kind}`, tabindex: "0", role: "group" });
      field._el = node;
      if (this.opts.mode === "design") {
        node.style.setProperty("--rs-color", SIGNER_COLORS[(field.signer || 0) % SIGNER_COLORS.length]);
      }
      if (this._editable()) {
        node.append(
          el("button", { class: "rs-del", type: "button", title: "Remove", "aria-label": "Remove field", onclick: (e) => { e.stopPropagation(); this.remove(field); } }, "×"),
          el("span", { class: "rs-grip", "aria-hidden": "true" }),
        );
        this._draggable(field, node);
      }
      node.addEventListener("keydown", (e) => this._key(e, field));
      node.addEventListener("focus", () => this._select(field));
      layer.append(node);
      this._place(field);
      this._paint(field);
    }

    _place(field) {
      const s = field._el.style;
      s.left = `${field.x * 100}%`;
      s.top = `${field.y * 100}%`;
      s.width = `${field.w * 100}%`;
      s.height = `${field.h * 100}%`;
    }

    _paint(field) {
      const node = field._el;
      if (!node) return;
      node.querySelector(".rs-content")?.remove();
      const content = el("div", { class: "rs-content" });
      const mode = this.opts.mode;
      const interactive = mode === "self" || (mode === "fill" && field.mine);
      node.classList.toggle("rs-filled", !!field.value);
      node.classList.toggle("rs-missing", this._missing(field));
      node.classList.toggle("rs-readonly", !interactive && mode !== "design");

      if (mode === "design") {
        const who = this.opts.signers[field.signer || 0]?.name || `Signer ${(field.signer || 0) + 1}`;
        const label = `${KINDS[field.kind].label}${this.opts.signers.length > 1 ? ` · ${who}` : ""}`;
        node.title = label;
        // A checkbox is too small for words; a tick in the signer's colour says enough.
        content.append(el("span", { class: "rs-tag" }, field.kind === "checkbox" ? "✓" : label));
      } else if (field.kind === "signature" || field.kind === "initials") {
        if (field.value) content.append(el("img", { src: field.value, alt: KINDS[field.kind].label, draggable: "false" }));
        else content.append(el("span", { class: "rs-tag" }, interactive ? `Click to ${field.kind === "initials" ? "initial" : "sign"}` : KINDS[field.kind].label));
      } else if (field.kind === "checkbox") {
        content.append(el("span", { class: "rs-tick" }, field.value === "true" ? "✓" : ""));
      } else if (interactive && (mode === "self" || field.kind === "text")) {
        const input = el("input", { type: "text", value: field.value || "", placeholder: KINDS[field.kind].label, "aria-label": KINDS[field.kind].label });
        input.addEventListener("input", () => {
          field.value = input.value;
          node.classList.toggle("rs-filled", !!input.value);
          node.classList.toggle("rs-missing", this._missing(field));
          this._changed();
        });
        input.addEventListener("pointerdown", (e) => e.stopPropagation());
        content.append(input);
      } else {
        // Name and date in fill mode are filled in by the server when the signer finishes.
        const auto = field.kind === "date" ? "Date signed" : field.kind === "name" ? this.opts.signerName || "Your name" : "";
        content.append(el("span", { class: field.value ? "rs-text" : "rs-tag" }, field.value || auto || KINDS[field.kind].label));
      }
      // In fill mode a click acts on the field; in self mode the drag handler does it for
      // clicks that didn't move the field, so one gesture never counts twice.
      if (interactive && mode === "fill") content.addEventListener("click", () => this._activate(field));
      node.setAttribute("aria-label", `${KINDS[field.kind].label} field${field.value ? ", filled" : ""}`);
      node.prepend(content);
    }

    /** A required field the signer still has to fill (fill mode only). */
    _missing(field) {
      if (this.opts.mode !== "fill" || !field.mine || !field.required) return false;
      if (field.kind === "date" || field.kind === "name") return false;
      return field.kind === "checkbox" ? field.value !== "true" : !field.value;
    }

    _activate(field) {
      const interactive = this.opts.mode === "self" || (this.opts.mode === "fill" && field.mine);
      if (!interactive) return;
      if (field.kind === "signature" || field.kind === "initials") return this._sign(field);
      if (field.kind === "checkbox") {
        field.value = field.value === "true" ? "false" : "true";
        this._paint(field);
        this._changed();
        return;
      }
      field._el?.querySelector(".rs-content input")?.focus();
    }

    async _sign(field) {
      if (!this.opts.onSign) return;
      const image = await this.opts.onSign(field);
      if (!image) return;
      // Like paper: once you've adopted a signature it goes on every signature box of yours.
      for (const f of this.fields) {
        if (f.kind === field.kind && (this.opts.mode !== "fill" || f.mine)) {
          f.value = image;
          this._paint(f);
        }
      }
      this._changed();
    }

    _select(field) {
      if (this.selected?._el) this.selected._el.classList.remove("rs-selected");
      this.selected = field;
      field._el?.classList.add("rs-selected");
    }

    _key(e, field) {
      if (e.target.tagName === "INPUT") return;
      if ((e.key === "Enter" || e.key === " ") && e.target === field._el) {
        e.preventDefault();
        this._activate(field);
        return;
      }
      if (!this._editable()) return;
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        this.remove(field);
        return;
      }
      const step = e.shiftKey ? 0.02 : 0.004;
      const moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
      if (moves[e.key]) {
        e.preventDefault();
        field.x = clamp(field.x + moves[e.key][0], 0, 1 - field.w);
        field.y = clamp(field.y + moves[e.key][1], 0, 1 - field.h);
        this._place(field);
        this._changed();
      }
    }

    _draggable(field, node) {
      let start = null;
      node.addEventListener("pointerdown", (e) => {
        if (e.target.closest(".rs-del") || e.target.tagName === "INPUT") return;
        // Signature boxes in self mode open the pad on click; only start dragging on movement.
        const layer = this.layers[field.page].getBoundingClientRect();
        start = { x: e.clientX, y: e.clientY, fx: field.x, fy: field.y, fw: field.w, fh: field.h, layer, resize: e.target.classList.contains("rs-grip"), moved: false };
        node.setPointerCapture(e.pointerId);
        this._select(field);
        e.preventDefault();
        node.focus({ preventScroll: true });
      });
      node.addEventListener("pointermove", (e) => {
        if (!start) return;
        const dx = (e.clientX - start.x) / start.layer.width;
        const dy = (e.clientY - start.y) / start.layer.height;
        if (!start.moved && Math.abs(e.clientX - start.x) + Math.abs(e.clientY - start.y) < 4) return;
        start.moved = true;
        node.classList.add("rs-dragging");
        if (start.resize) {
          field.w = clamp(start.fw + dx, 0.015, 1 - field.x);
          field.h = field.kind === "checkbox" ? field.w * this._pageAspect(field.page) : clamp(start.fh + dy, 0.012, 1 - field.y);
        } else {
          field.x = clamp(start.fx + dx, 0, 1 - field.w);
          field.y = clamp(start.fy + dy, 0, 1 - field.h);
        }
        this._place(field);
      });
      const end = (e) => {
        if (!start) return;
        const moved = start.moved;
        start = null;
        node.classList.remove("rs-dragging");
        if (moved) this._changed();
        else if (e.type === "pointerup") this._activate(field); // a click, not a drag
      };
      node.addEventListener("pointerup", end);
      node.addEventListener("pointercancel", end);
    }
  }

  // ------------------------------------------------------------------ signature pad

  class SignaturePad {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.strokes = [];
      this._resize();
      let current = null;
      canvas.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        canvas.setPointerCapture(e.pointerId);
        current = [this._pt(e)];
        this.strokes.push(current);
        this._draw();
      });
      canvas.addEventListener("pointermove", (e) => {
        if (!current) return;
        for (const ev of e.getCoalescedEvents ? e.getCoalescedEvents() : [e]) current.push(this._pt(ev));
        this._draw();
      });
      const stop = () => { current = null; this.onchange?.(); };
      canvas.addEventListener("pointerup", stop);
      canvas.addEventListener("pointercancel", stop);
    }

    _resize() {
      const ratio = window.devicePixelRatio || 1;
      const r = this.canvas.getBoundingClientRect();
      this.canvas.width = Math.round((r.width || 500) * ratio);
      this.canvas.height = Math.round((r.height || 160) * ratio);
      this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      this._draw();
    }

    _pt(e) {
      const r = this.canvas.getBoundingClientRect();
      return [e.clientX - r.left, e.clientY - r.top, e.pressure || 0.5];
    }

    _draw() {
      const { ctx, canvas } = this;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.strokeStyle = "#14244f";
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      for (const s of this.strokes) {
        ctx.lineWidth = 2.6;
        ctx.beginPath();
        ctx.moveTo(s[0][0], s[0][1]);
        if (s.length === 1) ctx.lineTo(s[0][0] + 0.1, s[0][1] + 0.1);
        for (let i = 1; i < s.length - 1; i++) {
          const mx = (s[i][0] + s[i + 1][0]) / 2, my = (s[i][1] + s[i + 1][1]) / 2;
          ctx.quadraticCurveTo(s[i][0], s[i][1], mx, my);
        }
        if (s.length > 1) ctx.lineTo(s[s.length - 1][0], s[s.length - 1][1]);
        ctx.stroke();
      }
    }

    clear() { this.strokes = []; this._draw(); this.onchange?.(); }
    isEmpty() { return this.strokes.length === 0; }
    toDataURL() { return trimmed(this.canvas); }
  }

  /** Copy just the inked part of a canvas, with a little margin, as a transparent PNG. */
  function trimmed(canvas) {
    const { width, height } = canvas;
    const data = canvas.getContext("2d").getImageData(0, 0, width, height).data;
    let x0 = width, y0 = height, x1 = -1, y1 = -1;
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        if (data[(y * width + x) * 4 + 3] > 8) {
          if (x < x0) x0 = x; if (x > x1) x1 = x;
          if (y < y0) y0 = y; if (y > y1) y1 = y;
        }
      }
    }
    if (x1 < 0) return null;
    const pad = 6;
    x0 = Math.max(x0 - pad, 0); y0 = Math.max(y0 - pad, 0);
    x1 = Math.min(x1 + pad, width - 1); y1 = Math.min(y1 + pad, height - 1);
    const out = document.createElement("canvas");
    out.width = x1 - x0 + 1;
    out.height = y1 - y0 + 1;
    out.getContext("2d").drawImage(canvas, x0, y0, out.width, out.height, 0, 0, out.width, out.height);
    return out.toDataURL("image/png");
  }

  const SCRIPT_FONTS = ["Segoe Script", "Brush Script MT", "Lucida Handwriting", "Apple Chancery", "URW Chancery L", "cursive"];

  function typedSignature(text, font = SCRIPT_FONTS.map((f) => (f === "cursive" ? f : `"${f}"`)).join(", ")) {
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    const size = 96;
    ctx.font = `${size}px ${font}`;
    canvas.width = Math.ceil(ctx.measureText(text).width + size);
    canvas.height = Math.ceil(size * 1.8);
    ctx.font = `${size}px ${font}`;
    ctx.fillStyle = "#14244f";
    ctx.textBaseline = "middle";
    ctx.fillText(text, size / 2, canvas.height / 2);
    return trimmed(canvas);
  }

  // ------------------------------------------------------------------ adopt dialog

  /**
   * Ask for a signature (or initials). Resolves to a PNG data URL, or null if cancelled.
   * opts: {kind: "signature"|"initials", name: suggested text, legal: optional consent sentence}
   */
  function adopt(opts = {}) {
    const kind = opts.kind || "signature";
    const noun = kind === "initials" ? "initials" : "signature";
    return new Promise((resolve) => {
      const typed = el("input", { type: "text", class: "rs-typed-input", value: opts.name || "", "aria-label": `Type your ${noun}`, maxlength: "60" });
      const preview = el("div", { class: "rs-typed-preview", "aria-hidden": "true" });
      const canvas = el("canvas", { class: "rs-pad", "aria-label": `Draw your ${noun}` });
      const drawTab = el("button", { type: "button", class: "rs-tab", "aria-selected": "true" }, "Draw");
      const typeTab = el("button", { type: "button", class: "rs-tab", "aria-selected": "false" }, "Type");
      const drawPane = el("div", { class: "rs-pane" }, canvas, el("button", { type: "button", class: "rs-link", onclick: () => pad.clear() }, "Clear"));
      const typePane = el("div", { class: "rs-pane", hidden: "" }, typed, preview);
      const ok = el("button", { type: "button", class: "rs-primary" }, `Adopt ${noun}`);
      const cancel = el("button", { type: "button", class: "rs-secondary" }, "Cancel");
      const dialog = el(
        "dialog",
        { class: "rs-dialog", "aria-label": `Adopt your ${noun}` },
        el("h3", {}, kind === "initials" ? "Your initials" : "Your signature"),
        el("div", { class: "rs-tabs", role: "tablist" }, drawTab, typeTab),
        drawPane,
        typePane,
        opts.legal ? el("p", { class: "rs-legal" }, opts.legal) : "",
        el("div", { class: "rs-actions" }, cancel, ok),
      );
      document.body.append(dialog);
      dialog.showModal();
      const pad = new SignaturePad(canvas);
      let tab = "draw";
      const show = (which) => {
        tab = which;
        drawTab.setAttribute("aria-selected", String(which === "draw"));
        typeTab.setAttribute("aria-selected", String(which === "type"));
        drawPane.hidden = which !== "draw";
        typePane.hidden = which !== "type";
        if (which === "type") typed.focus();
        update();
      };
      const update = () => {
        preview.textContent = typed.value;
        ok.disabled = tab === "draw" ? pad.isEmpty() : !typed.value.trim();
      };
      pad.onchange = update;
      typed.addEventListener("input", update);
      drawTab.addEventListener("click", () => show("draw"));
      typeTab.addEventListener("click", () => show("type"));
      const finish = (value) => { dialog.close(); dialog.remove(); resolve(value); };
      cancel.addEventListener("click", () => finish(null));
      dialog.addEventListener("cancel", (e) => { e.preventDefault(); finish(null); });
      ok.addEventListener("click", () => finish(tab === "draw" ? pad.toDataURL() : typedSignature(typed.value.trim())));
      show(opts.name ? "type" : "draw");
    });
  }

  window.RSign = { Viewer, SignaturePad, typedSignature, adopt, KINDS, SIGNER_COLORS };
})();
