"""Consulta y selección MP4. No inicia transferencias de contenido multimedia."""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class MediaError(RuntimeError):
    pass


class Cancelled(MediaError):
    pass


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled("Operación cancelada")


def youtube_url(url):
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain)
               for domain in ("youtube.com", "youtu.be", "youtube-nocookie.com"))


def ytdlp_command():
    explicit = os.environ.get("IDM_CLON_YT_DLP")
    executable = shutil.which(explicit or "yt-dlp")
    if explicit and not executable:
        raise MediaError("IDM_CLON_YT_DLP no apunta a un ejecutable disponible")
    if executable:
        command = [executable]
    else:
        legacy = Path.home() / ".local" / "bin" / "yt-dlp"
        if legacy.is_file() and os.access(legacy, os.X_OK):
            command = [str(legacy)]
        elif importlib.util.find_spec("yt_dlp") is not None:
            command = [sys.executable, "-m", "yt_dlp"]
        else:
            raise MediaError("No se encontró yt-dlp en PATH ni en el intérprete actual")
    command += ["--no-playlist"]
    node = shutil.which("node")
    if node:
        command += ["--js-runtimes", "node:" + node]
    if os.environ.get("IDM_CLON_EJS_REMOTE") == "1":
        command += ["--remote-components", "ejs:github"]
    return command


def run_process(args, cancel, timeout):
    """Drena ambos pipes y permite cancelar sin bloquear Tkinter."""
    check_cancel(cancel)
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   encoding="utf-8", errors="replace")
    except OSError as exc:
        raise MediaError(f"No se pudo iniciar la herramienta: {exc}") from exc
    deadline = time.monotonic() + timeout
    try:
        while True:
            check_cancel(cancel)
            if time.monotonic() >= deadline:
                raise MediaError("Se agotó el tiempo de espera de la herramienta")
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
        check_cancel(cancel)
        if process.returncode:
            raise MediaError("La herramienta falló: " + stderr.strip()[-3000:])
        return stdout
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def fetch_metadata(url, cancel):
    check_cancel(cancel)
    if not youtube_url(url):
        raise MediaError("El selector necesita una URL de YouTube")
    raw = run_process([*ytdlp_command(), "--dump-single-json", "--skip-download",
                       "--socket-timeout", "20", "--retries", "2", "--", url],
                      cancel, 120)
    try:
        info = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise MediaError("yt-dlp no devolvió metadatos válidos") from exc
    if not isinstance(info, dict) or not isinstance(info.get("formats"), list):
        raise MediaError("yt-dlp no devolvió una lista de formatos")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise MediaError("Esta etapa admite videos terminados, no emisiones en directo")
    return info


def direct_format(fmt):
    url = fmt.get("url")
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    return (fmt.get("protocol") == "https" and parsed.scheme == "https"
            and bool(parsed.netloc) and not youtube_url(url)
            and not fmt.get("has_drm") and not fmt.get("fragments"))


def has_audio(fmt):
    return fmt.get("acodec") not in (None, "none")


@dataclass(frozen=True)
class Choice:
    video_id: str
    audio_id: str | None
    width: int
    height: int
    fps: float
    vcodec: str

    @property
    def label(self):
        return (f"{min(self.width, self.height)}p — {self.width}×{self.height} — "
                f"{self.fps:g} fps — MP4 / {self.vcodec}")


