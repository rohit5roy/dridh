// Copy-to-clipboard with a toast. Plain ES module; no build step.

const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* Toasts: one polite live region, newest at the bottom, gone after 3.2 s. */
let region;
export function toast(title, description = "") {
  if (!region) {
    region = document.createElement("ol");
    region.className = "toasts";
    region.setAttribute("aria-live", "polite");
    document.body.append(region);
  }
  const item = document.createElement("li");
  item.className = "toast";
  item.innerHTML = `<strong class="toast-title"></strong><span class="toast-text"></span>`;
  item.querySelector(".toast-title").textContent = title;
  item.querySelector(".toast-text").textContent = description;
  region.append(item);
  requestAnimationFrame(() => item.classList.add("is-in"));
  setTimeout(() => {
    item.classList.remove("is-in");
    item.addEventListener("transitionend", () => item.remove(), { once: true });
    if (reduce) item.remove();
  }, 3200);
}

for (const button of document.querySelectorAll("[data-copy]")) {
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      toast("Install command copied", "Paste it into a terminal on Linux or macOS.");
    } catch {
      toast("Could not copy", "Select the command and copy it by hand.");
    }
  });
}

/* The latest stable release, read from GitHub when the page loads; "pre-1.0" stays if GitHub cannot be reached. */
const release = document.querySelector("[data-release]");
if (release) {
  fetch("https://api.github.com/repos/rohit5roy/dridh/releases/latest", { headers: { Accept: "application/vnd.github+json" } })
    .then((response) => (response.ok ? response.json() : null))
    .then((latest) => { if (latest && /^v\d+\.\d+\.\d+/.test(latest.tag_name || "")) release.textContent = latest.tag_name; })
    .catch(() => {});
}
