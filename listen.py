"""Servidor local para recibir URLs de descarga desde Firefox.

Uso: python listen.py
Escucha únicamente en 127.0.0.1:5000.
"""

import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOST = "127.0.0.1"
PORT = 5000
GUI = Path(__file__).resolve().parent / "orchestrator" / "gui.py"
MAX_BODY = 64 * 1024


class DownloadRequestHandler(BaseHTTPRequestHandler):
    def _reply(self, status, message):
        body = json.dumps({"message": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if self.path != "/download":
            self._reply(404, "Ruta no encontrada")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("Tamaño de petición no válido")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            url = payload.get("url") if isinstance(payload, dict) else None
            parsed = urlparse(url) if isinstance(url, str) else None
            if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("Se requiere una URL http o https")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._reply(400, str(exc))
            return

        try:
            subprocess.Popen([sys.executable, str(GUI), url], cwd=str(GUI.parent))
        except OSError as exc:
            self._reply(500, f"No se pudo abrir la GUI: {exc}")
            return
        self._reply(202, "GUI abierta; descarga iniciada")

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")


if __name__ == "__main__":
    print(f"idm-clon escucha en http://{HOST}:{PORT}/download")
    try:
        HTTPServer((HOST, PORT), DownloadRequestHandler).serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
