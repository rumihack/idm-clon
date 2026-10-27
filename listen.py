"""Servidor local para recibir URLs de descarga desde Firefox.

Uso: python listen.py
Escucha únicamente en 127.0.0.1:5000.
"""

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from orchestrator.media import youtube_url

if os.name != "nt":
    os.environ.setdefault("DISPLAY", ":0")

HOST = "127.0.0.1"
PORT = 5000
GUI = Path(__file__).resolve().parent / "orchestrator" / "gui.py"
MAX_BODY = 64 * 1024


class DownloadRequestHandler(BaseHTTPRequestHandler):
    def _reply(self, status, message):
        body = json.dumps({"message": message}, ensure_ascii=False).encode("utf-8")
        # Responder 200 también ante errores de procesamiento evita que Firefox
        # trate la petición local como un fallo de conexión.
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        url = None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if 0 < length <= MAX_BODY:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                url = payload.get("url") if isinstance(payload, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            pass

        print("URL RECIBIDA:", url)
        if self.path not in ("/", "/download"):
            self._reply(404, "Ruta no encontrada")
            return
        try:
            if not isinstance(url, str):
                raise ValueError("Tamaño de petición no válido")
            parsed = urlparse(url)
            if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("Se requiere una URL http o https")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._reply(400, str(exc))
            return

        try:
            gui_args = ["--select-youtube"] if youtube_url(url) else []
            subprocess.Popen(
                [sys.executable, str(GUI), url, *gui_args], cwd=str(GUI.parent),
            )
        except OSError as exc:
            self._reply(502, f"No se pudo abrir la ventana de descarga: {exc}")
            return
        self._reply(200, "Ventana de descarga abierta")

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")


if __name__ == "__main__":
    print(f"idm-clon escucha en http://{HOST}:{PORT}/download")
    try:
        HTTPServer((HOST, PORT), DownloadRequestHandler).serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
