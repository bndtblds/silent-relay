"use strict";

document.querySelectorAll("[data-confirm]").forEach((element) => {
  element.addEventListener("click", (event) => {
    if (!window.confirm(element.dataset.confirm)) {
      event.preventDefault();
    }
  });
});

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const input = document.createElement("textarea");
  input.value = text;
  input.setAttribute("readonly", "");
  input.className = "copy-fallback";
  document.body.appendChild(input);
  input.select();
  const copied = document.execCommand("copy");
  input.remove();
  if (!copied) {
    throw new Error("copy failed");
  }
}

document.querySelectorAll("[data-copy-target]").forEach((button) => {
  button.addEventListener("click", async () => {
    const target = document.getElementById(button.dataset.copyTarget);
    const status = button.parentElement.querySelector("[data-copy-status]");
    if (!target || !status) {
      return;
    }

    try {
      await copyText(target.textContent.trim());
      status.textContent = button.dataset.copySuccess || "Copied";
      button.title = button.dataset.copySuccess || "Copied";
    } catch {
      status.textContent = button.dataset.copyFailure || "Copy failed";
    }
  });
});
