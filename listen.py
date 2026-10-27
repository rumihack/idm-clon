"""Servidor local para recibir URLs de descarga desde Firefox.

Uso: python listen.py
Escucha únicamente en 127.0.0.1:5000.
"""

import importlib.util
import shutil
import time
import urllib.request
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

if os.name != "nt":
    os.environ.setdefault("DISPLAY", ":0")

HOST = "127.0.0.1"
PORT = 5000
GUI = Path(__file__).resolve().parent / "orchestrator" / "gui.py"
EXTRACT_TIMEOUT = 120
AUDIO_TIMEOUT = 3600
AUDIO_JOBS = {}
MAX_BODY = 64 * 1024


def youtube_url(url):
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain)
               for domain in ("youtube.com", "youtu.be", "youtube-nocookie.com"))


def ytdlp_command():
    explicit = os.environ.get("IDM_CLON_YT_DLP")
    if explicit:
        executable = shutil.which(explicit)
        if not executable:
            raise ValueError("IDM_CLON_YT_DLP no apunta a un ejecutable disponible")
        command = [executable]
    else:
        executable = shutil.which("yt-dlp")
        legacy = Path.home() / ".local" / "bin" / "yt-dlp"
        if executable:
            command = [executable]
        elif legacy.is_file() and os.access(legacy, os.X_OK):
            command = [str(legacy)]
        elif importlib.util.find_spec("yt_dlp") is not None:
            command = [sys.executable, "-m", "yt_dlp"]
        else:
            raise ValueError("No se encontró yt-dlp en PATH ni en el intérprete actual")
    print("yt-dlp seleccionado:", command)
    command += ["--no-playlist"]
    node = shutil.which("node")
    if node:
        command += ["--js-runtimes", "node:" + node]
    # Opt-in para instalaciones sin EJS incluido; no requiere red por defecto.
    if os.environ.get("IDM_CLON_EJS_REMOTE") == "1":
        command += ["--remote-components", "ejs:github"]
    return command


def resolve_video(command, url):
    result = subprocess.run(
        [*command, "--dump-single-json", "--skip-download", "-f",
         "bestvideo[protocol=https][ext=mp4]/best[protocol=https][ext=mp4]", "--", url],
        check=True, capture_output=True, encoding="utf-8", errors="replace",
        timeout=EXTRACT_TIMEOUT,
    )
    info = json.loads(result.stdout)
    direct = info.get("url", "")
    parsed = urlparse(direct)
    if (info.get("protocol") != "https" or parsed.scheme != "https"
            or not parsed.netloc or youtube_url(direct)
            or info.get("vcodec") in (None, "none")
            or info.get("ext") != "mp4"):
        raise ValueError("yt-dlp no devolvió un archivo de video HTTPS/MP4 directo")
    # Comprobar con el User-Agent que realmente usará el orquestador, sin
    # simular encabezados que el motor C++ no puede transmitir.
    request = urllib.request.Request(direct, headers={
        "Range": "bytes=0-511", "User-Agent": "idm-clon-orchestrator/0.1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        content_type = response.headers.get("Content-Type", "").lower()
        prefix = response.read(512).lstrip().lower()
        if (youtube_url(response.geturl()) or "html" in content_type
                or "mpegurl" in content_type or "dash+xml" in content_type
                or prefix.startswith((b"<!doctype html", b"<html", b"#extm3u", b"<?xml"))):
            raise ValueError("La URL extraída devuelve HTML o un manifiesto, no un video")
    return direct


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def supervise_audio(process, gui_process, directory, error_file):
    started = time.monotonic()
    try:
        while process.poll() is None:
            if (gui_process.poll() is not None or (directory / "release").exists()
                    or (directory / "cancel").exists()
                    or time.monotonic() - started >= AUDIO_TIMEOUT):
                stop_process(process)
                break
            time.sleep(0.2)
        error_file.close()
        temporary = directory / "audio.status.tmp"
        temporary.write_text(str(process.returncode), encoding="ascii")
        temporary.replace(directory / "audio.status")
        while gui_process.poll() is None and not (directory / "release").exists():
            time.sleep(0.2)
    finally:
        stop_process(process)
        error_file.close()
        for attempt in range(10):
            shutil.rmtree(directory, ignore_errors=True)
            if not directory.exists():
                break
            time.sleep(0.2)
        AUDIO_JOBS.pop(process, None)


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

        audio_process = None
        audio_dir = None
        error_file = None
        try:
            download_url = url
            gui_args = []
            if youtube_url(url):
                command = ytdlp_command()
                if not shutil.which("ffmpeg"):
                    raise ValueError("Se necesita ffmpeg para integrar audio y video")
                download_url = resolve_video(command, url)
                audio_dir = Path(tempfile.mkdtemp(prefix="idm-clon-youtube-"))
                audio_status = audio_dir / "audio.status"
                error_file = (audio_dir / "audio.stderr.log").open("wb")
                audio_process = subprocess.Popen(
                    [*command, "--socket-timeout", "20", "--retries", "3",
                     "-f", "bestaudio[protocol=https]/bestaudio", "-o",
                     str(audio_dir / "audio.%(ext)s"), "--", url],
                    stdout=subprocess.DEVNULL, stderr=error_file,
                )
                AUDIO_JOBS[audio_process] = (audio_dir, error_file)
                gui_args = ["--audio-dir", str(audio_dir), "--audio-status", str(audio_status)]
            gui_process = subprocess.Popen(
                [sys.executable, str(GUI), download_url, *gui_args], cwd=str(GUI.parent),
            )
            if audio_process is not None:
                threading.Thread(target=supervise_audio,
                                 args=(audio_process, gui_process, audio_dir, error_file),
                                 daemon=True).start()
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError) as exc:
            if audio_process is not None:
                stop_process(audio_process)
                AUDIO_JOBS.pop(audio_process, None)
            if error_file is not None:
                error_file.close()
            if audio_dir is not None:
                shutil.rmtree(audio_dir, ignore_errors=True)
            detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
            print("Error multimedia:", detail)
            self._reply(502, f"No se pudo resolver la URL del video: {detail}")
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

    finally:
        for process, (directory, error_file) in list(AUDIO_JOBS.items()):
            stop_process(process)
            error_file.close()
            shutil.rmtree(directory, ignore_errors=True)
