browser.downloads.onCreated.addListener((item) => {
  if (!item.url || !/^https?:\/\//i.test(item.url)) {
    return;
  }

  // Cancelar la descarga del navegador para que solo la procese idm-clon.
  browser.downloads.cancel(item.id).catch(() => {});

  fetch("http://127.0.0.1:5000/download", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: item.url })
  }).catch((error) => {
    console.error("No se pudo conectar con idm-clon:", error);
  });
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
