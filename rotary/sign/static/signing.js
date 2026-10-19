// Signer's page: fill in your fields, adopt a signature, finish.
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const data = JSON.parse($("page-data").textContent);
  const initials = (name) => name.split(/\s+/).filter(Boolean).map((w) => w[0].toUpperCase()).join("");
  const AUTO = new Set(["date", "name"]); // filled in by the server

  const viewer = new RSign.Viewer($("doc"), {
    pages: data.pages,
    mode: "fill",
    signerName: data.name,
    onChange: progress,
    onSign: (field) =>
      RSign.adopt({
        kind: field.kind,
        name: field.kind === "initials" ? initials(data.name) : data.name,
        legal: "By selecting Adopt, I agree that this mark is my electronic signature and is as legally binding as my handwritten signature.",
      }),
  });
  viewer.setFields(data.fields);

  const mine = () => viewer.fields.filter((f) => f.mine && !AUTO.has(f.kind));
  const todo = () => mine().filter((f) => f.required && (f.kind === "checkbox" ? f.value !== "true" : !f.value));

  function status(text, kind = "") {
    $("status").textContent = text;
    $("status").className = "status center" + (kind ? " " + kind : "");
  }

  function progress() {
    const required = mine().filter((f) => f.required).length;
    const left = todo().length;
    $("progress").textContent = required ? `${required - left} of ${required} required field${required === 1 ? "" : "s"} done` : "";
    $("next").hidden = left === 0;
  }

  function goTo(field) {
    field._el?.scrollIntoView({ behavior: "smooth", block: "center" });
    field._el?.focus({ preventScroll: true });
  }

  $("next").addEventListener("click", () => {
    const next = todo()[0];
    if (next) goTo(next);
  });

  $("finish").addEventListener("click", async () => {
    const left = todo();
    if (left.length) {
      status(`Please complete ${left.length} more required field${left.length === 1 ? "" : "s"}.`, "bad");
      goTo(left[0]);
      return;
    }
    $("finish").disabled = true;
    status("Signing…");
    const values = {};
    for (const f of viewer.fields.filter((x) => x.mine)) values[f.id] = f.value ?? null;
    try {
      const res = await fetch(data.submitUrl, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ values }) });
      const out = await res.json().catch(() => ({}));
      if (!res.ok) {
        const field = viewer.fields.find((f) => f.id === out.field);
        if (field) goTo(field);
        throw new Error(out.error || `Something went wrong (${res.status})`);
      }
      window.location.href = out.url;
    } catch (err) {
      status(err.message, "bad");
      $("finish").disabled = false;
    }
  });

  progress();
})();