def mp4_choices(info):
    formats = [f for f in info.get("formats", []) if isinstance(f, dict) and direct_format(f)]
    audios = [f for f in formats if f.get("vcodec") == "none" and has_audio(f)
              and f.get("ext") in ("m4a", "mp4")
              and str(f.get("acodec", "")).startswith(("mp4a", "aac"))
              and f.get("format_id")]
    # yt-dlp entrega formatos de menor a mayor preferencia.
    audio = audios[-1] if audios else None
    choices = {}
    for fmt in formats:
        codec = str(fmt.get("vcodec") or "none")
        if (fmt.get("ext") != "mp4" or not fmt.get("format_id")
                or not codec.startswith(("avc", "h264", "hev", "hvc", "hevc", "av01", "vp09", "vp9"))):
            continue
        width, height = fmt.get("width"), fmt.get("height")
        if not isinstance(width, int) or not isinstance(height, int) or min(width, height) <= 0:
            continue
        if not has_audio(fmt) and audio is None:
            continue
        fps = fmt.get("fps") or 0
        if not isinstance(fps, (int, float)):
            continue
        choice = Choice(str(fmt["format_id"]), None if has_audio(fmt) else str(audio["format_id"]),
                        width, height, fps, codec)
        # Una opción por dimensiones/FPS; conservar la preferencia de yt-dlp.
        choices[(width, height, fps)] = choice
    return sorted(choices.values(), key=lambda c: (c.height, c.width, c.fps), reverse=True)


def resolve_choice(info, choice):
    """No cambiar silenciosamente de resolución, códec o pistas al reconsultar."""
    if choice not in mp4_choices(info):
        raise MediaError("El formato seleccionado ya no está disponible. Vuelve a consultar el video")
    formats = {str(f.get("format_id")): f for f in info["formats"] if isinstance(f, dict)}
    return formats[choice.video_id], formats.get(choice.audio_id)


def validate_stream(fmt, cancel):
    check_cancel(cancel)
    if not direct_format(fmt):
        raise MediaError("El formato no es un archivo HTTPS directo compatible")
    request = urllib.request.Request(fmt["url"], headers={
        "Range": "bytes=0-511", "User-Agent": "idm-clon-orchestrator/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            kind = response.headers.get("Content-Type", "").lower()
            prefix = response.read(512).lstrip().lower()
            if (youtube_url(response.geturl()) or any(s in kind for s in ("html", "mpegurl", "dash+xml"))
                    or prefix.startswith((b"<!doctype", b"<html", b"#extm3u", b"<?xml"))):
                raise MediaError("El servidor devolvió HTML o un manifiesto; no se iniciará la descarga")
    except OSError as exc:
        raise MediaError("No se puede acceder al formato con el descargador actual: " + str(exc)) from exc
    check_cancel(cancel)


def available_choices(info, cancel):
    """Excluir URLs que ya fallan antes de ofrecerlas en la ventana."""
    available = []
    checked = {}
    formats = {str(f.get("format_id")): f for f in info.get("formats", []) if isinstance(f, dict)}
    for choice in mp4_choices(info):
        valid = True
        for format_id in (choice.video_id, choice.audio_id):
            if format_id is None:
                continue
            if format_id not in checked:
                try:
                    validate_stream(formats[format_id], cancel)
                    checked[format_id] = True
                except Cancelled:
                    raise
                except MediaError:
                    checked[format_id] = False
            valid = valid and checked[format_id]
        if valid:
            available.append(choice)
    return available


def safe_filename(title):
    """Un solo componente de ruta válido en Windows y Linux, preservando Unicode."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(title)).strip().rstrip(". ")
    if name.lower().endswith(".mp4"):
        name = name[:-4].rstrip(". ")
    name = name[:120].rstrip(". ") or "video"
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        name = "_" + name
    return name + ".mp4"


def destination(folder, name):
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise MediaError("La carpeta de destino no existe")
    target = folder / safe_filename(name)
    if os.path.lexists(target):
        raise MediaError("El archivo ya existe. Elige otro nombre; no se sobrescribirá")
    return target


def publish(source, target):
    """Publicación atómica sin reemplazo. Ambos archivos están en el mismo volumen."""
    try:
        if os.name == "nt":
            # rename en Windows falla si el destino existe, también en exFAT.
            os.rename(source, target)
        else:
            os.link(source, target)
    except FileExistsError as exc:
        raise MediaError("El destino ya existe; no se sobrescribió") from exc
    except OSError as exc:
        raise MediaError("No se pudo publicar el archivo sin sobrescribir: " + str(exc)) from exc
