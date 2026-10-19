// Small behaviours shared by every page (inline scripts are blocked by the CSP).
"use strict";

// <button data-confirm="Are you sure?"> asks before submitting its form.
document.addEventListener("click", (e) => {
  const button = e.target.closest("[data-confirm]");
  if (button && !window.confirm(button.dataset.confirm)) e.preventDefault();
});

// <button data-copy> copies the value of the input just before it.
document.addEventListener("click", async (e) => {
  const button = e.target.closest("[data-copy]");
  if (!button) return;
  const field = button.previousElementSibling;
  field.select();
  try {
    await navigator.clipboard.writeText(field.value);
  } catch {
    document.execCommand("copy");
  }
  const label = button.textContent;
  button.textContent = "Copied";
  setTimeout(() => { button.textContent = label; }, 1500);
});
