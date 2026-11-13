(() => {
  "use strict";

  const meta = document.querySelector('meta[name="gh-repository"]');
  const repository = meta?.getAttribute("content") || "";
  if (repository && /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)) {
    const releaseUrl = `https://github.com/${repository}/releases/latest`;
    const projectUrl = `https://github.com/${repository}`;
    document.querySelectorAll("[data-release-link]").forEach((link) => link.setAttribute("href", releaseUrl));
    document.querySelectorAll("[data-project-link]").forEach((link) => link.setAttribute("href", projectUrl));
    document.querySelectorAll("[data-license-link]").forEach((link) => link.setAttribute("href", `${projectUrl}/blob/main/LICENSE`));
  }

  const menuButton = document.querySelector(".menu-toggle");
  const navigation = document.querySelector("#main-nav");

  if (menuButton && navigation) {
    menuButton.addEventListener("click", () => {
      const isOpen = menuButton.getAttribute("aria-expanded") === "true";
      menuButton.setAttribute("aria-expanded", String(!isOpen));
      menuButton.setAttribute("aria-label", isOpen ? "Abrir menú" : "Cerrar menú");
      navigation.classList.toggle("is-open", !isOpen);
    });

    navigation.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLAnchorElement) || !navigation.classList.contains("is-open")) {
        return;
      }
      navigation.classList.remove("is-open");
      menuButton.setAttribute("aria-expanded", "false");
      menuButton.setAttribute("aria-label", "Abrir menú");
    });

    window.addEventListener("resize", () => {
      if (window.innerWidth > 760 && navigation.classList.contains("is-open")) {
        navigation.classList.remove("is-open");
        menuButton.setAttribute("aria-expanded", "false");
        menuButton.setAttribute("aria-label", "Abrir menú");
      }
    });
  }

  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      const value = button.getAttribute("data-copy");
      if (!value) {
        return;
      }
      const original = button.textContent;
      try {
        await navigator.clipboard.writeText(value);
        button.textContent = "COPIADO";
        button.setAttribute("aria-label", "Comando copiado");
      } catch {
        const range = document.createRange();
        const code = button.parentElement?.querySelector("code");
        if (code) {
          range.selectNodeContents(code);
          const selection = window.getSelection();
          selection?.removeAllRanges();
          selection?.addRange(range);
          button.textContent = "SELECCIONADO";
        }
      }
      window.setTimeout(() => {
        button.textContent = original;
        button.setAttribute("aria-label", "Copiar comando");
      }, 1500);
    });
  });
})();
