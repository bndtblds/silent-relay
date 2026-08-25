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

document.querySelectorAll("[data-local-datetime]").forEach((element) => {
  const value = new Date(element.dateTime);
  if (Number.isNaN(value.getTime())) {
    return;
  }

  try {
    const formatter = new Intl.DateTimeFormat(document.documentElement.lang || undefined, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      timeZoneName: "short",
    });
    element.textContent = formatter.format(value);
    element.title = Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch {
    // Keep the visible UTC fallback when local formatting is unavailable.
  }
});

document.querySelectorAll('input[name$="_confirm"]').forEach((confirmation) => {
  const sourceName = confirmation.name.replace(/_confirm$/, "");
  const source = confirmation.form?.elements.namedItem(sourceName);
  if (!(source instanceof HTMLInputElement)) {
    return;
  }
  const status = document.createElement("span");
  status.className = "form-hint field-match-error";
  status.setAttribute("aria-live", "polite");
  status.id = `${confirmation.id || confirmation.name}-match-status`;
  confirmation.insertAdjacentElement("afterend", status);
  confirmation.setAttribute("aria-describedby", status.id);
  const validateMatch = () => {
    const mismatch = confirmation.value !== "" && source.value !== confirmation.value;
    const message = document.documentElement.lang === "de"
      ? "Die Eingaben stimmen nicht überein."
      : "The entries do not match.";
    confirmation.setCustomValidity(mismatch ? message : "");
    status.textContent = mismatch ? message : "";
  };
  source.addEventListener("input", validateMatch);
  confirmation.addEventListener("input", validateMatch);
});
