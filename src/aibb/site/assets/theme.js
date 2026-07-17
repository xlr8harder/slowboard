(() => {
  const root = document.documentElement;
  const stored = localStorage.getItem("aibb-theme");
  if (stored === "light" || stored === "dark") root.dataset.theme = stored;
  addEventListener("DOMContentLoaded", () => {
    const button = document.querySelector("[data-theme-toggle]");
    if (!button) return;
    button.addEventListener("click", () => {
      const current = root.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      const next = current === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      localStorage.setItem("aibb-theme", next);
    });
  });
})();
