// Sender's editor: add signers, place their fields, save and send.
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const data = JSON.parse($("page-data").textContent);
  const csrf = $("csrf").value;
  const EMAIL = /^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$/;
  const state = { recipients: data.recipients.slice(), current: 0, dirty: false, sent: false };

  $("title").value = data.title;
  $("message").value = data.message;
  $("sequential").checked = data.sequential;

  const viewer = new RSign.Viewer($("doc"), {
    pages: data.pages,
    mode: "design",
    signers: state.recipients,
    onChange: changed,
    onPlaced: () => document.querySelectorAll(".tool").forEach((b) => b.setAttribute("aria-pressed", "false")),
  });
  viewer.setFields(data.fields);

  function status(text, kind = "") {
    $("status").textContent = text;
    $("status").className = "status" + (kind ? " " + kind : "");
  }

  // ---------------------------------------------------------------- signers
  function renderSigners() {
    const list = $("signers");
    list.replaceChildren();
    state.recipients.forEach((r, i) => {
      const li = document.createElement("li");
      const dot = document.createElement("span");
      dot.className = "dot";
      dot.style.background = RSign.SIGNER_COLORS[i % RSign.SIGNER_COLORS.length];
      const who = document.createElement("span");
      who.className = "who";
      who.append(Object.assign(document.createElement("strong"), { textContent: `${i + 1}. ${r.name}` }), Object.assign(document.createElement("span"), { textContent: r.email }));
      const ctl = document.createElement("span");
      ctl.className = "ctl";
      const button = (label, title, fn, disabled) => {
        const b = Object.assign(document.createElement("button"), { type: "button", textContent: label, title, disabled });
        b.setAttribute("aria-label", `${title}: ${r.name}`);
        b.addEventListener("click", fn);
        ctl.append(b);
      };
      button("↑", "Move up", () => move(i, -1), i === 0);
      button("↓", "Move down", () => move(i, 1), i === state.recipients.length - 1);
      button("×", "Remove", () => removeSigner(i), false);
      li.append(dot, who, ctl);
      list.append(li);
    });
    const select = $("field-signer");
    select.replaceChildren(...state.recipients.map((r, i) => new Option(`${i + 1}. ${r.name}`, String(i))));
    state.current = Math.min(state.current, Math.max(state.recipients.length - 1, 0));
    select.value = String(state.current);
    select.disabled = !state.recipients.length;
    document.querySelectorAll(".tool").forEach((b) => { b.disabled = !state.recipients.length; });
    viewer.opts.signers = state.recipients;
    viewer.render();
  }

  function addSigner(name, email) {
    name = name.trim();
    email = email.trim();
    if (!name || !EMAIL.test(email)) {
      status("Enter the signer's name and a valid email address.", "bad");
      return false;
    }
    state.recipients.push({ name, email });
    state.current = state.recipients.length - 1;
    status("");
    renderSigners();
    changed();
    return true;
  }

  function removeSigner(i) {
    const theirs = viewer.fields.filter((f) => f.signer === i);
    if (theirs.length && !window.confirm(`Remove ${state.recipients[i].name} and their ${theirs.length} field(s)?`)) return;
    theirs.forEach((f) => viewer.remove(f));
    viewer.fields.forEach((f) => { if (f.signer > i) f.signer -= 1; });
    state.recipients.splice(i, 1);
    renderSigners();
    changed();
  }

  function move(i, delta) {
    const j = i + delta;
    [state.recipients[i], state.recipients[j]] = [state.recipients[j], state.recipients[i]];
    viewer.fields.forEach((f) => {
      if (f.signer === i) f.signer = j;
      else if (f.signer === j) f.signer = i;
    });
    renderSigners();
    changed();
  }

  $("add-signer").addEventListener("click", () => {
    if (addSigner($("new-name").value, $("new-email").value)) {
      $("new-name").value = "";
      $("new-email").value = "";
      $("new-name").focus();
    }
  });
  $("new-email").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("add-signer").click(); } });
  $("add-me").addEventListener("click", () => addSigner(data.me.name, data.me.email));
  $("field-signer").addEventListener("change", () => { state.current = Number($("field-signer").value); });
  document.querySelectorAll(".tool").forEach((button) => button.addEventListener("click", () => {
    const on = button.getAttribute("aria-pressed") !== "true";
    document.querySelectorAll(".tool").forEach((b) => b.setAttribute("aria-pressed", "false"));
    button.setAttribute("aria-pressed", String(on));
    viewer.setTool(on ? button.dataset.kind : null, { signer: state.current });
  }));
  ["title", "message", "sequential"].forEach((id) => $(id).addEventListener("input", changed));

  // ---------------------------------------------------------------- saving & sending
  let saveTimer = null;
  function changed() {
    state.dirty = true;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => save().catch(() => {}), 2500);
  }

  async function post(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: body === undefined ? "{}" : JSON.stringify(body),
    });
    const out = await res.json().catch(() => ({ error: `Server error (${res.status})` }));
    if (!res.ok) throw new Error(out.error || `Server error (${res.status})`);
    return out;
  }

  async function save() {
    clearTimeout(saveTimer);
    await post(data.saveUrl, {
      title: $("title").value,
      message: $("message").value,
      sequential: $("sequential").checked,
      recipients: state.recipients,
      fields: viewer.fields.map(({ _el, value, ...f }) => f),
    });
    state.dirty = false;
  }

  $("save").addEventListener("click", async () => {
    try {
      await save();
      status("Draft saved.", "ok");
    } catch (err) {
      status(err.message, "bad");
    }
  });

  $("send").addEventListener("click", async () => {
    if (!state.recipients.length) return status("Add at least one signer.", "bad");
    const missing = state.recipients.filter((r, i) => !viewer.fields.some((f) => f.signer === i));
    if (missing.length) return status(`${missing.map((r) => r.name).join(", ")} ${missing.length === 1 ? "has" : "have"} no fields yet. Pick them under "Fields for" and add a signature.`, "bad");
    const unsigned = state.recipients.filter((r, i) => !viewer.fields.some((f) => f.signer === i && f.kind === "signature"));
    if (unsigned.length && !window.confirm(`${unsigned.map((r) => r.name).join(", ")} won't be asked for a signature. Send anyway?`)) return;
    $("send").disabled = true;
    status("Sending…");
    try {
      await save();
      const result = await post(data.sendUrl);
      state.sent = true;
      const links = Object.entries(result.links || {});
      if (!links.length) {
        window.location.href = result.url;
        return;
      }
      // No email server: hand the links to the sender to pass on.
      const list = $("link-list");
      list.replaceChildren();
      for (const [email, link] of links) {
        const row = document.createElement("div");
        row.className = "link-row";
        const input = Object.assign(document.createElement("input"), { readOnly: true, value: link, className: "copy-field" });
        input.setAttribute("aria-label", `Signing link for ${email}`);
        const copy = Object.assign(document.createElement("button"), { type: "button", textContent: "Copy", className: "btn small ghost" });
        copy.dataset.copy = "";
        row.append(Object.assign(document.createElement("strong"), { textContent: email }), input, copy);
        list.append(row);
      }
      $("links-done").href = result.url;
      $("links").hidden = false;
      status($("sequential").checked && state.recipients.length > 1 ? "Sent. Later signers' links appear on the envelope page when it's their turn." : "Sent.", "ok");
    } catch (err) {
      status(err.message, "bad");
      $("send").disabled = false;
    }
  });

  window.addEventListener("beforeunload", (e) => {
    if (state.dirty && !state.sent) {
      save().catch(() => {});
      e.preventDefault();
    }
  });

  renderSigners();
})();
