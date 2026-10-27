"""Trabajo MP4 aislado: motor existente, audio yt-dlp y combinación FFmpeg."""

import shutil
import tempfile
import time
from pathlib import Path

if __package__:
    from . import media
else:
    import media


def download_mp4(url, choice, target, cancel, core, active, connections, progress, log):
    media.check_cancel(cancel)
    # Comprobar otra vez antes de hacer trabajo costoso; publish también protege
    # frente a un archivo creado por otro proceso durante la descarga.
    target = media.destination(target.parent, target.name)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise media.MediaError("Se necesita FFmpeg para generar el MP4 final")
    started = time.monotonic()
    log("Actualizando los formatos seleccionados…")
    info = media.fetch_metadata(url, cancel)
    video, audio = media.resolve_choice(info, choice)
    media.validate_stream(video, cancel)
    if audio:
        media.validate_stream(audio, cancel)
    # En el destino para publicar con un enlace atómico en el mismo volumen.
    with tempfile.TemporaryDirectory(prefix=".idm-clon-", dir=target.parent) as temporary:
        stage = Path(temporary)
        video_path = stage / "video.mp4"
        log("Descargando video con el motor existente…")
        core.download(video["url"], output=video_path, connections=connections,
                      progress=progress, active=active, log=log)
        media.check_cancel(cancel)
        inputs = ["-i", str(video_path)]
        audio_map = "0:a:0"
        if audio:
            log("Descargando la pista de audio seleccionada…")
            audio_path = stage / "audio.m4a"
            media.run_process(
                [*media.ytdlp_command(), "--socket-timeout", "20", "--retries", "3",
                 "--no-part", "--fixup", "never", "-f", choice.audio_id,
                 "-o", str(audio_path), "--", url], cancel, 3600)
            if not audio_path.is_file() or audio_path.stat().st_size == 0:
                raise media.MediaError("yt-dlp no generó la pista de audio seleccionada")
            inputs += ["-i", str(audio_path)]
            audio_map = "1:a:0"
        media.check_cancel(cancel)
        log("Combinando video y audio con FFmpeg…")
        merged = stage / "final.mp4"
        media.run_process(
            [ffmpeg, "-nostdin", "-n", *inputs, "-map", "0:v:0", "-map", audio_map,
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
             str(merged)], cancel, 600)
        media.check_cancel(cancel)
        if not merged.is_file() or merged.stat().st_size == 0:
            raise media.MediaError("FFmpeg no generó un archivo MP4 válido")
        media.publish(merged, target)
        size = target.stat().st_size
    return target, size, time.monotonic() - started
