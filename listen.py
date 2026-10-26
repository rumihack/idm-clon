"""Servidor local para recibir URLs de descarga desde Firefox.

Uso: python listen.py
Escucha únicamente en 127.0.0.1:5000.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

os.environ["DISPLAY"] = ":0"

HOST = "127.0.0.1"
PORT = 5000
GUI = Path(__file__).resolve().parent / "orchestrator" / "gui.py"
YT_DLP = Path.home() / ".local" / "bin" / "yt-dlp"
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
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if self.path not in ("/", "/download"):
            self._reply(404, "Ruta no encontrada")
            return

        url = None
        try:
            content_length = self.headers.get("Content-Length")
            if content_length is None:
                raise ValueError("Tamaño de petición no válido")
            length = int(content_length)
            if length < 0:
                raise ValueError("Tamaño de petición no válido")
        except ValueError:
            self._reply(400, "Tamaño de petición no válido")
            return

        if length > MAX_BODY:
            self._reply(413, "Tamaño de petición no válido")
            return
        if length == 0:
            self._reply(400, "Tamaño de petición no válido")
            return

        try:
            body = self.rfile.read(length).decode("utf-8")
        except UnicodeDecodeError:
            self._reply(400, "Codificación UTF-8 no válida")
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._reply(400, "JSON no válido")
            return
        url = payload.get("url") if isinstance(payload, dict) else None

        print("URL RECIBIDA:", url)
        try:
            if not isinstance(url, str):
                raise ValueError("Se requiere una URL http o https")
            parsed = urlparse(url)
            if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("Se requiere una URL http o https")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._reply(400, str(exc))
            return

        try:
            download_url = url
            gui_args = []
            if "youtube.com" in url.lower() or "youtu.be" in url.lower():
                try:
                    audio_dir = Path(tempfile.mkdtemp(prefix="idm-clon-youtube-"))
                    audio_status = audio_dir / "audio.status"
                    audio_process = subprocess.Popen(
                        [
                            str(YT_DLP),
                            "-f",
                            "bestaudio[protocol=https]/bestaudio",
                            "-o",
                            str(audio_dir / "audio.%(ext)s"),
                            url,
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    gui_args = ["--audio-dir", str(audio_dir), "--audio-status", str(audio_status)]

                    def record_audio_result():
                        return_code = audio_process.wait()
                        temporary_status = audio_status.with_suffix(".tmp")
                        temporary_status.write_text(str(return_code), encoding="ascii")
                        temporary_status.replace(audio_status)

                    threading.Thread(target=record_audio_result, daemon=True).start()

                    result = subprocess.run(
                        [
                            str(YT_DLP),
                            "-g",
                            "-f",
                            "bestvideo[protocol=https]/best[protocol=https]/18",
                            url,
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    stream_urls = result.stdout.strip().splitlines()
                    if not stream_urls:
                        raise ValueError("yt-dlp no devolvió una URL directa")
                    download_url = stream_urls[0].strip()

                    if ".m3u8" in download_url.lower():
                        result = subprocess.run(
                            [str(YT_DLP), "-g", "-f", "18", url],
                            check=True,
                            capture_output=True,
                            text=True,
                        )
                        stream_urls = result.stdout.strip().splitlines()
                        if not stream_urls:
                            raise ValueError(
                                "yt-dlp no devolvió una URL para el formato 18"
                            )
                        download_url = stream_urls[0].strip()
                except (OSError, subprocess.CalledProcessError, ValueError) as exc:
                    print(f"yt-dlp falló; se usará la URL original: {exc}")
                    download_url = url
            subprocess.Popen(
                [sys.executable, str(GUI), download_url, *gui_args],
                cwd=str(GUI.parent),
            )
        except (OSError, subprocess.CalledProcessError, ValueError, IndexError) as exc:
            detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
            self._reply(500, f"No se pudo resolver la URL del video: {detail}")
            return
        self._reply(200, "GUI abierta; descarga iniciada")

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")


if __name__ == "__main__":
    print(f"idm-clon escucha en http://{HOST}:{PORT}/download")
    try:
        HTTPServer((HOST, PORT), DownloadRequestHandler).serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
