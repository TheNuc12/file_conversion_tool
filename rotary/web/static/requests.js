// "Get signatures" tab: send a PDF to other people to sign, without a server.
// Drafts are edited with the shared RSign viewer; everything else is buttons on
// the left. Server side: rotary/web/requests_api.py.
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const side = $("req-side");
  const main = $("req-main");
  const EMAIL = /^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$/;
  const STATUS = { draft: "Draft", out: "Out for signature", completed: "Done" };
  const SIGNER_STATUS = { waiting: "Waiting", signed: "Signed", check: "Needs a look" };
  let current = null; // the open request
  let viewer = null;
  let saveTimer = null;
  let fieldsFor = 0;
  let caps = { outlook: false, can_seal: false };

  function h(tag, attrs = {}, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === false || v == null) continue;
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) node.append(kid);
    return node;
  }

  async function api(path, { method = "GET", body, json } = {}) {
    const headers = { "X-Rotary": "1" };
    if (json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(json); }
    const res = await fetch(path, { method, headers: method === "GET" ? {} : headers, body });
    const isJson = (res.headers.get("Content-Type") || "").includes("json");
    const data = isJson ? await res.json() : await res.blob();
    if (!res.ok) throw new Error((isJson && data.error) || `Something went wrong (${res.status})`);
    return { data, res };
  }

  function say(text, kind = "") {
    const node = $("req-status");
    if (!node) return;
    node.textContent = text;
    node.className = "sign-status" + (kind ? " " + kind : "");
  }

  async function download(path) {
    const { data, res } = await api(path);
    const name = decodeURIComponent(res.headers.get("X-Rotary-Filename") || "document.pdf");
    const a = h("a", { href: URL.createObjectURL(data), download: name });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
    return name;
  }

  const myName = () => ($("sign-name")?.value || "").trim();

  // ---------------------------------------------------------------- home: the list
  async function showHome() {
    current = null; viewer = null;
    const { data } = await api("/api/requests");
    caps = { outlook: data.outlook, can_seal: data.can_seal };
    const nameInput = h("input", { type: "text", id: "req-me", value: myName(), placeholder: "So signers know who it's from", autocomplete: "name" });
    nameInput.addEventListener("change", () => {
      if ($("sign-name")) { $("sign-name").value = nameInput.value; $("sign-name").dispatchEvent(new Event("change")); }
    });
    const picker = h("input", { type: "file", id: "req-picker", accept: "application/pdf,.pdf" });
    picker.addEventListener("change", () => { if (picker.files[0]) createRequest(picker.files[0]); picker.value = ""; });
    const list = h("ul", { class: "req-list" },
      data.requests.length ? data.requests.map((r) => h("li", {},
        h("button", { type: "button", class: "req-item", onclick: () => open(r.id) },
          h("strong", {}, r.title),
          h("span", { class: `req-chip ${r.status}` }, r.status === "out" ? `${r.signed} of ${r.signers} signed` : STATUS[r.status]),
        ))) : h("li", { class: "muted-note" }, "No requests yet."));
    side.replaceChildren(
      h("h2", {}, "New request"),
      h("label", { class: "field" }, "Your name", nameInput),
      h("label", { class: "drop compact", id: "req-drop" }, picker, h("strong", {}, "Open the PDF to send"), h("span", {}, "or drop it here")),
      h("p", { class: "sign-status", id: "req-status", role: "status" }),
      h("h2", {}, "Your requests"),
      list,
    );
    main.replaceChildren(h("div", { class: "sign-empty req-intro" },
      h("div", {},
        h("h3", {}, "Get signatures without Adobe Sign"),
        h("ol", {},
          h("li", {}, "Open a PDF, add the people who need to sign, and place their fields."),
          h("li", {}, "Rotary makes each person a fillable copy. Email it to them (with Outlook it's one click)."),
          h("li", {}, "They sign it in Adobe Reader or Edge and email it back. Add each returned copy here: Rotary checks nothing else was changed."),
          h("li", {}, "When everyone has signed, Rotary puts all the signatures on your original and adds a certificate of who signed when."),
        ),
      )));
  }

  async function createRequest(file) {
    if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") return say("That isn't a PDF. Convert it on the Convert tab first.", "bad");
    say("Opening…");
    try {
      const sender = ($("req-me")?.value || myName()).trim();
      const { data } = await api(`/api/requests?name=${encodeURIComponent(file.name)}&sender=${encodeURIComponent(sender)}`, { method: "POST", body: file });
      render(data);
    } catch (err) { say(err.message, "bad"); }
  }

  async function open(id) {
    try { render((await api(`/api/requests/${id}`)).data); } catch (err) { say(err.message, "bad"); }
  }

  function back() { return h("button", { type: "button", class: "linkish", onclick: () => showHome() }, "← All requests"); }

  function render(request) {
    current = request;
    caps = { outlook: request.outlook, can_seal: request.can_seal };
    if (request.status === "draft") return renderDraft(request);
    return renderSent(request);
  }

  // ---------------------------------------------------------------- draft: signers and fields
  function renderDraft(r) {
    const title = h("input", { type: "text", value: r.title, maxlength: "200", "aria-label": "Title" });
    title.addEventListener("input", () => { r.title = title.value; queueSave(); });
    const newName = h("input", { type: "text", placeholder: "Name", "aria-label": "Signer name", autocomplete: "off" });
    const newEmail = h("input", { type: "email", placeholder: "Email", "aria-label": "Signer email", autocomplete: "off" });
    const add = () => {
      const name = newName.value.trim(), email = newEmail.value.trim();
      if (!name || (email && !EMAIL.test(email))) return say("Enter the signer's name, and a valid email address if you have one.", "bad");
      r.signers.push({ name, email });
      fieldsFor = r.signers.length - 1;
      newName.value = newEmail.value = "";
      say("");
      drawSigners(); queueSave(); newName.focus();
    };
    newEmail.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
    const signers = h("ol", { class: "signers", id: "req-signers" });
    const select = h("select", { id: "req-for", "aria-label": "Fields for" });
    select.addEventListener("change", () => { fieldsFor = Number(select.value); });
    const tools = h("div", { class: "tools" }, ["signature", "initials", "date", "name", "text", "checkbox"].map((kind) =>
      h("button", { type: "button", class: "tool", "data-kind": kind, onclick: (e) => arm(e.currentTarget) }, RSign.KINDS[kind].label)));

    function drawSigners() {
      signers.replaceChildren(...r.signers.map((s, i) => h("li", {},
        h("span", { class: "dot", style: `background:${RSign.SIGNER_COLORS[i % RSign.SIGNER_COLORS.length]}` }),
        h("span", { class: "who" }, h("strong", {}, `${i + 1}. ${s.name}`), h("span", {}, s.email || "no email")),
        h("button", { type: "button", class: "x", "aria-label": `Remove ${s.name}`, onclick: () => removeSigner(i) }, "×"),
      )));
      select.replaceChildren(...r.signers.map((s, i) => h("option", { value: String(i) }, `${i + 1}. ${s.name}`)));
      fieldsFor = Math.min(fieldsFor, Math.max(r.signers.length - 1, 0));
      select.value = String(fieldsFor);
      tools.querySelectorAll(".tool").forEach((b) => { b.disabled = !r.signers.length; });
      if (viewer) { viewer.opts.signers = r.signers; viewer.render(); }
    }
    function removeSigner(i) {
      const theirs = viewer.fields.filter((f) => f.signer === i);
      if (theirs.length && !confirm(`Remove ${r.signers[i].name} and their ${theirs.length} field(s)?`)) return;
      theirs.forEach((f) => viewer.remove(f));
      viewer.fields.forEach((f) => { if (f.signer > i) f.signer -= 1; });
      r.signers.splice(i, 1);
      drawSigners(); queueSave();
    }
    function arm(button) {
      const on = button.getAttribute("aria-pressed") !== "true";
      tools.querySelectorAll(".tool").forEach((b) => b.setAttribute("aria-pressed", "false"));
      button.setAttribute("aria-pressed", String(on));
      viewer.setTool(on ? button.dataset.kind : null, { signer: fieldsFor });
    }

    side.replaceChildren(
      back(),
      h("h2", {}, "1 · Title"), title,
      h("h2", {}, "2 · Who signs"), signers,
      h("div", { class: "add-signer" }, newName, newEmail, h("button", { type: "button", class: "tool", onclick: add }, "Add signer")),
      h("h2", {}, "3 · Their fields"),
      h("label", { class: "field" }, "Fields for", select),
      tools,
      h("p", { class: "hint", style: "text-align:left;margin:0" }, "Pick a field, then click the page. Name and date fill themselves in."),
      h("button", { type: "button", class: "go", id: "req-send", onclick: send }, "Create signing copies"),
      h("p", { class: "sign-status", id: "req-status", role: "status" }),
      h("button", { type: "button", class: "linkish danger", onclick: remove }, "Delete this request"),
    );
    const doc = h("div", { class: "sign-doc" });
    main.replaceChildren(doc);
    viewer = new RSign.Viewer(doc, {
      pages: r.pages, mode: "design", signers: r.signers,
      onChange: queueSave,
      onPlaced: () => tools.querySelectorAll(".tool").forEach((b) => b.setAttribute("aria-pressed", "false")),
    });
    viewer.setFields(r.fields);
    drawSigners();
  }

  function queueSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => saveDraft().catch((err) => say(err.message, "bad")), 800);
  }

  async function saveDraft() {
    clearTimeout(saveTimer);
    if (!current || current.status !== "draft") return;
    const fields = viewer.fields.map(({ _el, value, ...f }) => f);
    await api(`/api/requests/${current.id}/draft`, { method: "POST", json: { title: current.title, signers: current.signers, fields } });
  }

  async function send() {
    const r = current;
    if (!r.signers.length) return say("Add at least one signer.", "bad");
    const unsigned = r.signers.filter((s, i) => !viewer.fields.some((f) => f.signer === i && f.kind === "signature"));
    if (unsigned.length && !confirm(`${unsigned.map((s) => s.name).join(", ")} won't be asked for a signature. Continue?`)) return;
    $("req-send").disabled = true;
    say("Making the copies…");
    try {
      await saveDraft();
      render((await api(`/api/requests/${r.id}/send`, { method: "POST" })).data);
    } catch (err) { say(err.message, "bad"); $("req-send").disabled = false; }
  }

  async function remove() {
    if (!confirm("Delete this request and its files?")) return;
    await api(`/api/requests/${current.id}/delete`, { method: "POST" });
    showHome();
  }

  // ---------------------------------------------------------------- sent: collect copies, finish
  function renderSent(r) {
    const done = r.status === "completed";
    const cards = r.signers.map((s, i) => signerCard(r, s, i, done));
    const allSigned = r.signers.every((s) => s.status === "signed");
    const seal = h("input", { type: "checkbox", id: "req-seal", checked: caps.can_seal });
    const finishBlock = done ? [
      h("h2", {}, "Signed copy"),
      h("button", { type: "button", class: "go", onclick: () => download(`/api/requests/${r.id}/signed`).then((n) => say(`Saved ${n} to your Downloads.`, "ok")).catch((e) => say(e.message, "bad")) }, "Download signed PDF"),
      h("div", { class: "row-actions" },
        caps.outlook
          ? h("button", { type: "button", class: "tool", onclick: () => act(`/api/requests/${r.id}/email-signed`, "Opened an Outlook email with the signed copy attached.") }, "Email it to everyone (Outlook)")
          : h("a", { class: "tool", href: mailto(r.finished_mail) }, "Email everyone"),
        h("button", { type: "button", class: "tool", onclick: () => act(`/api/requests/${r.id}/reveal/signed`, "") }, "Show file"),
      ),
    ] : [
      h("button", { type: "button", class: "go", disabled: !allSigned, onclick: finish }, allSigned ? "Finish: put everyone's signatures on it" : "Waiting for everyone to sign"),
      caps.can_seal ? h("label", { class: "check" }, seal, "Add a tamper-evident seal (Adobe Reader may show it as “validity unknown”)") : null,
    ];
    side.replaceChildren(
      back(),
      h("h2", {}, STATUS[r.status]),
      h("p", { class: "req-title" }, r.title),
      ...cards,
      ...finishBlock,
      h("p", { class: "sign-status", id: "req-status", role: "status" }),
      h("button", { type: "button", class: "linkish danger", onclick: remove }, "Delete this request"),
    );
    const doc = h("div", { class: "sign-doc" });
    main.replaceChildren(doc);
    const preview = new RSign.Viewer(doc, { pages: r.pages, mode: "fill" });
    preview.setFields([]);
  }

  function mailto(mail) {
    return `mailto:${encodeURIComponent(mail.to || "").replace(/%40/g, "@").replace(/%3B/gi, ";")}?subject=${encodeURIComponent(mail.subject)}&body=${encodeURIComponent(mail.body)}`;
  }

  function signerCard(r, s, i, done) {
    const upload = h("input", { type: "file", accept: "application/pdf,.pdf", class: "visually-hidden", id: `req-up-${i}` });
    upload.addEventListener("change", async () => {
      const file = upload.files[0]; upload.value = "";
      if (!file) return;
      say(`Checking ${s.name}'s copy…`);
      try {
        const { data } = await api(`/api/requests/${r.id}/return/${i}`, { method: "POST", body: file });
        render(data);
        const me = data.signers[i];
        say(me.status === "signed" ? `${s.name}'s copy looks good.` : `${s.name}'s copy needs a look.`, me.status === "signed" ? "ok" : "bad");
      } catch (err) { say(err.message, "bad"); }
    });
    const status = h("span", { class: `req-chip ${s.status}` }, SIGNER_STATUS[s.status] || s.status);
    const actions = done ? [] : s.status === "signed" ? [
      h("label", { class: "linkish", for: `req-up-${i}` }, "Replace their signed copy", upload),
    ] : [
      h("div", { class: "row-actions" },
        caps.outlook && s.email
          ? h("button", { type: "button", class: "tool", onclick: () => act(`/api/requests/${r.id}/email/${i}`, `Opened an Outlook email to ${s.name} with their copy attached. Press Send in Outlook.`) }, "Email (Outlook)")
          : (s.email ? h("a", { class: "tool", href: mailto(s.mail), title: "Opens your email app. Attach the copy you download." }, "Email") : null),
        h("button", { type: "button", class: "tool", onclick: () => download(`/api/requests/${r.id}/copy/${i}`).then((n) => say(`Saved ${n} to your Downloads. Attach it to your email to ${s.name}.`, "ok")).catch((e) => say(e.message, "bad")) }, "Download copy"),
        h("button", { type: "button", class: "tool", onclick: () => act(`/api/requests/${r.id}/reveal/${i}`, "") }, "Show file"),
      ),
      h("label", { class: "go small-go", for: `req-up-${i}` }, s.returned_at ? "Replace their signed copy" : "Add their signed copy", upload),
    ];
    const issues = (s.issues || []).length && s.status === "check" ? h("div", { class: "req-issues" },
      h("ul", {}, s.issues.map((t) => h("li", {}, t))),
      h("button", { type: "button", class: "linkish", onclick: () => accept(r, i) }, "Accept anyway"),
    ) : null;
    const accepted = s.accepted_reason ? h("p", { class: "muted-note" }, `Accepted: ${s.accepted_reason}`) : null;
    return h("div", { class: "req-card" },
      h("div", { class: "req-card-head" }, h("span", { class: "who" }, h("strong", {}, s.name), h("span", {}, s.email || "no email")), status),
      issues, accepted, ...actions);
  }

  async function act(path, message) {
    try { await api(path, { method: "POST" }); if (message) say(message, "ok"); } catch (err) { say(err.message, "bad"); }
  }

  async function accept(r, i) {
    const reason = prompt(`Why are you accepting ${r.signers[i].name}'s copy anyway? (This goes on the certificate.)`);
    if (!reason || !reason.trim()) return;
    try { render((await api(`/api/requests/${r.id}/accept/${i}`, { method: "POST", json: { reason } })).data); } catch (err) { say(err.message, "bad"); }
  }

  async function finish() {
    say("Putting it together…");
    try {
      const sealed = $("req-seal")?.checked || false;
      render((await api(`/api/requests/${current.id}/finish`, { method: "POST", json: { seal: sealed } })).data);
      const name = await download(`/api/requests/${current.id}/signed`);
      say(`Done. Saved ${name} to your Downloads.`, "ok");
    } catch (err) { say(err.message, "bad"); }
  }

  // ---------------------------------------------------------------- wiring
  window.RotaryRequests = {
    show: () => (current ? open(current.id) : showHome()).catch((err) => say(err.message, "bad")),
    drop: (file) => (current && current.status === "out" ? null : createRequest(file)),
  };
})();
