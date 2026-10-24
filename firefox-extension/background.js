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
