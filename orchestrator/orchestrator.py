"""Orquestador de idm-clon: descarga un archivo en fragmentos paralelos.

Flujo:
  1. Sondea el servidor (petición con Range: bytes=0-0) para conocer el tamaño
     total y saber si admite descargas por rangos.
  2. Divide el archivo en N rangos y lanza una instancia de core_downloader.exe
     por rango, en paralelo, con reintentos. Lee en tiempo real las líneas
     "PROGRESS" que emite cada proceso y muestra una barra de progreso global.
  3. Une los fragmentos en orden, verifica el tamaño final y borra los temporales.

Reanudación: los fragmentos se guardan en <salida>.parts/ junto con un
manifest.json (URL, tamaño, ETag/Last-Modified y rangos). Si la descarga se
interrumpe (fallo, Ctrl+C, corte de luz...), al repetir el mismo comando se
reutiliza ese plan y cada fragmento continúa desde el último byte en disco.
Si el archivo remoto cambió, los fragmentos viejos se descartan.

Si el servidor no admite rangos (o dice admitirlos pero no los respeta),
descarga el archivo completo en una sola conexión desde Python.

Uso:
  python orchestrator.py <url> [-o salida] [-n conexiones] [--retries N] [--restart]
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

USER_AGENT = "idm-clon-orchestrator/0.1"
CORE_DOWNLOADER_NAME = "core_downloader.exe" if os.name == "nt" else "core_downloader"
DEFAULT_EXE = (Path(__file__).resolve().parent.parent / "core_downloader" / "bin"
               / CORE_DOWNLOADER_NAME)
MIN_PART_SIZE = 256 * 1024  # no tiene sentido abrir conexiones para trozos diminutos
MANIFEST_NAME = "manifest.json"

# Códigos de salida de core_downloader (ver core_downloader/main.cpp).
EXIT_OK = 0
EXIT_NETWORK = 3
EXIT_RANGE_REJECTED = 4
EXIT_SIZE_MISMATCH = 5
RETRYABLE_EXITS = {EXIT_NETWORK, EXIT_SIZE_MISMATCH}


def console_log(message, error=False):
    """Destino por defecto de los mensajes: la consola (stderr si es un error)."""
    print(message, file=sys.stderr if error else sys.stdout)


class DownloadError(Exception):
    pass


class RangesNotHonored(DownloadError):
    """El servidor anunció rangos pero no los respetó (códigos 4 o 5)."""


def probe(url):
    """Devuelve (url_final, tamaño_total o None, admite_rangos, huella).

    La huella (ETag o, en su defecto, Last-Modified) identifica la versión del
    archivo remoto, para no mezclar al reanudar fragmentos de versiones distintas.
    """
    request = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        final_url = response.geturl()
        validator = response.headers.get("ETag") or response.headers.get("Last-Modified")
        if response.status == 206:
            match = re.match(r"bytes \d+-\d+/(\d+)", response.headers.get("Content-Range", ""))
            if match:
                return final_url, int(match.group(1)), True, validator
        length = response.headers.get("Content-Length")
        # 200: el servidor ignoró el Range; Content-Length es el tamaño completo.
        size = int(length) if response.status == 200 and length else None
        return final_url, size, False, validator


def format_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class ProgressBar:
    """Barra de progreso global en una sola línea, redibujada desde un hilo propio.

    Los hilos de descarga solo actualizan contadores (update); el dibujo se hace
    a intervalos fijos para no saturar la consola con cientos de líneas PROGRESS.
    """

    WIDTH = 30

    def __init__(self, total, done):
        """done: bytes ya en disco por fragmento (distintos de 0 al reanudar)."""
        self.total = total
        self.done = list(done)
        self.resumed = sum(done)  # no cuentan para la velocidad de esta sesión
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.last_len = 0
        # Fuera de una terminal (redirigido a archivo/pipe) se redibuja menos.
        self.interval = 0.1 if sys.stdout.isatty() else 1.0
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stopped.set()
        self.thread.join()
        with self.lock:
            self._draw()
            sys.stdout.write("\n")

    def update(self, index, nbytes):
        with self.lock:
            self.done[index] = nbytes

    def log(self, message):
        """Imprime un mensaje encima de la barra sin romperla."""
        with self.lock:
            sys.stdout.write("\r" + message.ljust(self.last_len) + "\n")
            self._draw()

    def _run(self):
        while not self.stopped.wait(self.interval):
            with self.lock:
                self._draw()

    def _draw(self):
        # Un servidor que no respeta rangos puede enviar de más: se limita al total.
        done = min(sum(self.done), self.total)
        fraction = done / self.total
        filled = int(fraction * self.WIDTH)
        elapsed = time.monotonic() - self.started
        speed = (done - self.resumed) / elapsed if elapsed > 0 else 0
        eta = f"{(self.total - done) / speed:.0f} s" if speed > 0 else "--"
        line = (
            f"[{'█' * filled}{'░' * (self.WIDTH - filled)}] {fraction:6.1%}  "
            f"{format_bytes(done)}/{format_bytes(self.total)}  "
            f"{format_bytes(speed)}/s  ETA {eta}"
        )
        # Se rellena con espacios para borrar restos de una línea anterior más larga.
        sys.stdout.write("\r" + line.ljust(self.last_len))
        sys.stdout.flush()
        self.last_len = len(line)


class ActiveProcesses:
    """Registro de procesos core_downloader en marcha, para matarlos si se aborta."""

    def __init__(self):
        self.procs = set()
        self.lock = threading.Lock()
        self.aborted = False

    def start(self, args):
        with self.lock:
            if self.aborted:
                raise DownloadError("descarga cancelada")
            # stderr solo recibe un mensaje corto de error, así que leerlo al final
            # no puede llenar su pipe y bloquear al proceso.
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",  # main.cpp está en UTF-8 y sus mensajes también
                errors="replace",
            )
            self.procs.add(proc)
            return proc

    def finished(self, proc):
        with self.lock:
            self.procs.discard(proc)

    def kill_all(self):
        with self.lock:
            self.aborted = True
            for proc in self.procs:
                proc.kill()


def split_ranges(total, connections):
    """Divide [0, total) en rangos inclusivos (inicio, fin) de tamaño parecido."""
    parts = max(1, min(connections, total // MIN_PART_SIZE))
    base, extra = divmod(total, parts)
    ranges, start = [], 0
    for i in range(parts):
        size = base + (1 if i < extra else 0)
        ranges.append((start, start + size - 1))
        start += size
    return ranges


def bytes_on_disk(part_path, size):
    """Bytes válidos ya descargados de un fragmento de `size` bytes."""
    have = part_path.stat().st_size if part_path.exists() else 0
    if have > size:
        # core_downloader nunca escribe de más, así que esto es un archivo ajeno.
        part_path.unlink()
        return 0
    return have


def download_part(exe, url, index, start, end, part_path, retries, bar, active):
    """Descarga un fragmento con core_downloader, reintentando fallos transitorios.

    Cada intento (y cada reanudación) continúa desde el último byte en disco:
    core_downloader garantiza que lo escrito es siempre un prefijo válido del rango.
    """
    size = end - start + 1
    for attempt in range(1, retries + 1):
        have = bytes_on_disk(part_path, size)
        bar.update(index, have)
        if have == size:
            return index

        args = [str(exe), url, str(start + have), str(end), str(part_path)]
        if have:
            args.append("--append")
        proc = active.start(args)
        try:
            for line in proc.stdout:  # una línea "PROGRESS <escritos> <esperados>" por cada 1 %
                if line.startswith("PROGRESS "):
                    bar.update(index, have + int(line.split()[1]))
            message = proc.stderr.read().strip()
            returncode = proc.wait()
        finally:
            active.finished(proc)

        if returncode == EXIT_OK:
            return index
        if returncode not in RETRYABLE_EXITS or attempt == retries:
            error = f"fragmento {index} ({start}-{end}): código {returncode}: {message}"
            if returncode in (EXIT_RANGE_REJECTED, EXIT_SIZE_MISMATCH):
                raise RangesNotHonored(error)
            raise DownloadError(error)
        bar.log(f"  fragmento {index}: intento {attempt} falló ({message}); reintentando...")
        time.sleep(attempt)  # espera creciente entre reintentos


def plan_parts(parts_dir, url, total, validator, connections, log=console_log):
    """Devuelve (rangos, reanudada): reutiliza el plan guardado si sigue siendo válido."""
    manifest_path = parts_dir / MANIFEST_NAME
    if parts_dir.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = None
        same_file = (
            isinstance(manifest, dict)
            and manifest.get("url") == url
            and manifest.get("total") == total
            and manifest.get("validator") == validator
            and isinstance(manifest.get("ranges"), list)
        )
        if same_file:
            return [tuple(r) for r in manifest["ranges"]], True
        log("Hay fragmentos de otra versión del archivo (o de otra URL): se descartan.")
        shutil.rmtree(parts_dir)

    parts_dir.mkdir()
    ranges = split_ranges(total, connections)
    manifest = {"url": url, "total": total, "validator": validator, "ranges": ranges}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return ranges, False


def download_parallel(exe, url, final_url, total, validator, output, connections, retries,
                      progress=ProgressBar, active=None, log=console_log):
    """progress: fábrica (total, hechos_por_fragmento) de un objeto con la interfaz de
    ProgressBar (context manager con update y log). active: ActiveProcesses externo,
    para poder cancelar desde fuera (la GUI) llamando a kill_all()."""
    parts_dir = output.with_name(output.name + ".parts")
    ranges, resumed = plan_parts(parts_dir, url, total, validator, connections, log)
    part_paths = [parts_dir / f"part{i:03d}.tmp" for i in range(len(ranges))]
    done = [bytes_on_disk(path, end - start + 1) for path, (start, end) in zip(part_paths, ranges)]

    if resumed:
        log(f"Reanudando: {format_bytes(sum(done))} de {format_bytes(total)} ya descargados.")
    parts_label = "1 fragmento" if len(ranges) == 1 else f"{len(ranges)} fragmentos paralelos"
    log(f"Descargando {format_bytes(total)} en {parts_label}...")
    active = active or ActiveProcesses()
    try:
        with progress(total, done) as bar, ThreadPoolExecutor(max_workers=len(ranges)) as pool:
            futures = [
                pool.submit(download_part, exe, final_url, i, start, end, part_paths[i], retries, bar, active)
                for i, (start, end) in enumerate(ranges)
            ]
            try:
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                # Un fragmento falló sin remedio (o Ctrl+C): se para todo lo demás.
                for f in futures:
                    f.cancel()
                active.kill_all()
                raise

        log("Uniendo fragmentos...")
        with open(output, "wb") as out:
            for path in part_paths:
                with open(path, "rb") as part:
                    shutil.copyfileobj(part, out)

        if output.stat().st_size != total:
            raise DownloadError(f"tamaño final {output.stat().st_size} distinto del esperado {total}")
    except RangesNotHonored:
        # Se pasará a descarga en una sola conexión: los fragmentos no sirven.
        shutil.rmtree(parts_dir, ignore_errors=True)
        raise
    except BaseException:
        log(f"Descarga interrumpida. Los fragmentos se conservan en {parts_dir};", error=True)
        log("vuelve a ejecutar el mismo comando para reanudar.", error=True)
        raise
    shutil.rmtree(parts_dir)


def download_single(url, output, expected, log=console_log):
    """Descarga sin rangos, en una sola conexión (servidores que no los admiten)."""
    log("El servidor no admite rangos: descargando en una sola conexión...")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response, open(output, "wb") as out:
        shutil.copyfileobj(response, out)
    size = output.stat().st_size
    if expected is not None and size != expected:
        raise DownloadError(f"tamaño final {size} distinto del esperado {expected}")


def default_output_name(url):
    name = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name
    return name or "descarga.bin"


def download(url, output=None, output_dir=None, connections=8, retries=3, exe=DEFAULT_EXE,
             restart=False, progress=ProgressBar, active=None, log=console_log):
    """Descarga `url` completa y devuelve (ruta_final, bytes, segundos).

    output: ruta del archivo; si falta, se usa el nombre de la URL dentro de
    output_dir (o del directorio actual). Los demás parámetros opcionales
    permiten a la GUI sustituir la barra de consola, cancelar y recibir mensajes.
    """
    final_url, total, ranges_ok, validator = probe(url)
    if output is None:
        output = Path(output_dir or ".") / default_output_name(final_url)
    output = Path(output).resolve()
    if restart:
        shutil.rmtree(output.with_name(output.name + ".parts"), ignore_errors=True)
    began = time.monotonic()

    if ranges_ok and total > 0:
        if not Path(exe).is_file():
            raise DownloadError(f"no se encontró {exe}; ejecuta core_downloader\\build.bat")
        try:
            download_parallel(exe, url, final_url, total, validator, output, connections, retries,
                              progress, active, log)
        except RangesNotHonored as exc:
            log(f"Aviso: el servidor no respeta los rangos ({exc}).", error=True)
            # El tamaño anunciado no es fiable, así que no se verifica.
            download_single(final_url, output, None, log)
    else:
        download_single(final_url, output, total, log)

    return output, output.stat().st_size, time.monotonic() - began


def main():
    # UTF-8 y volcado por línea: acentos correctos en pipes (Git Bash) y
    # mensajes de stdout/stderr en el orden en que se emiten.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", line_buffering=True)

    parser = argparse.ArgumentParser(description="Descarga un archivo en fragmentos paralelos.")
    parser.add_argument("url")
    parser.add_argument("-o", "--output", help="archivo de salida (por defecto, el nombre de la URL)")
    parser.add_argument("-n", "--connections", type=int, default=8, help="conexiones paralelas (8); al reanudar se mantiene el reparto original")
    parser.add_argument("--retries", type=int, default=3, help="intentos por fragmento (3)")
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE,
                        help="ruta al ejecutable core_downloader")
    parser.add_argument("--restart", action="store_true",
                        help="descarta los fragmentos de una descarga anterior y empieza de cero")
    args = parser.parse_args()

    if args.connections < 1 or args.retries < 1:
        parser.error("--connections y --retries deben ser >= 1")

    try:
        output, size, elapsed = download(args.url, args.output, connections=args.connections,
                                         retries=args.retries, exe=args.exe, restart=args.restart)
        speed = size / elapsed / 1024 / 1024 if elapsed > 0 else 0
        print(f"OK {output} ({size} bytes en {elapsed:.1f} s, {speed:.2f} MB/s)")
        return 0
    except (DownloadError, OSError) as exc:
        # urllib.error.URLError hereda de OSError.
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelado por el usuario.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
