const pendingDownloads = new Set();
const IDM_REQUEST_TIMEOUT_MS = 10000;

browser.downloads.onCreated.addListener(async (item) => {
  if (!item.url || !/^https?:\/\//i.test(item.url)) {
    return;
  }

  if (pendingDownloads.has(item.id)) {
    return;
  }
  pendingDownloads.add(item.id);

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), IDM_REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch("http://127.0.0.1:5000/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: item.url }),
      signal: controller.signal
    });
    if (!response.ok) {
      console.error(`idm-clon rechazó la descarga: HTTP ${response.status}`);
      return;
    }

    try {
      await browser.downloads.cancel(item.id);
    } catch (error) {
      console.error("No se pudo cancelar la descarga de Firefox:", error);
    }
  } catch (error) {
    if (error.name === "AbortError") {
      console.error("Tiempo de espera agotado al conectar con idm-clon:", error);
    } else {
      console.error("No se pudo conectar con idm-clon:", error);
    }
  } finally {
    clearTimeout(timeoutId);
    pendingDownloads.delete(item.id);
  }
});

browser.runtime.onMessage.addListener((message) => {
  if (!message || message.type !== "idm-clon-download-video") {
    return undefined;
  }

  if (typeof message.url !== "string" || !/^https?:\/\//i.test(message.url)) {
    return Promise.resolve({ ok: false, error: "La URL del video no es válida" });
  }

  return fetch("http://localhost:5000/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: message.url })
  }).then((response) => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return { ok: true };
  }).catch((error) => {
    console.error("No se pudo enviar el video a idm-clon:", error);
    return { ok: false, error: error.message };
  });
});
