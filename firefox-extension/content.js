(() => {
  const BUTTON_CLASS = "idm-clon-download-video";
  const CONTAINER_CLASS = "idm-clon-video-container";

  function getVideoUrl(video) {
    const source = video.currentSrc || video.src;
    if (source && /^https?:\/\//i.test(source)) return source;
    return location.href;
  }

  function makeButton(video) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = BUTTON_CLASS;
    button.textContent = "↓ Descargar";
    button.title = "Enviar este video a idm-clon";
    Object.assign(button.style, {
      position: "absolute",
      top: "12px",
      right: "12px",
      zIndex: "2147483647",
      padding: "9px 13px",
      border: "0",
      borderRadius: "5px",
      background: "#e85d04",
      color: "white",
      font: "bold 14px sans-serif",
      cursor: "pointer",
      boxShadow: "0 2px 8px #0008"
    });

    button.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      const originalText = button.textContent;
      button.disabled = true;
      button.textContent = "Enviando…";
      try {
        const response = await browser.runtime.sendMessage({
          type: "idm-clon-download-video",
          url: getVideoUrl(video)
        });
        if (!response || !response.ok) {
          throw new Error(response?.error || "No se pudo enviar el video");
        }
        button.textContent = "Enviado ✓";
      } catch (error) {
        console.error("No se pudo enviar el video a idm-clon:", error);
        button.textContent = "Error al enviar";
      }
      window.setTimeout(() => {
        button.textContent = originalText;
        button.disabled = false;
      }, 2200);
    });
    return button;
  }

  function addButtons() {
    document.querySelectorAll("video").forEach((video) => {
      const container = video.closest("#movie_player, .html5-video-container") || video.parentElement;
      if (!container || container.querySelector(`:scope > .${BUTTON_CLASS}`)) return;

      if (getComputedStyle(container).position === "static") {
        container.classList.add(CONTAINER_CLASS);
        container.style.position = "relative";
      }
      container.appendChild(makeButton(video));
    });
  }

  addButtons();
  new MutationObserver(addButtons).observe(document.documentElement, {
    childList: true,
    subtree: true
  });
})();
